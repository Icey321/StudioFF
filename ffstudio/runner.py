import re
import subprocess
import time
from typing import List, Optional

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from ffstudio.environment import kill_process_tree

ANSI_PATTERN = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
# facefusion draws its bar as <active color>====<neutral color>====<reset>
PROGRESS_PATTERN = re.compile(r'\x1b\[G(?P<title>.*?)\s*\x1b\[94m(?P<done>=*)\x1b\[90m(?P<todo>=*)\x1b\[0m\s*(?P<status>[^|\x1b]*)(?:\|\s*(?P<description>[^\x1b]*))?')


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

	def is_running(self) -> bool:
		return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

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

	def stop(self) -> None:
		if self.is_running():
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
