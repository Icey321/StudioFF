import os
from typing import Dict, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import QApplication
from qfluentwidgets import CaptionLabel, DotInfoBadge, FluentIcon, FluentWindow, InfoBar, InfoBarPosition, NavigationItemPosition, Theme, isDarkTheme, setTheme, setThemeColor

from ffstudio import APP_NAME, APP_VERSION, splash
from ffstudio.config import APP_DIR
from ffstudio.session import Session

THEMES = { 'auto': Theme.AUTO, 'light': Theme.LIGHT, 'dark': Theme.DARK }
DEFAULT_ACCENT = '#7c5cff'
UPDATE_DOT_COLOR = QColor('#ff8c00')


def apply_theme(name : str, accents : Optional[Dict[str, str]] = None) -> None:
	"""switches the theme, and the accent color to the one picked for the light or dark look it ends up in"""
	setTheme(THEMES.get(name, Theme.AUTO))
	accents = accents or {}
	setThemeColor(accents.get('dark' if isDarkTheme() else 'light') or DEFAULT_ACCENT)


class MainWindow(FluentWindow):
	def __init__(self, session : Session) -> None:
		super().__init__()
		self.session = session
		from ffstudio.pages.batch import BatchPage
		from ffstudio.pages.benchmark import BenchmarkPage
		from ffstudio.pages.console import ConsolePage
		from ffstudio.pages.models import ModelsPage
		from ffstudio.pages.setup import SetupPage
		from ffstudio.pages.studio import StudioPage

		splash.step('Building the Studio page…')
		self.studio_page = StudioPage(session, self)
		splash.step('Building the Batch page…')
		self.batch_page = BatchPage(session, self)
		splash.step('Building the Models page…')
		self.models_page = ModelsPage(session, self)
		splash.step('Building the Benchmark page…')
		self.benchmark_page = BenchmarkPage(session, self)
		splash.step('Building the Console page…')
		self.console_page = ConsolePage(session, self)
		splash.step('Building the Setup page…')
		self.setup_page = SetupPage(session, self)
		splash.step('Opening the window…')

		self.addSubInterface(self.studio_page, FluentIcon.PHOTO, 'Studio')
		self.addSubInterface(self.batch_page, FluentIcon.LIBRARY, 'Batch')
		self.addSubInterface(self.models_page, FluentIcon.CLOUD_DOWNLOAD, 'Models')
		self.addSubInterface(self.benchmark_page, FluentIcon.SPEED_HIGH, 'Benchmark')
		self.addSubInterface(self.console_page, FluentIcon.COMMAND_PROMPT, 'Console')
		self.addSubInterface(self.setup_page, FluentIcon.SETTING, 'Setup', NavigationItemPosition.BOTTOM)

		# just wide enough for the longest page name
		self.navigationInterface.setExpandWidth(150)
		self.setWindowTitle(APP_NAME)
		# the version sits right after the name in the title bar, a little dimmer
		self.version_label = CaptionLabel('v' + APP_VERSION, self.titleBar)
		self.version_label.setTextColor('#808080', '#9a9a9a')
		self.titleBar.hBoxLayout.insertSpacing(2, 8)
		self.titleBar.hBoxLayout.insertWidget(3, self.version_label, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
		# orange dot on the Setup gear while a newer FaceFusion Studio release is out
		setup_item = self.navigationInterface.widget(self.setup_page.objectName())
		self.update_dot = DotInfoBadge.custom(UPDATE_DOT_COLOR, UPDATE_DOT_COLOR, setup_item)
		self.update_dot.setFixedSize(8, 8)
		self.update_dot.move(24, 7)
		self.update_dot.setToolTip('A FaceFusion Studio update is available')
		self.update_dot.hide()
		self.setup_page.studio_update_found.connect(self.update_dot.setVisible)
		icon_path = os.path.join(APP_DIR, 'assets', 'icon.png')
		if os.path.exists(icon_path):
			self.setWindowIcon(QIcon(icon_path))
		self.resize(1500, 960)
		self.setMinimumSize(1100, 720)
		self.center()

		session.notify.connect(self.show_notification)
		session.schema_loading.connect(self.on_schema_loading)
		QTimer.singleShot(300, self.startup)

	def center(self) -> None:
		screen = QApplication.primaryScreen()
		if screen:
			geometry = screen.availableGeometry()
			self.move(geometry.x() + (geometry.width() - self.width()) // 2, geometry.y() + (geometry.height() - self.height()) // 2)

	def startup(self) -> None:
		from ffstudio.updater import read_update_result
		update_result = read_update_result()
		if update_result and update_result[0]:
			self.show_notification('success', 'FaceFusion Studio updated', 'You are now on %s.' % update_result[1])
		elif update_result:
			self.show_notification('error', 'The update did not install', '%s. FaceFusion Studio stayed on v%s.' % (update_result[1][:1].upper() + update_result[1][1:].rstrip('.'), APP_VERSION))
		self.setup_page.check_studio_update(quiet = True)
		if not self.session.is_configured():
			self.switchTo(self.setup_page)
			self.show_notification('info', 'Welcome', 'Point the app at your FaceFusion v4 folder, or install v4 from here.')
			return
		self.session.check_python()
		self.setup_page.refresh_status()
		self.session.refresh_schema()

	def on_schema_loading(self, loading : bool) -> None:
		if loading:
			self.show_notification('info', 'Reading FaceFusion options…', 'This takes a few seconds.')

	def show_notification(self, level : str, title : str, content : str) -> None:
		factory = { 'success': InfoBar.success, 'warning': InfoBar.warning, 'error': InfoBar.error }.get(level, InfoBar.info)
		factory(title, content, orient = Qt.Orientation.Horizontal, isClosable = True, position = InfoBarPosition.TOP_RIGHT, duration = 8000 if level == 'error' else 4000, parent = self)

	def closeEvent(self, event) -> None:
		if self.session.runner.is_running():
			from qfluentwidgets import MessageBox
			box = MessageBox('FaceFusion is still running', 'Stop "%s" and quit?' % self.session.task, self)
			if not box.exec():
				event.ignore()
				return
			self.session.runner.stop()
			self.session.clear_facefusion_temp()
		self.studio_page.release_result_reader()
		self.studio_page.face_probe.stop()
		self.session.preview_worker.stop()
		self.session.clear_preview_files()
		self.session.save()
		super().closeEvent(event)
