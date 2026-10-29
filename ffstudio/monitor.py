import json
import os
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QObject, QProcess, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ToolTipFilter

from ffstudio.config import APP_DIR
from ffstudio.drive import format_size, output_drives
from ffstudio.environment import create_process_environment, kill_process_tree
from ffstudio.session import Session

RESULT_PREFIX = 'METRICS '
GIGABYTE = 1024 * 1024 * 1024
# memory this full shows in orange, a run may slow down or fail
WARN_SHARE = 0.9
WARN_COLOR = '#e8890c'
METER_HEIGHT = 3
# share of full, and the meter color there
METER_STOPS = [ (0.0, (46, 160, 67)), (0.5, (46, 160, 67)), (0.75, (232, 137, 12)), (1.0, (220, 38, 38)) ]
RESTART_DELAY = 10000
MAXIMUM_RESTARTS = 3


def format_gigabytes(used : int, total : int) -> str:
	return '%.1f / %.0f GB' % (used / GIGABYTE, total / GIGABYTE)


def describe_metrics(metrics : Dict[str, Any]) -> List[Dict[str, Any]]:
	"""the title bar parts: their text, whether they are close to full, and a longer tooltip"""
	parts = []
	for gpu in metrics.get('gpus') or []:
		name = gpu.get('name') or 'Graphics card'
		parts.append({ 'text': 'GPU %d%%' % gpu.get('load', 0), 'warn': gpu.get('load', 0) >= 100 * WARN_SHARE, 'share': gpu.get('load', 0) / 100, 'tip': '%s\nBusy %d%% · %d °C' % (name, gpu.get('load', 0), gpu.get('temperature', 0)) })
		used, total = gpu.get('used', 0), gpu.get('total', 0)
		if total:
			parts.append({ 'text': 'VRAM ' + format_gigabytes(used, total), 'warn': used >= total * WARN_SHARE, 'share': used / total, 'tip': '%s memory in use, by every program\nFaceFusion needs more of it for larger models and pixel boost' % name })
	parts.append({ 'text': 'CPU %d%%' % round(metrics.get('cpu', 0)), 'warn': metrics.get('cpu', 0) >= 100 * WARN_SHARE, 'share': metrics.get('cpu', 0) / 100, 'tip': 'Processor busy, all cores together' })
	used, total = metrics.get('ram_used', 0), metrics.get('ram_total', 0)
	if total:
		parts.append({ 'text': 'RAM ' + format_gigabytes(used, total), 'warn': used >= total * WARN_SHARE, 'share': used / total, 'tip': 'System memory in use, by every program' })
	for part in parts:
		part['share'] = min(max(part.get('share', 0), 0.0), 1.0)
	return parts


def describe_drives(folders : List[str]) -> List[Dict[str, Any]]:
	"""the title bar parts for the drives the output folders are on, the meter fills as the drive fills"""
	parts = []
	for drive in output_drives(folders):
		tip = 'Drive %s  %s free of %s\nAn output folder is on this drive' % (drive.get('drive'), format_size(drive.get('free')), format_size(drive.get('total')))
		if drive.get('warn'):
			tip += '\nGetting full, a long video may not fit'
		parts.append({ 'text': '%s %s free' % (drive.get('drive'), format_size(drive.get('free'))), 'warn': drive.get('warn'), 'share': drive.get('share'), 'tip': tip })
	return parts


def meter_color(share : float) -> QColor:
	"""green while there is room, fading through orange to red as it reaches full"""
	for (start, start_color), (end, end_color) in zip(METER_STOPS, METER_STOPS[1:]):
		if share <= end:
			mix = max(share - start, 0.0) / (end - start)
			return QColor(*[ round(a + (b - a) * mix) for a, b in zip(start_color, end_color) ])
	return QColor(*METER_STOPS[-1][1])


class UsageMeter(QWidget):
	"""a thin bar under a title bar reading, filled to its share of 100%"""

	def __init__(self, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.share = 0.0
		self.setFixedHeight(METER_HEIGHT)
		self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

	def set_share(self, share : float) -> None:
		if share != self.share:
			self.share = share
			self.update()

	def paintEvent(self, _ : Any) -> None:
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		painter.setPen(Qt.PenStyle.NoPen)
		radius = self.height() / 2
		painter.setBrush(QColor(128, 128, 128, 60))
		painter.drawRoundedRect(QRectF(self.rect()), radius, radius)
		if self.share > 0:
			painter.setBrush(meter_color(self.share))
			painter.drawRoundedRect(QRectF(0, 0, max(self.width() * self.share, self.height()), self.height()), radius, radius)


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
		self.readings : List[QWidget] = []
		self.labels : List[CaptionLabel] = []
		self.meters : List[UsageMeter] = []
		# the output folders of the pages, their drives show after the memory
		self.drive_folders : Callable[[], List[str]] = lambda: []

	def show_metrics(self, metrics : Dict[str, Any]) -> None:
		parts = describe_metrics(metrics) + describe_drives(self.drive_folders())
		while len(self.labels) < len(parts):
			reading = QWidget(self)
			column = QVBoxLayout(reading)
			column.setContentsMargins(0, 0, 0, 0)
			column.setSpacing(2)
			label = CaptionLabel(reading)
			label.installEventFilter(ToolTipFilter(label, 300))
			meter = UsageMeter(reading)
			column.addWidget(label)
			column.addWidget(meter)
			self.row.addWidget(reading)
			self.readings.append(reading)
			self.labels.append(label)
			self.meters.append(meter)
		for reading, label, meter, part in zip(self.readings, self.labels, self.meters, parts):
			label.setText(part.get('text'))
			label.setToolTip(part.get('tip'))
			if part.get('warn'):
				label.setTextColor(WARN_COLOR, WARN_COLOR)
			else:
				label.setTextColor('#808080', '#9a9a9a')
			meter.set_share(part.get('share'))
			reading.show()
			# only ever widens, so the meter keeps its length as the numbers change
			reading.setMinimumWidth(max(reading.minimumWidth(), reading.sizeHint().width()))
		for reading in self.readings[len(parts):]:
			reading.hide()

	def clear(self) -> None:
		for reading in self.readings:
			reading.hide()
