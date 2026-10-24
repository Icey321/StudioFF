import argparse
import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication


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

	from qfluentwidgets import setThemeColor
	from ffstudio.main_window import MainWindow, apply_theme
	from ffstudio.session import Session

	session = Session()
	apply_theme(args.theme or session.config.get('theme') or 'auto')
	setThemeColor('#7c5cff')
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
		window.show()

	sys.exit(app.exec())


if __name__ == '__main__':
	main()
