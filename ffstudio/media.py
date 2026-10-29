import hashlib
import os
import shutil
import subprocess
from functools import lru_cache
from typing import Optional, Tuple

import cv2
import numpy
from PySide6.QtGui import QImage, QPixmap

from ffstudio.environment import CREATE_NO_WINDOW

IMAGE_EXTENSIONS = { '.bmp', '.jpeg', '.jpg', '.png', '.tif', '.tiff', '.webp' }
# gif is not a facefusion format, studio converts it to video around the run (see gif_run.py)
GIF_EXTENSIONS = { '.gif' }
VIDEO_EXTENSIONS = { '.avi', '.m4v', '.mkv', '.mov', '.mp4', '.mpeg', '.mxf', '.webm', '.wmv' } | GIF_EXTENSIONS
AUDIO_EXTENSIONS = { '.aac', '.flac', '.m4a', '.mp3', '.ogg', '.opus', '.wav' }
# images neither facefusion nor opencv read, studio uses a png copy of them made with pillow (see usable_path)
CONVERTED_EXTENSIONS = { '.avif', '.heic', '.heif' }


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


def usable_path(path : str) -> str:
	"""the path itself, or for an avif or heic a png copy with the same name in the converted folder, made once and kept.
	the copy is what studio shows and runs, so the result is a png named after the avif. the path itself when it cannot be made"""
	if extension(path) not in CONVERTED_EXTENSIONS:
		return path
	from ffstudio.config import CONVERTED_DIR

	try:
		modified = os.path.getmtime(path)
	except OSError:
		return path
	# a folder per file and version, so two photo.avif files in different folders do not meet
	folder = os.path.join(CONVERTED_DIR, hashlib.sha1(('%s|%f' % (os.path.abspath(path), modified)).encode()).hexdigest()[:16])
	copy_path = os.path.join(folder, os.path.splitext(os.path.basename(path))[0] + '.png')
	if os.path.isfile(copy_path):
		write_original_note(folder, path)
		return copy_path
	try:
		from PIL import Image, ImageOps
		# heic is read by the pillow-heif plugin, avif by pillow itself
		from pillow_heif import register_heif_opener

		register_heif_opener()
		with Image.open(path) as image:
			# phones store the turn of a photo apart from its pixels
			image = ImageOps.exif_transpose(image)
			os.makedirs(folder, exist_ok = True)
			image.convert('RGBA' if 'A' in image.getbands() else 'RGB').save(copy_path + '.part', 'PNG')
		os.replace(copy_path + '.part', copy_path)
	except (ImportError, OSError, ValueError):
		return path
	write_original_note(folder, path)
	return copy_path


# beside each png copy, the path of the avif or heic it was made from
ORIGINAL_NOTE = 'original.txt'


def write_original_note(folder : str, path : str) -> None:
	note_path = os.path.join(folder, ORIGINAL_NOTE)
	if not os.path.isfile(note_path):
		try:
			with open(note_path, 'w', encoding = 'utf-8') as note_file:
				note_file.write(os.path.abspath(path))
		except OSError:
			pass


def is_converted(path : str) -> bool:
	"""a png copy usable_path made, its folder is no place to browse from"""
	from ffstudio.config import CONVERTED_DIR

	return os.path.normcase(os.path.abspath(path)).startswith(os.path.normcase(os.path.abspath(CONVERTED_DIR)) + os.sep)


def browse_dir(path : str) -> Optional[str]:
	"""the folder a browse window should open in for this file: its own, or for a png copy the folder of the avif or heic.
	None when that is unknown"""
	if not is_converted(path):
		return os.path.dirname(path)
	try:
		with open(os.path.join(os.path.dirname(path), ORIGINAL_NOTE), encoding = 'utf-8') as note_file:
			folder = os.path.dirname(note_file.read().strip())
	except OSError:
		return None
	return folder if os.path.isdir(folder) else None


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


@lru_cache(maxsize = 32)
def probe_facefusion_video(ffprobe : str, path : str, modified : float) -> Optional[dict]:
	"""the frame rate, frame count, size and color transfer, worked out the way facefusion's ffprobe.extract_video_metadata does"""
	entries = {}
	for kind, names in [ ('stream', 'width,height,avg_frame_rate,r_frame_rate,color_transfer'), ('format', 'duration') ]:
		select = [ '-select_streams', 'v:0' ] if kind == 'stream' else []
		try:
			output = subprocess.run([ ffprobe, '-loglevel', 'error' ] + select + [ '-show_entries', kind + '=' + names, '-of', 'default=noprint_wrappers=1', '-i', path ], capture_output = True, text = True, timeout = 20, creationflags = CREATE_NO_WINDOW).stdout
		except (OSError, subprocess.SubprocessError):
			return None
		for line in output.splitlines():
			key, _, value = line.partition('=')
			entries.setdefault(key.strip(), value.strip())
	fps = 0.0
	for frame_rate in [ entries.get('avg_frame_rate'), entries.get('r_frame_rate') ]:
		numerator, _, denominator = (frame_rate or '').partition('/')
		if numerator.isdigit() and denominator.isdigit() and int(numerator) and int(denominator):
			fps = int(numerator) / int(denominator)
			break
	try:
		duration = float(entries.get('duration'))
		width, height = int(entries.get('width')), int(entries.get('height'))
	except (TypeError, ValueError):
		return None
	if not fps or not width or not height:
		return None
	return { 'fps': fps, 'frame_total': round(duration * fps), 'width': width, 'height': height, 'color_transfer': entries.get('color_transfer', 'unknown') }


def gif_as_video(ffmpeg : str, path : str, work_dir : str) -> Optional[str]:
	"""the video gif_run.py turns the gif into for facefusion, made once per gif and kept for the next preview"""
	from ffstudio.gif_run import gif_to_video_args

	try:
		modified = os.path.getmtime(path)
	except OSError:
		return None
	video_path = os.path.join(work_dir, 'gif_%s.mp4' % hashlib.sha1(('%s|%f' % (os.path.abspath(path), modified)).encode()).hexdigest()[:16])
	if os.path.isfile(video_path):
		return video_path
	os.makedirs(work_dir, exist_ok = True)
	# only the last gif is kept
	for name in os.listdir(work_dir):
		if name.startswith('gif_') and name.endswith('.mp4'):
			try:
				os.remove(os.path.join(work_dir, name))
			except OSError:
				pass
	temp_path = video_path + '.part.mp4'
	try:
		code = subprocess.run([ ffmpeg, '-hide_banner', '-loglevel', 'error', '-y' ] + gif_to_video_args(path, temp_path), capture_output = True, timeout = 120, creationflags = CREATE_NO_WINDOW).returncode
		if code or not os.path.isfile(temp_path):
			return None
		os.replace(temp_path, video_path)
	except (OSError, subprocess.SubprocessError):
		return None
	return video_path


def read_facefusion_frame(path : str, frame_index : int, search_path : str, work_dir : str) -> Optional[numpy.ndarray]:
	"""a video frame decoded by ffmpeg the way facefusion's video reader decodes it, so the nsfw filter sees the same pixels
	in a preview as in a whole run. opencv decodes a little differently, enough to flip a borderline frame. a gif is read
	from the video gif_run.py makes of it, which is what facefusion gets. None when ffmpeg is missing or the frame cannot
	be read, the caller then uses opencv's frame"""
	ffmpeg = shutil.which('ffmpeg', path = search_path)
	ffprobe = shutil.which('ffprobe', path = search_path)
	if not ffmpeg or not ffprobe:
		return None
	if extension(path) in GIF_EXTENSIONS:
		path = gif_as_video(ffmpeg, path, work_dir)
		if not path:
			return None
	try:
		metadata = probe_facefusion_video(ffprobe, path, os.path.getmtime(path))
	except OSError:
		return None
	if not metadata:
		return None
	frame_index = max(0, min(metadata.get('frame_total') - 1, frame_index))
	width, height = metadata.get('width'), metadata.get('height')
	# the same command as facefusion's ffmpeg.create_video_reader, stopped after the one frame
	commands = [ ffmpeg, '-loglevel', 'error', '-ss', str(frame_index / metadata.get('fps')), '-i', path ]
	if metadata.get('color_transfer') in [ 'smpte2084', 'arib-std-b67' ]:
		commands += [ '-vf', 'scale=out_primaries=bt709:out_transfer=bt709:intent=perceptual' ]
	commands += [ '-fps_mode', 'passthrough', '-pix_fmt', 'bgr24', '-f', 'rawvideo', '-frames:v', '1', '-' ]
	try:
		data = subprocess.run(commands, capture_output = True, timeout = 30, creationflags = CREATE_NO_WINDOW).stdout
	except (OSError, subprocess.SubprocessError):
		return None
	if len(data) != width * height * 3:
		return None
	return numpy.frombuffer(data, numpy.uint8).reshape(height, width, 3).copy()


def thumbnail(path : str, size : int = 96) -> QPixmap:
	frame = read_frame(path)
	if frame is None:
		return QPixmap()
	height, width = frame.shape[:2]
	scale = size / max(height, width)
	frame = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))), interpolation = cv2.INTER_AREA)
	return to_pixmap(frame)


def video_length(path : str) -> Tuple[float, int]:
	"""how many seconds a video plays and how many frames it has, 0 when it cannot be read"""
	reader = VideoReader(path)
	seconds = reader.frame_count / reader.fps if reader.fps > 0 else 0.0
	frames = max(reader.frame_count, 0) if seconds > 0 else 0
	reader.release()
	return seconds, frames


def format_length(seconds : float) -> str:
	"""a running time like 0:42 or 1:05:03"""
	seconds = int(round(seconds))
	if seconds >= 3600:
		return '%d:%02d:%02d' % (seconds // 3600, seconds % 3600 // 60, seconds % 60)
	return '%d:%02d' % (seconds // 60, seconds % 60)


def format_timecode(frame_index : int, fps : float) -> str:
	if fps <= 0:
		return str(frame_index)
	seconds = frame_index / fps
	return '%02d:%02d:%05.2f' % (seconds // 3600, seconds % 3600 // 60, seconds % 60)
