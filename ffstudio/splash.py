import json
import os
import time
from typing import List, Optional

from PySide6.QtCore import QEventLoop, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from ffstudio import APP_NAME, APP_VERSION
from ffstudio.config import APP_DIR, AppConfig, DATA_DIR

# when each step finished on the last start, so the bar fills at the real pace this time
STARTUP_FILE = os.path.join(DATA_DIR, 'startup.json')
DEFAULT_STEPS = 420
DEFAULT_ACCENT = '#7c5cff'
# repainting on every step would slow the start down, a few times a second is smooth enough
REPAINT_INTERVAL = 0.03


class Splash(QWidget):
	"""plain qt on purpose, it shows before the fluent widgets library has loaded"""

	def __init__(self) -> None:
		super().__init__(None, Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint)
		self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
		self.setFixedSize(460, 210)
		config = AppConfig()
		theme = config.get('theme') or 'auto'
		self.dark = theme == 'dark' or theme == 'auto' and QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
		self.accent = QColor((config.get('accent_colors') or {}).get('dark' if self.dark else 'light') or DEFAULT_ACCENT)
		self.icon = QPixmap(os.path.join(APP_DIR, 'assets', 'icon.png'))
		self.status = 'Starting…'
		self.started = time.perf_counter()
		self.times : List[float] = []
		self.last_marks = load_marks()
		self.last_repaint = 0.0
		screen = QGuiApplication.primaryScreen()
		if screen:
			geometry = screen.availableGeometry()
			self.move(geometry.x() + (geometry.width() - self.width()) // 2, geometry.y() + (geometry.height() - self.height()) // 2)

	def progress(self) -> float:
		steps = len(self.times)
		if len(self.last_marks) > steps > 0:
			done = self.last_marks[steps - 1]
		else:
			# first start, or the options changed since: count the steps instead
			done = steps / max(len(self.last_marks), DEFAULT_STEPS)
		# never shows full before the window is really up
		return min(done, 0.98)

	def step(self, status : Optional[str] = None) -> None:
		now = time.perf_counter()
		self.times.append(now - self.started)
		if status:
			self.status = status
		if status or now - self.last_repaint >= REPAINT_INTERVAL:
			self.last_repaint = now
			self.update()
			# keeps the splash painted and windows from calling the app not responding, clicks wait until the window is up
			QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

	def paintEvent(self, event) -> None:
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		background = QColor('#202020') if self.dark else QColor('#f9f9f9')
		border = QColor('#3a3a3a') if self.dark else QColor('#d9d9d9')
		text = QColor('#ffffff') if self.dark else QColor('#1a1a1a')
		dim = QColor('#a0a0a0') if self.dark else QColor('#606060')
		track = QColor('#3a3a3a') if self.dark else QColor('#e0e0e0')
		frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
		painter.setPen(border)
		painter.setBrush(background)
		painter.drawRoundedRect(frame, 10, 10)

		if not self.icon.isNull():
			painter.drawPixmap(32, 40, 72, 72, self.icon)
		# not the variable display face, windows takes a second to load that one the first time
		title_font = QFont('Segoe UI', 20)
		title_font.setWeight(QFont.Weight.DemiBold)
		painter.setFont(title_font)
		painter.setPen(text)
		painter.drawText(QRectF(124, 42, 320, 40), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, APP_NAME)
		painter.setFont(QFont('Segoe UI', 10))
		painter.setPen(dim)
		painter.drawText(QRectF(125, 82, 320, 24), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, 'Version ' + APP_VERSION)

		painter.drawText(QRectF(32, 136, 340, 22), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.status)
		painter.drawText(QRectF(372, 136, 56, 22), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, '%d%%' % round(self.progress() * 100))
		bar = QRectF(32, 166, self.width() - 64, 6)
		painter.setPen(Qt.PenStyle.NoPen)
		painter.setBrush(track)
		painter.drawRoundedRect(bar, 3, 3)
		filled = QPainterPath()
		filled.addRoundedRect(QRectF(bar.x(), bar.y(), max(bar.height(), bar.width() * self.progress()), bar.height()), 3, 3)
		painter.fillPath(filled, self.accent)

	def finish(self) -> None:
		# the window's first paint counts as the last step
		self.times.append(time.perf_counter() - self.started)
		save_marks(self.times)
		self.close()
		self.deleteLater()


def load_marks() -> List[float]:
	try:
		with open(STARTUP_FILE, encoding = 'utf-8') as startup_file:
			return [ float(mark) for mark in json.load(startup_file).get('marks') ]
	except (OSError, ValueError, TypeError, AttributeError):
		return []


def save_marks(times : List[float]) -> None:
	"""keeps each step as a share of the whole start"""
	if not times:
		return
	total = max(times[-1], 0.001)
	try:
		os.makedirs(DATA_DIR, exist_ok = True)
		with open(STARTUP_FILE, 'w', encoding = 'utf-8') as startup_file:
			json.dump({ 'seconds': round(total, 2), 'marks': [ round(value / total, 4) for value in times ] }, startup_file)
	except OSError:
		pass


SPLASH : Optional[Splash] = None


def show() -> None:
	global SPLASH
	SPLASH = Splash()
	SPLASH.show()
	SPLASH.repaint()
	QApplication.processEvents()


def step(status : Optional[str] = None) -> None:
	"""one unit of startup work done, does nothing once the app is up"""
	if SPLASH:
		SPLASH.step(status)


def finish() -> None:
	global SPLASH
	if SPLASH:
		SPLASH.finish()
		SPLASH = None
