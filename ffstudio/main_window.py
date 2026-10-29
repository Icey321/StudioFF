import os
from typing import Dict, Optional

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
from qfluentwidgets import CaptionLabel, DotInfoBadge, FluentIcon, FluentWindow, InfoBar, InfoBarPosition, NavigationItemPosition, Theme, ToolTipFilter, TransparentPushButton, isDarkTheme, setTheme, setThemeColor

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
		self.tray : Optional[QSystemTrayIcon] = None
		self.quitting = False
		# small steps through the slow imports, so the bar keeps moving instead of stopping at 10%
		splash.step()
		from ffstudio.pages.batch import BatchPage
		splash.step()
		from ffstudio.pages.benchmark import BenchmarkPage
		splash.step()
		from ffstudio.pages.console import ConsolePage
		splash.step()
		from ffstudio.pages.models import ModelsPage
		splash.step()
		from ffstudio.pages.setup import SetupPage
		splash.step()
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
		splash.step('Building the Settings page…')
		self.setup_page = SetupPage(session, self)
		splash.step('Opening the window…')

		self.addSubInterface(self.studio_page, FluentIcon.PHOTO, 'Studio')
		self.addSubInterface(self.batch_page, FluentIcon.LIBRARY, 'Batch')
		self.addSubInterface(self.models_page, FluentIcon.CLOUD_DOWNLOAD, 'Models')
		self.addSubInterface(self.benchmark_page, FluentIcon.SPEED_HIGH, 'Benchmark')
		self.addSubInterface(self.console_page, FluentIcon.COMMAND_PROMPT, 'Console')
		self.addSubInterface(self.setup_page, FluentIcon.SETTING, 'Settings', NavigationItemPosition.BOTTOM)
		splash.step()

		# just wide enough for the longest page name
		self.navigationInterface.setExpandWidth(150)
		self.setWindowTitle(APP_NAME)
		# the version sits right after the name in the title bar, a little dimmer
		self.version_label = CaptionLabel('v' + APP_VERSION, self.titleBar)
		self.version_label.setTextColor('#808080', '#9a9a9a')
		self.titleBar.hBoxLayout.insertSpacing(2, 8)
		self.titleBar.hBoxLayout.insertWidget(3, self.version_label, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
		# and after it, while a newer release is out, the button that installs it
		self.update_button = TransparentPushButton(FluentIcon.DOWNLOAD, '', self.titleBar)
		self.update_button.setFixedHeight(26)
		self.update_button.setToolTip('Download the new StudioFF and install it, your settings and presets stay as they are')
		self.update_button.installEventFilter(ToolTipFilter(self.update_button))
		self.update_button.clicked.connect(self.update_studio_now)
		self.update_button.hide()
		self.titleBar.hBoxLayout.insertSpacing(4, 10)
		self.titleBar.hBoxLayout.insertWidget(5, self.update_button, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
		# graphics card, processor and memory use, left of the window buttons
		from ffstudio.monitor import MonitorBar, SystemMonitor
		self.monitor_bar = MonitorBar(self.titleBar)
		# the window buttons sit in the title bar's last layout
		buttons_index = self.titleBar.hBoxLayout.count() - 1
		self.titleBar.hBoxLayout.insertWidget(buttons_index, self.monitor_bar, 0, Qt.AlignmentFlag.AlignVCenter)
		self.titleBar.hBoxLayout.insertSpacing(buttons_index + 1, 18)
		self.system_monitor = SystemMonitor(session, self)
		self.system_monitor.metrics.connect(self.monitor_bar.show_metrics)
		self.system_monitor.stopped.connect(self.monitor_bar.clear)
		session.schema_changed.connect(self.system_monitor.restart)
		self.setup_page.monitor_toggled.connect(self.set_monitor_shown)
		# orange dot on the Setup gear while a newer StudioFF release is out
		setup_item = self.navigationInterface.widget(self.setup_page.objectName())
		self.update_dot = DotInfoBadge.custom(UPDATE_DOT_COLOR, UPDATE_DOT_COLOR, setup_item)
		self.update_dot.setFixedSize(8, 8)
		self.update_dot.move(24, 7)
		self.update_dot.setToolTip('A StudioFF update is available')
		self.update_dot.hide()
		self.setup_page.studio_update_found.connect(self.update_dot.setVisible)
		self.setup_page.studio_update_found.connect(self.show_update_button)
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

	def set_monitor_shown(self, shown : bool) -> None:
		if shown:
			self.system_monitor.start()
		else:
			self.system_monitor.stop()

	def startup(self) -> None:
		from ffstudio.updater import read_update_result
		update_result = read_update_result()
		if update_result and update_result[0]:
			self.show_notification('success', 'StudioFF updated', 'You are now on %s.' % update_result[1])
		elif update_result:
			self.show_notification('error', 'The update did not install', '%s. StudioFF stayed on v%s.' % (update_result[1][:1].upper() + update_result[1][1:].rstrip('.'), APP_VERSION))
		if self.session.config.get('check_updates_at_startup') is not False:
			self.setup_page.check_studio_update(quiet = True)
		if not self.session.is_configured():
			self.switchTo(self.setup_page)
			self.show_notification('info', 'Welcome', 'Point the app at your FaceFusion v4 folder, or install v4 from here.')
			return
		if self.session.config.get('show_system_monitor'):
			self.system_monitor.start()
		# the python check runs off the ui thread and fills in the session's python version
		self.setup_page.refresh_status()
		self.session.refresh_schema()
		# the restored target's check waits for the window, every page should see it run
		self.studio_page.auto_check_content()

	def on_schema_loading(self, loading : bool) -> None:
		if loading:
			self.show_notification('info', 'Reading FaceFusion options…', 'This takes a few seconds.')

	def show_notification(self, level : str, title : str, content : str) -> None:
		factory = { 'success': InfoBar.success, 'warning': InfoBar.warning, 'error': InfoBar.error }.get(level, InfoBar.info)
		factory(title, content, orient = Qt.Orientation.Horizontal, isClosable = True, position = InfoBarPosition.TOP_RIGHT, duration = 8000 if level == 'error' else 4000, parent = self)

	def show_update_button(self, newer : bool) -> None:
		self.update_button.setText('Update to %s' % self.setup_page.studio_release.get('tag', '') if newer else '')
		self.update_button.setVisible(newer)

	def update_studio_now(self) -> None:
		self.switchTo(self.setup_page)
		page = self.setup_page
		# a git checkout cannot take a release zip, its Setup card says how it updates instead
		if not page.studio_install_button.isHidden() and page.studio_install_button.isEnabled():
			page.install_studio_update()

	def restore_window_state(self) -> None:
		"""puts the window back the way it was closed, maximized or at its normal size and position"""
		state = self.session.config.get('window_state') or {}
		geometry = state.get('geometry')
		if isinstance(geometry, list) and len(geometry) == 4 and all(isinstance(value, int) for value in geometry):
			x, y, width, height = geometry
			# only when the spot is still on a screen, a monitor may be gone since
			if any(screen.availableGeometry().intersects(QRect(x, y, width, height)) for screen in QApplication.screens()):
				self.setGeometry(x, y, max(width, self.minimumWidth()), max(height, self.minimumHeight()))
		if state.get('maximized', True):
			self.showMaximized()
		else:
			self.showNormal()

	def save_window_state(self) -> None:
		# normalGeometry is the restore size, also while the window is maximized
		rect = self.normalGeometry() if self.isMaximized() else self.geometry()
		self.session.config.set('window_state', { 'maximized': self.isMaximized(), 'geometry': [ rect.x(), rect.y(), rect.width(), rect.height() ] })

	def setup_tray(self) -> None:
		if self.tray:
			return
		self.tray = QSystemTrayIcon(self.windowIcon(), self)
		self.tray.setToolTip(APP_NAME)
		menu = QMenu(self)
		menu.addAction('Show ' + APP_NAME, self.show_from_tray)
		menu.addAction('Quit', self.quit_app)
		self.tray.setContextMenu(menu)
		self.tray.activated.connect(lambda reason: self.show_from_tray() if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
		self.tray.show()

	def show_from_tray(self) -> None:
		if self.tray:
			self.tray.hide()
		self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
		self.show()
		self.raise_()
		self.activateWindow()

	def quit_app(self) -> None:
		self.quitting = True
		if self.tray:
			self.tray.hide()
		self.show()
		self.close()
		# a declined "Stop and quit?" leaves the app open
		self.quitting = False

	def closeEvent(self, event) -> None:
		action = self.session.config.get('close_action') or 'close'
		if not self.quitting and action != 'close':
			self.save_window_state()
			self.session.config.save()
			event.ignore()
			if action == 'tray' and QSystemTrayIcon.isSystemTrayAvailable():
				self.setup_tray()
				self.hide()
			else:
				self.showMinimized()
			return
		self.save_window_state()
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
		self.system_monitor.stop()
		self.session.preview_worker.stop()
		self.session.clear_preview_files()
		self.session.save()
		super().closeEvent(event)
