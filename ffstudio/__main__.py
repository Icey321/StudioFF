import argparse
import hashlib
import os
import sys
from typing import Dict

from PySide6.QtCore import Qt, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QWidget

from ffstudio import splash
from ffstudio.config import DATA_DIR


def instance_name() -> str:
	# one per data folder, so a test copy with its own data does not count as the app
	return 'FaceFusionStudio-' + hashlib.sha1(os.path.normcase(os.path.abspath(DATA_DIR)).encode()).hexdigest()[:12]


def wake_running_instance() -> bool:
	"""asks an app that is already open to come to the front, true when there was one"""
	socket = QLocalSocket()
	socket.connectToServer(instance_name())
	if not socket.waitForConnected(500):
		return False
	if sys.platform == 'win32':
		import ctypes
		# this process got the focus from the double-click, let the open app take it
		ctypes.windll.user32.AllowSetForegroundWindow(-1)
	socket.write(b'show')
	socket.waitForBytesWritten(500)
	socket.disconnectFromServer()
	return True


def listen_for_instances(app : QApplication, opened : Dict[str, QWidget]) -> None:
	server = QLocalServer(app)
	QLocalServer.removeServer(instance_name())
	server.listen(instance_name())

	def on_connection() -> None:
		socket = server.nextPendingConnection()
		if socket:
			socket.disconnected.connect(socket.deleteLater)
		window = opened.get('window')
		# while still starting, the splash already shows it is on its way
		if window:
			if window.isMinimized():
				window.showNormal()
			window.raise_()
			window.activateWindow()

	server.newConnection.connect(on_connection)


def main() -> None:
	parser = argparse.ArgumentParser(prog = 'ffstudio')
	parser.add_argument('--screenshot', help = 'save a screenshot of the window to this png and quit')
	parser.add_argument('--page', default = 'studio', help = 'page to show for --screenshot')
	parser.add_argument('--theme', help = 'override the theme (auto, light, dark)')
	parser.add_argument('--size', default = '1500x960')
	args = parser.parse_args()

	if sys.platform == 'win32':
		import ctypes
		ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('FaceFusionStudio')

	QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
	app = QApplication(sys.argv)
	app.setApplicationName('FaceFusion Studio')

	if not args.screenshot:
		if wake_running_instance():
			return
		opened : Dict[str, QWidget] = {}
		listen_for_instances(app, opened)
		splash.show()
		splash.step('Loading the interface…')

	from ffstudio.main_window import MainWindow, apply_theme
	from ffstudio.session import Session
	from qfluentwidgets import setFontFamilies

	splash.step('Reading your settings…')
	session = Session()
	splash.step()
	apply_theme(args.theme or session.config.get('theme') or 'auto', session.config.get('accent_colors'))
	# one family only: with fallback families the first text box takes a second to appear, windows fills in other scripts anyway
	setFontFamilies([ 'Segoe UI' ])
	splash.step()
	window = MainWindow(session)

	if args.screenshot:
		width, height = (int(part) for part in args.size.split('x'))
		# mica is composited by windows and does not show up in grab()
		window.setMicaEffectEnabled(False)
		window.resize(width, height)
		window.show()
		page = getattr(window, args.page + '_page', window.studio_page)

		def capture() -> None:
			window.switchTo(page)
			QTimer.singleShot(1200, save)

		def save() -> None:
			os.makedirs(os.path.dirname(os.path.abspath(args.screenshot)), exist_ok = True)
			window.grab().save(args.screenshot)
			app.quit()

		QTimer.singleShot(1500, capture)
	else:
		opened['window'] = window
		splash.step()
		window.show()
		QTimer.singleShot(0, splash.finish)

	sys.exit(app.exec())


if __name__ == '__main__':
	main()
