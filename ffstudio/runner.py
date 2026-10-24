import json
import os
import re
import subprocess
import time
from typing import Any, List, Optional, Tuple

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from ffstudio.config import APP_DIR
from ffstudio.environment import kill_process_tree

ANSI_PATTERN = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
# facefusion draws its bar as <active color>====<neutral color>====<reset>
PROGRESS_PATTERN = re.compile(r'\x1b\[G(?P<title>.*?)\s*\x1b\[94m(?P<done>=*)\x1b\[90m(?P<todo>=*)\x1b\[0m\s*(?P<status>[^|\x1b]*)(?:\|\s*(?P<description>[^\x1b]*))?')
# preview_worker.py ends each preview with this line
WORKER_RESULT_PREFIX = 'PREVIEW_DONE '
# the worker lets go of its models after this long without a preview
WORKER_IDLE_SECONDS = 5 * 60
# after this many failures in a row previews go back to starting facefusion.py each time
WORKER_FAILURE_LIMIT = 2


def quote_arg(arg : str) -> str:
	return subprocess.list2cmdline([ arg ])


class ProcessRunner(QObject):
	"""runs one external command at a time, streaming log lines and parsed facefusion progress"""
	started = Signal(str)
	log = Signal(str)
	progress = Signal(str, float, str)
	finished = Signal(int, float)

	def __init__(self, parent : Optional[QObject] = None) -> None:
		super().__init__(parent)
		self.process : Optional[QProcess] = None
		self.buffer = ''
		self.start_time = 0.0
		self.cancelled = False
		# the preview worker running the current command, and how to run it without the worker
		self.worker : Optional['PreviewWorker'] = None
		self.fallback : Optional[Tuple[str, List[str], str, Optional[QProcessEnvironment]]] = None

	def is_running(self) -> bool:
		return self.worker is not None or (self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning)

	def start(self, program : str, args : List[str], working_dir : str, environment : Optional[QProcessEnvironment] = None) -> bool:
		if self.is_running():
			return False
		self.buffer = ''
		self.cancelled = False
		self.start_time = time.monotonic()
		self.process = QProcess(self)
		self.process.setProgram(program)
		self.process.setArguments(args)
		self.process.setWorkingDirectory(working_dir)
		self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
		if environment is not None:
			self.process.setProcessEnvironment(environment)
		self.process.readyReadStandardOutput.connect(self.on_ready_read)
		self.process.finished.connect(self.on_finished)
		self.process.errorOccurred.connect(self.on_error)
		self.started.emit(' '.join(quote_arg(part) for part in [ program ] + args))
		self.process.start()
		return True

	def start_on_worker(self, worker : 'PreviewWorker', program : str, args : List[str], working_dir : str, environment : Optional[QProcessEnvironment] = None) -> bool:
		"""runs facefusion.py's arguments on the preview worker, or as a process of their own when the worker fails"""
		if self.is_running():
			return False
		self.buffer = ''
		self.cancelled = False
		self.start_time = time.monotonic()
		self.worker = worker
		self.fallback = (program, args, working_dir, environment)
		worker.log.connect(self.emit_line)
		worker.done.connect(self.on_worker_done)
		self.started.emit(' '.join(quote_arg(part) for part in [ program ] + args) + '   (on the preview worker)')
		worker.run(program, working_dir, environment, args[1:])
		return True

	def on_worker_done(self, exit_code : Optional[int]) -> None:
		worker, self.worker = self.worker, None
		worker.log.disconnect(self.emit_line)
		worker.done.disconnect(self.on_worker_done)
		fallback, self.fallback = self.fallback, None
		if self.cancelled:
			self.finished.emit(-1, time.monotonic() - self.start_time)
			return
		if exit_code is None:
			# the same preview the usual way, only slower
			self.log.emit('The preview worker stopped, making this preview with facefusion.py instead')
			self.start(*fallback)
			return
		self.finished.emit(exit_code, time.monotonic() - self.start_time)

	def stop(self) -> None:
		if self.worker is not None:
			self.cancelled = True
			self.worker.stop()
		elif self.is_running():
			self.cancelled = True
			kill_process_tree(int(self.process.processId()))
			self.process.kill()

	def on_ready_read(self) -> None:
		chunk = bytes(self.process.readAllStandardOutput().data()).decode('utf-8', errors = 'replace')
		self.buffer += chunk
		self.flush_buffer(final = False)

	def flush_buffer(self, final : bool) -> None:
		while True:
			match = PROGRESS_PATTERN.search(self.buffer)
			newline_index = self.buffer.find('\n')

			if match and (newline_index < 0 or match.start() < newline_index):
				before = self.buffer[:match.start()]
				if before.strip():
					self.emit_line(before)
				# an unterminated status could still be growing, wait for more output
				if match.end() == len(self.buffer) and not final:
					return
				done = len(match.group('done'))
				total = done + len(match.group('todo'))
				ratio = done / total if total else 0.0
				title = ANSI_PATTERN.sub('', match.group('title')).strip()
				status = ANSI_PATTERN.sub('', match.group('status') or '').strip()
				description = ANSI_PATTERN.sub('', match.group('description') or '').strip()
				self.progress.emit(title, ratio, (status + ('  ·  ' + description if description else '')).strip())
				self.buffer = self.buffer[match.end():]
				continue

			if newline_index >= 0:
				self.emit_line(self.buffer[:newline_index])
				self.buffer = self.buffer[newline_index + 1:]
				continue

			if final and self.buffer.strip():
				self.emit_line(self.buffer)
				self.buffer = ''
			return

	def emit_line(self, text : str) -> None:
		for part in text.replace('\r', '\n').split('\n'):
			line = ANSI_PATTERN.sub('', part).rstrip()
			if line.strip():
				self.log.emit(line)

	def on_finished(self, exit_code : int, exit_status : QProcess.ExitStatus) -> None:
		self.flush_buffer(final = True)
		if self.cancelled:
			exit_code = -1
		elif exit_status == QProcess.ExitStatus.CrashExit:
			exit_code = exit_code or -2
		# release before emitting, listeners may start the next process right away
		self.release_process()
		self.finished.emit(exit_code, time.monotonic() - self.start_time)

	def on_error(self, error : QProcess.ProcessError) -> None:
		if error == QProcess.ProcessError.FailedToStart and self.process:
			program = self.process.program()
			self.release_process()
			self.log.emit('Failed to start: ' + program)
			self.finished.emit(-3, 0.0)

	def release_process(self) -> None:
		if self.process:
			self.process.deleteLater()
			self.process = None


class PreviewWorker(QObject):
	"""keeps preview_worker.py running in the facefusion environment, so a preview after the first one skips loading the models

	it makes one preview at a time for a ProcessRunner. done reports facefusion's exit code, or None when the worker itself
	failed, the runner then makes the preview with facefusion.py. it quits after a while without previews to free the memory
	"""
	log = Signal(str)
	done = Signal(object)

	def __init__(self, parent : Optional[QObject] = None) -> None:
		super().__init__(parent)
		self.process : Optional[QProcess] = None
		self.key : Tuple[str, str] = ('', '')
		self.buffer = ''
		self.request_id = 0
		self.running_id : Optional[int] = None
		self.failures = 0
		self.idle_timer = QTimer(self)
		self.idle_timer.setSingleShot(True)
		self.idle_timer.setInterval(WORKER_IDLE_SECONDS * 1000)
		self.idle_timer.timeout.connect(self.stop)

	def is_running(self) -> bool:
		return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

	def is_usable(self) -> bool:
		return self.failures < WORKER_FAILURE_LIMIT

	def run(self, python_path : str, facefusion_root : str, environment : Optional[QProcessEnvironment], args : List[str]) -> None:
		self.idle_timer.stop()
		if self.key != (python_path, facefusion_root):
			self.stop()
		if not self.is_running():
			self.start(python_path, facefusion_root, environment)
		self.request_id += 1
		self.running_id = self.request_id
		line = json.dumps({ 'id': self.request_id, 'args': args })
		self.process.write((line + '\n').encode('utf-8'))

	def start(self, python_path : str, facefusion_root : str, environment : Optional[QProcessEnvironment]) -> None:
		self.key = (python_path, facefusion_root)
		self.buffer = ''
		self.process = QProcess(self)
		self.process.setProgram(python_path)
		self.process.setArguments([ '-u', os.path.join(APP_DIR, 'preview_worker.py') ])
		self.process.setWorkingDirectory(facefusion_root)
		# facefusion logs to stderr, merged its lines stay in order with the result line
		self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
		if environment is not None:
			self.process.setProcessEnvironment(environment)
		self.process.readyReadStandardOutput.connect(self.on_output)
		self.process.finished.connect(self.on_finished)
		self.process.errorOccurred.connect(self.on_error)
		self.process.start()

	def stop(self) -> None:
		"""quits the worker and frees its models, a running preview ends as failed"""
		self.idle_timer.stop()
		if self.process:
			process, self.process = self.process, None
			process.finished.disconnect(self.on_finished)
			if process.state() != QProcess.ProcessState.NotRunning:
				kill_process_tree(int(process.processId()))
				process.kill()
				process.waitForFinished(2000)
			process.deleteLater()
		if self.running_id is not None:
			self.running_id = None
			self.done.emit(None)

	def on_output(self) -> None:
		self.buffer += bytes(self.process.readAllStandardOutput().data()).decode('utf-8', errors = 'replace')
		while '\n' in self.buffer:
			line, self.buffer = self.buffer.split('\n', 1)
			if line.startswith(WORKER_RESULT_PREFIX):
				self.on_result(line[len(WORKER_RESULT_PREFIX):])
			# what the worker says between previews, such as that it is ready, is nobody's run log
			elif self.running_id is not None:
				self.log.emit(line)

	def on_result(self, text : str) -> None:
		try:
			result : Any = json.loads(text)
		except ValueError:
			return
		if result.get('id') != self.running_id:
			return
		self.running_id = None
		exit_code = result.get('code')
		self.failures = 0 if exit_code is not None else self.failures + 1
		self.idle_timer.start()
		self.done.emit(exit_code)

	def on_error(self, error : QProcess.ProcessError) -> None:
		# a process that never started never finishes either
		if error == QProcess.ProcessError.FailedToStart and self.process:
			self.process.finished.disconnect(self.on_finished)
			self.on_finished()

	def on_finished(self, *_ : Any) -> None:
		self.process.deleteLater()
		self.process = None
		self.idle_timer.stop()
		if self.running_id is not None:
			self.running_id = None
			self.failures += 1
			self.done.emit(None)
