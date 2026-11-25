"""the benchmark page's live panel (speed dial, a tile per resolution, swapped frames) and its results chart"""
import math
import os
from typing import Any, Dict, List, Optional

import cv2
from PySide6.QtCore import Property, QEasingCurve, QPointF, QPropertyAnimation, QRectF, QThread, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget
from qfluentwidgets import isDarkTheme, themeColor

from ffstudio.benchmark_report import CHART_COLORS, REAL_TIME_FPS, chart_height, color, draw_chart, make_font

GRAB_COUNT = 12
GRAB_WIDTH = 640


def colors() -> Dict[str, Any]:
	return CHART_COLORS.get('dark' if isDarkTheme() else 'light')


def rounded_clip(painter : QPainter, rect : QRectF, radius : float) -> None:
	path = QPainterPath()
	path.addRoundedRect(rect, radius, radius)
	painter.setClipPath(path)


class SpeedDial(QWidget):
	"""a half circle gauge, the needle eases to each new value, the tick marks real time (30 fps)"""

	def __init__(self, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.setFixedSize(210, 160)
		self._shown = 0.0
		self.value : Optional[float] = None
		self.top = 60.0
		self.caption = 'fps while processing frames'
		self.animation = QPropertyAnimation(self, b'shown', self)
		self.animation.setDuration(600)
		self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)

	def get_shown(self) -> float:
		return self._shown

	def set_shown(self, value : float) -> None:
		self._shown = value
		self.update()

	shown = Property(float, get_shown, set_shown)

	def reset(self) -> None:
		self.animation.stop()
		self.value = None
		self.top = 60.0
		self._shown = 0.0
		self.update()

	def set_value(self, value : Optional[float]) -> None:
		self.value = value
		if value is not None:
			# the scale only grows during a run, in steps of 30 so the real time tick stays readable
			while value > self.top * 0.92:
				self.top += 30
		self.animation.stop()
		self.animation.setStartValue(self._shown)
		self.animation.setEndValue(float(value or 0))
		self.animation.start()

	def paintEvent(self, event) -> None:
		palette = colors()
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		center = QPointF(self.width() / 2, 104)
		radius = 86.0
		arc = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)
		pen = QPen(color('#3a3a3a' if isDarkTheme() else '#e3e3e3'), 16, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
		painter.setPen(pen)
		painter.drawArc(arc, 0, 180 * 16)
		share = max(0.0, min(1.0, self._shown / self.top))
		if share > 0:
			pen.setColor(themeColor())
			painter.setPen(pen)
			painter.drawArc(arc, 180 * 16, int(-180 * 16 * share))
		# real time tick
		tick = math.pi * (1 - REAL_TIME_FPS / self.top)
		painter.setPen(QPen(color(palette.get('real_time')), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
		painter.drawLine(QPointF(center.x() + math.cos(tick) * (radius - 13), center.y() - math.sin(tick) * (radius - 13)), QPointF(center.x() + math.cos(tick) * (radius + 13), center.y() - math.sin(tick) * (radius + 13)))
		angle = math.pi * (1 - share)
		needle = color(palette.get('text'))
		painter.setPen(QPen(needle, 3.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
		painter.drawLine(center, QPointF(center.x() + math.cos(angle) * (radius - 20), center.y() - math.sin(angle) * (radius - 20)))
		painter.setPen(Qt.PenStyle.NoPen)
		painter.setBrush(needle)
		painter.drawEllipse(center, 6, 6)
		painter.setPen(needle)
		painter.setFont(make_font(21, True))
		painter.drawText(QRectF(0, 112, self.width(), 30), int(Qt.AlignmentFlag.AlignCenter), '—' if self.value is None else '%.1f' % self._shown)
		painter.setFont(make_font(8.5))
		painter.setPen(color(palette.get('muted')))
		painter.drawText(QRectF(0, 140, self.width(), 18), int(Qt.AlignmentFlag.AlignCenter), self.caption)


class ResolutionTile(QWidget):
	"""one resolution of the run: waiting, running (with its own progress), estimated or final"""

	def __init__(self, resolution : str, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.resolution = resolution
		self.state = 'waiting'
		self.fps : Optional[float] = None
		self.detail = 'waiting'
		self.ratio = 0.0
		self.setFixedHeight(84)
		self.setMinimumWidth(110)
		self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

	def set_state(self, state : str, detail : str = '', fps : Optional[float] = None, ratio : float = 0.0) -> None:
		self.state = state
		self.detail = detail
		self.fps = fps if fps is not None else self.fps
		self.ratio = ratio
		self.update()

	def paintEvent(self, event) -> None:
		dark = isDarkTheme()
		palette = colors()
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
		done = self.state in ('estimate', 'final')
		fill = color('#173a2a' if dark else '#dff3e5') if done else color('#ffffff0d' if dark else '#00000008')
		edge = color('#2fa86b') if done else themeColor() if self.state == 'running' else color('#ffffff22' if dark else '#00000022')
		painter.setPen(QPen(edge, 2 if self.state == 'running' else 1))
		painter.setBrush(fill)
		painter.drawRoundedRect(rect, 8, 8)
		painter.setPen(color(palette.get('text')))
		painter.setFont(make_font(10.5, True))
		painter.drawText(QRectF(rect.left() + 12, rect.top() + 8, rect.width() - 24, 18), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.resolution)
		if done and self.fps:
			value = ('≈ ' if self.state == 'estimate' else '') + '%.1f' % self.fps
			font = make_font(15, True)
			painter.setFont(font)
			painter.drawText(QRectF(rect.left() + 12, rect.top() + 28, rect.width() - 24, 26), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), value)
			painter.setFont(make_font(8.5))
			painter.setPen(color(palette.get('muted')))
			painter.drawText(QRectF(rect.left() + 18 + QFontMetricsF(font).horizontalAdvance(value), rect.top() + 34, 40, 18), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), 'fps')
		painter.setFont(make_font(8.5))
		painter.setPen(color(palette.get('muted')))
		detail_top = rect.top() + (56 if done else 30)
		painter.drawText(QRectF(rect.left() + 12, detail_top, rect.width() - 24, 18), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.detail)
		if self.state == 'running':
			track = QRectF(rect.left() + 12, rect.bottom() - 18, rect.width() - 24, 6)
			painter.setPen(Qt.PenStyle.NoPen)
			painter.setBrush(color('#ffffff1a' if dark else '#0000001a'))
			painter.drawRoundedRect(track, 3, 3)
			painter.setBrush(themeColor())
			painter.drawRoundedRect(QRectF(track.left(), track.top(), max(6.0, track.width() * min(1.0, self.ratio)), track.height()), 3, 3)


class FrameView(QWidget):
	"""a picture with a corner badge and a caption strip, scaled to fill a 16:9 box"""

	def __init__(self, parent : Optional[QWidget] = None, square : bool = False) -> None:
		super().__init__(parent)
		self.pixmap = QPixmap()
		self.badge = ''
		self.badge_color = color('#e5484d')
		self.caption = ''
		self.square = square
		if square:
			self.setFixedSize(132, 132)
		else:
			self.setFixedSize(round(224 * 16 / 9), 224)

	def set_frame(self, pixmap : QPixmap, badge : str = '', caption : str = '', badge_color : str = '#e5484d') -> None:
		self.pixmap = pixmap
		self.badge = badge
		self.caption = caption
		self.badge_color = QColor(badge_color)
		self.update()

	def paintEvent(self, event) -> None:
		dark = isDarkTheme()
		painter = QPainter(self)
		painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
		rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
		if not self.square:
			# keep 16:9, centered
			width = min(rect.width(), rect.height() * 16 / 9)
			rect = QRectF(rect.left(), rect.top(), width, width * 9 / 16)
		painter.save()
		rounded_clip(painter, rect, 8)
		painter.fillRect(rect, color('#111111' if dark else '#e8e8e8'))
		if not self.pixmap.isNull():
			scaled = self.pixmap.scaled(rect.size().toSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
			painter.drawPixmap(QPointF(rect.center().x() - scaled.width() / 2, rect.center().y() - scaled.height() / 2), scaled)
		else:
			painter.setPen(color('#9aa1ad'))
			painter.setFont(make_font(9))
			painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap), 'The test video shows here once\nFaceFusion has downloaded it.' if not self.square else 'Test face')
		if self.caption:
			font = make_font(8.5)
			strip = QRectF(rect.left() + 8, rect.bottom() - 26, min(rect.width() - 16, QFontMetricsF(font).horizontalAdvance(self.caption) + 16), 20)
			painter.setPen(Qt.PenStyle.NoPen)
			painter.setBrush(QColor(0, 0, 0, 170))
			painter.drawRoundedRect(strip, 4, 4)
			painter.setPen(color('#ffffff'))
			painter.setFont(font)
			painter.drawText(strip, int(Qt.AlignmentFlag.AlignCenter), self.caption)
		if self.badge:
			font = make_font(8, True)
			badge = QRectF(rect.left() + 8, rect.top() + 8, QFontMetricsF(font).horizontalAdvance(self.badge) + 16, 19)
			painter.setPen(Qt.PenStyle.NoPen)
			painter.setBrush(self.badge_color)
			painter.drawRoundedRect(badge, 4, 4)
			painter.setPen(color('#ffffff'))
			painter.setFont(font)
			painter.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), self.badge)
		painter.restore()
		if not self.square:
			painter.setPen(QPen(themeColor() if self.badge == '● SWAPPED' else color('#ffffff22' if dark else '#00000022'), 1.5))
			painter.setBrush(Qt.BrushStyle.NoBrush)
			painter.drawRoundedRect(rect, 8, 8)


class ResultsChart(QWidget):
	"""the bar chart of one run"""

	def __init__(self, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.rows : List[Dict[str, Any]] = []
		self.ghost : Dict[str, float] = {}
		self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
		self.setMinimumWidth(360)
		self.setFixedHeight(0)

	def set_run(self, rows : List[Dict[str, Any]], ghost : Dict[str, float]) -> None:
		self.rows = rows
		self.ghost = ghost
		self.setFixedHeight(int(chart_height(len(rows)) + 4) if rows else 0)
		self.update()

	def paintEvent(self, event) -> None:
		if not self.rows:
			return
		painter = QPainter(self)
		palette = dict(colors())
		draw_chart(painter, 0, 0, self.width(), self.rows, self.ghost, palette)


def to_image(frame) -> QImage:
	rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
	height, width = rgb.shape[:2]
	return QImage(rgb.data, width, height, width * 3, QImage.Format.Format_RGB888).copy()


class FrameGrabber(QThread):
	"""reads a few frames spread over a finished benchmark video, small, on one thread so FaceFusion keeps the rest"""
	grabbed = Signal(str, list, int)

	def __init__(self, path : str, resolution : str, count : int = GRAB_COUNT, parent = None) -> None:
		super().__init__(parent)
		self.path = path
		self.resolution = resolution
		self.count = count
		self.seconds = 0.0

	def run(self) -> None:
		import time
		started = time.perf_counter()
		images : List[QImage] = []
		total = 0
		if os.path.isfile(self.path):
			try:
				capture = cv2.VideoCapture(self.path, cv2.CAP_FFMPEG, [ cv2.CAP_PROP_N_THREADS, 1 ])
				total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) if capture.isOpened() else 0
				step = max(1, total // self.count) if total else 1
				index = 0
				while capture.isOpened() and len(images) < self.count:
					if not capture.grab():
						break
					if index % step == step // 2:
						success, frame = capture.retrieve()
						if success:
							height, width = frame.shape[:2]
							if width > GRAB_WIDTH:
								frame = cv2.resize(frame, (GRAB_WIDTH, round(height * GRAB_WIDTH / width)), interpolation = cv2.INTER_AREA)
							images.append(to_image(frame))
					index += 1
				capture.release()
			except Exception:
				images = []
		self.seconds = time.perf_counter() - started
		self.grabbed.emit(self.resolution, images, total)


def read_still(path : str, frame_index : int = 40) -> QPixmap:
	"""one frame of a video, or a picture, as a pixmap, empty when it cannot be read"""
	if not os.path.isfile(path):
		return QPixmap()
	if not path.lower().endswith('.mp4'):
		return QPixmap(path)
	capture = cv2.VideoCapture(path)
	capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
	success, frame = capture.read()
	capture.release()
	if not success:
		return QPixmap()
	height, width = frame.shape[:2]
	if width > GRAB_WIDTH:
		frame = cv2.resize(frame, (GRAB_WIDTH, round(height * GRAB_WIDTH / width)), interpolation = cv2.INTER_AREA)
	return QPixmap.fromImage(to_image(frame))


def video_frame_total(path : str) -> int:
	capture = cv2.VideoCapture(path)
	total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) if capture.isOpened() else 0
	capture.release()
	return total
