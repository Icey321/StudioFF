import os
from typing import Optional

import cv2
import numpy
from PySide6.QtGui import QImage, QPixmap

IMAGE_EXTENSIONS = { '.bmp', '.jpeg', '.jpg', '.png', '.tif', '.tiff', '.webp' }
# gif is not a facefusion format, studio converts it to video around the run (see gif_run.py)
GIF_EXTENSIONS = { '.gif' }
VIDEO_EXTENSIONS = { '.avi', '.m4v', '.mkv', '.mov', '.mp4', '.mpeg', '.mxf', '.webm', '.wmv' } | GIF_EXTENSIONS
AUDIO_EXTENSIONS = { '.aac', '.flac', '.m4a', '.mp3', '.ogg', '.opus', '.wav' }


def extension(path : str) -> str:
	return os.path.splitext(path)[1].lower()


def media_kind(path : str) -> Optional[str]:
	ext = extension(path)
	if ext in IMAGE_EXTENSIONS:
		return 'image'
	if ext in VIDEO_EXTENSIONS:
		return 'video'
	if ext in AUDIO_EXTENSIONS:
		return 'audio'
	return None


def read_image(path : str) -> Optional[numpy.ndarray]:
	# cv2.imread cannot open non ascii paths on windows
	try:
		data = numpy.fromfile(path, dtype = numpy.uint8)
		return cv2.imdecode(data, cv2.IMREAD_COLOR)
	except (OSError, ValueError):
		return None


def write_image(path : str, frame : numpy.ndarray) -> bool:
	success, data = cv2.imencode(extension(path) or '.png', frame)
	if success:
		data.tofile(path)
	return bool(success)


def to_pixmap(frame : Optional[numpy.ndarray]) -> QPixmap:
	if frame is None:
		return QPixmap()
	rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
	height, width = rgb.shape[:2]
	image = QImage(rgb.data, width, height, width * 3, QImage.Format.Format_RGB888)
	return QPixmap.fromImage(image.copy())


class VideoReader:
	def __init__(self, path : str) -> None:
		self.path = path
		self.capture = cv2.VideoCapture(path)
		self.frame_count = int(self.capture.get(cv2.CAP_PROP_FRAME_COUNT)) if self.capture.isOpened() else 0
		self.fps = self.capture.get(cv2.CAP_PROP_FPS) if self.capture.isOpened() else 0.0
		# the frame the capture reads next, -1 when unknown
		self.position = 0
		self.last_index = -1
		self.last_frame = None

	def read(self, frame_index : int) -> Optional[numpy.ndarray]:
		if not self.capture.isOpened():
			return None
		frame_index = max(0, frame_index)
		# the same frame again is kept, the next one needs no seek
		if frame_index == self.last_index:
			return self.last_frame
		if frame_index != self.position:
			self.capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
		success, frame = self.capture.read()
		self.position = frame_index + 1 if success else -1
		self.last_index = frame_index if success else -1
		self.last_frame = frame if success else None
		return self.last_frame

	def release(self) -> None:
		self.capture.release()


def read_frame(path : str, frame_index : int = 0) -> Optional[numpy.ndarray]:
	kind = media_kind(path)
	if kind == 'image':
		return read_image(path)
	if kind == 'video':
		reader = VideoReader(path)
		frame = reader.read(frame_index)
		reader.release()
		return frame
	return None


def thumbnail(path : str, size : int = 96) -> QPixmap:
	frame = read_frame(path)
	if frame is None:
		return QPixmap()
	height, width = frame.shape[:2]
	scale = size / max(height, width)
	frame = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))), interpolation = cv2.INTER_AREA)
	return to_pixmap(frame)


def format_timecode(frame_index : int, fps : float) -> str:
	if fps <= 0:
		return str(frame_index)
	seconds = frame_index / fps
	return '%02d:%02d:%05.2f' % (seconds // 3600, seconds % 3600 // 60, seconds % 60)
