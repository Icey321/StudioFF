"""hands the swapped webcam picture to a virtual camera that Zoom, Discord or OBS can pick, through pyvirtualcam

pyvirtualcam is not one of StudioFF's own packages, it is installed into StudioFF's Python the first time it is asked for
(after asking). On Windows it sends to the virtual camera that OBS Studio installs"""
import importlib
import importlib.util
import sys
import threading
from typing import Any, Optional

from PySide6.QtCore import QObject, QProcess, Signal

PACKAGE = 'pyvirtualcam'
INSTALL_TITLE = 'Install pyvirtualcam?'
INSTALL_TEXT = ('Sending the webcam picture to other apps needs pyvirtualcam, a small Python package (about 1 MB). '
	'It is installed with pip into StudioFF\'s own Python environment and nowhere else.\n\n'
	'It also needs a virtual camera driver: install OBS Studio and press its Start Virtual Camera button once. '
	'Then pick "OBS Virtual Camera" as the camera in Zoom, Discord or Teams.')
NO_DRIVER_TEXT = 'No virtual camera found. Install OBS Studio and press its Start Virtual Camera button once, then try again.'


def is_installed() -> bool:
	return importlib.util.find_spec(PACKAGE) is not None


class PackageInstaller(QObject):
	"""pip install pyvirtualcam into the Python StudioFF runs on"""
	finished = Signal(bool, str)

	def __init__(self, parent : Optional[QObject] = None) -> None:
		super().__init__(parent)
		self.process : Optional[QProcess] = None
		self.output = ''

	def is_running(self) -> bool:
		return self.process is not None

	def start(self) -> None:
		if self.process:
			return
		self.output = ''
		self.process = QProcess(self)
		self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
		self.process.readyReadStandardOutput.connect(lambda: setattr(self, 'output', self.output + bytes(self.process.readAllStandardOutput().data()).decode(errors = 'replace')))
		self.process.finished.connect(self.on_finished)
		self.process.errorOccurred.connect(lambda error: self.on_finished(-1, None) if error == QProcess.ProcessError.FailedToStart else None)
		self.process.start(sys.executable, [ '-m', 'pip', 'install', '--disable-pip-version-check', PACKAGE ])

	def on_finished(self, exit_code : int, _ : Any) -> None:
		if not self.process:
			return
		self.process.deleteLater()
		self.process = None
		importlib.invalidate_caches()
		installed = exit_code == 0 and is_installed()
		tail = '\n'.join(line for line in self.output.strip().splitlines()[-3:])
		self.finished.emit(installed, '' if installed else (tail or 'pip exit code %d' % exit_code))


class VirtualCamera:
	"""sends frames to the virtual camera off the ui thread, a frame that comes while the last one is still being sent replaces
	the one waiting, so the camera never falls behind. it opens again when the picture changes size"""

	def __init__(self, fps : float = 30.0) -> None:
		self.fps = fps
		self.condition = threading.Condition()
		self.waiting : Any = None
		self.running = False
		self.thread : Optional[threading.Thread] = None
		# the error that ended the thread, read by the page
		self.error = ''
		self.device = ''

	def start(self) -> None:
		if self.running:
			return
		self.error = ''
		self.running = True
		self.thread = threading.Thread(target = self.run, name = 'virtual camera', daemon = True)
		self.thread.start()

	def stop(self) -> None:
		with self.condition:
			self.running = False
			self.waiting = None
			self.condition.notify_all()
		if self.thread:
			self.thread.join(2)
			self.thread = None

	def send(self, frame : Any) -> None:
		"""a bgr numpy frame"""
		with self.condition:
			if self.running:
				self.waiting = frame
				self.condition.notify_all()

	def run(self) -> None:
		pyvirtualcam = importlib.import_module(PACKAGE)
		camera = None
		try:
			while True:
				with self.condition:
					while self.running and self.waiting is None:
						self.condition.wait()
					if not self.running:
						return
					frame, self.waiting = self.waiting, None
				height, width = frame.shape[:2]
				if camera is None or (camera.width, camera.height) != (width, height):
					if camera is not None:
						camera.close()
					camera = pyvirtualcam.Camera(width, height, self.fps, fmt = pyvirtualcam.PixelFormat.BGR)
					self.device = camera.device
				camera.send(frame)
		except Exception as exception:
			self.error = NO_DRIVER_TEXT if 'backend' in str(exception).lower() or 'not installed' in str(exception).lower() else str(exception)
			self.running = False
		finally:
			if camera is not None:
				camera.close()
