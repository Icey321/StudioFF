import json
import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, QProcess, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ToolTipFilter

from ffstudio.config import APP_DIR
from ffstudio.environment import create_process_environment, kill_process_tree
from ffstudio.session import Session

RESULT_PREFIX = 'METRICS '
GIGABYTE = 1024 * 1024 * 1024
# memory this full shows in orange, a run may slow down or fail
WARN_SHARE = 0.9
WARN_COLOR = '#e8890c'
RESTART_DELAY = 10000
MAXIMUM_RESTARTS = 3


def format_gigabytes(used : int, total : int) -> str:
	return '%.1f / %.0f GB' % (used / GIGABYTE, total / GIGABYTE)


def describe_metrics(metrics : Dict[str, Any]) -> List[Dict[str, Any]]:
	"""the title bar parts: their text, whether they are close to full, and a longer tooltip"""
	parts = []
	for gpu in metrics.get('gpus') or []:
		name = gpu.get('name') or 'Graphics card'
		parts.append({ 'text': 'GPU %d%%' % gpu.get('load', 0), 'warn': False, 'tip': '%s\nBusy %d%% · %d °C' % (name, gpu.get('load', 0), gpu.get('temperature', 0)) })
		used, total = gpu.get('used', 0), gpu.get('total', 0)
		if total:
			parts.append({ 'text': 'VRAM ' + format_gigabytes(used, total), 'warn': used >= total * WARN_SHARE, 'tip': '%s memory in use, by every program\nFaceFusion needs more of it for larger models and pixel boost' % name })
	parts.append({ 'text': 'CPU %d%%' % round(metrics.get('cpu', 0)), 'warn': False, 'tip': 'Processor busy, all cores together' })
	used, total = metrics.get('ram_used', 0), metrics.get('ram_total', 0)
	if total:
		parts.append({ 'text': 'RAM ' + format_gigabytes(used, total), 'warn': used >= total * WARN_SHARE, 'tip': 'System memory in use, by every program' })
	return parts


class SystemMonitor(QObject):
	"""keeps system_monitor.py running in the facefusion environment, it reads the cards with facefusion's own libraries"""
	metrics = Signal(dict)
	stopped = Signal()

	def __init__(self, session : Session, parent : Optional[QObject] = None) -> None:
		super().__init__(parent)
		self.session = session
		self.process : Optional[QProcess] = None
		self.buffer = ''
		self.restarts = 0
		self.wanted = False
		self.restart_timer = QTimer(self)
		self.restart_timer.setSingleShot(True)
		self.restart_timer.timeout.connect(self.start)

	def is_running(self) -> bool:
		return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

	def start(self) -> None:
		self.wanted = True
		if self.is_running() or not self.session.is_configured():
			return
		self.buffer = ''
		self.process = QProcess(self)
		self.process.setProgram(self.session.python_path)
		self.process.setArguments([ '-u', os.path.join(APP_DIR, 'system_monitor.py'), '1' ])
		self.process.setWorkingDirectory(self.session.facefusion_root)
		self.process.setProcessEnvironment(create_process_environment(self.session.python_path))
		self.process.readyReadStandardOutput.connect(self.on_output)
		self.process.finished.connect(self.on_finished)
		self.process.start()

	def stop(self) -> None:
		self.wanted = False
		self.restart_timer.stop()
		if self.process:
			process, self.process = self.process, None
			process.finished.disconnect(self.on_finished)
			if process.state() != QProcess.ProcessState.NotRunning:
				kill_process_tree(int(process.processId()))
				process.kill()
				process.waitForFinished(2000)
			process.deleteLater()
		self.stopped.emit()

	def restart(self) -> None:
		"""after the facefusion folder or python changed"""
		if self.wanted:
			self.stop()
			self.restarts = 0
			self.start()

	def on_output(self) -> None:
		if not self.process:
			return
		self.buffer += bytes(self.process.readAllStandardOutput().data()).decode('utf-8', errors = 'replace')
		while '\n' in self.buffer:
			line, self.buffer = self.buffer.split('\n', 1)
			if line.startswith(RESULT_PREFIX):
				try:
					self.metrics.emit(json.loads(line[len(RESULT_PREFIX):]))
				except ValueError:
					continue
				self.restarts = 0

	def on_finished(self, *_ : Any) -> None:
		if self.process:
			self.process.deleteLater()
			self.process = None
		self.stopped.emit()
		# a crash is retried a few times, then the bar stays empty until the next start
		if self.wanted and self.restarts < MAXIMUM_RESTARTS:
			self.restarts += 1
			self.restart_timer.start(RESTART_DELAY)


class MonitorBar(QWidget):
	"""graphics card, processor and memory use in the title bar, once a second"""

	def __init__(self, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.row = QHBoxLayout(self)
		self.row.setContentsMargins(0, 0, 0, 0)
		self.row.setSpacing(14)
		self.labels : List[CaptionLabel] = []

	def show_metrics(self, metrics : Dict[str, Any]) -> None:
		parts = describe_metrics(metrics)
		while len(self.labels) < len(parts):
			label = CaptionLabel(self)
			label.installEventFilter(ToolTipFilter(label, 300))
			self.row.addWidget(label)
			self.labels.append(label)
		for label, part in zip(self.labels, parts):
			label.setText(part.get('text'))
			label.setToolTip(part.get('tip'))
			if part.get('warn'):
				label.setTextColor(WARN_COLOR, WARN_COLOR)
			else:
				label.setTextColor('#808080', '#9a9a9a')
			label.show()
		for label in self.labels[len(parts):]:
			label.hide()

	def clear(self) -> None:
		for label in self.labels:
			label.hide()
