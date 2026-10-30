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
VIDEO_EXTENSIONS = { '.avi', '.m4v', '.mkv', '.mov', '.mp4', '.mpeg', '.mpg', '.mxf', '.webm', '.wmv' } | GIF_EXTENSIONS
AUDIO_EXTENSIONS = { '.aac', '.flac', '.m4a', '.mp3', '.ogg', '.opus', '.wav' }
# files facefusion turns away by their extension, studio uses a copy of them in the converted folder (see usable_path):
# avif and heic as a png made with pillow, jfif (a jpeg by another name) as a jpg, flash video rewrapped into mkv by ffmpeg
# the see-through parts of an animated webp, kept as a gray video beside its video copy
ALPHA_SUFFIX = '.alpha.mp4'
CONVERTED_EXTENSIONS = { '.avif': '.png', '.heic': '.png', '.heif': '.png', '.jfif': '.jpg', '.flv': '.mkv', '.f4v': '.mkv' }


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
	"""the path itself, or for an avif, heic, jfif or flash video a copy with the same name in the converted folder, made once
	and kept. the copy is what studio shows and runs, so the result is a png named after the avif, an mkv named after the flv.
	the path itself when it cannot be made"""
	# an animated webp runs as a video, studio turns the result back into a webp (see gif_run.py)
	copy_extension = '.mp4' if is_animated_webp(path) else CONVERTED_EXTENSIONS.get(extension(path))
	if not copy_extension:
		return path
	from ffstudio.config import CONVERTED_DIR

	try:
		modified = os.path.getmtime(path)
	except OSError:
		return path
	# a folder per file and version (video copies of animated webps made before the see-through parts were kept differ), so two photo.avif files in different folders do not meet
	folder = os.path.join(CONVERTED_DIR, hashlib.sha1(('%s|%f%s' % (os.path.abspath(path), modified, '|alpha' if copy_extension == '.mp4' else '')).encode()).hexdigest()[:16])
	copy_path = os.path.join(folder, os.path.splitext(os.path.basename(path))[0] + copy_extension)
	if os.path.isfile(copy_path):
		write_original_note(folder, path)
		return copy_path
	os.makedirs(folder, exist_ok = True)
	if copy_extension == '.jpg':
		try:
			shutil.copyfile(path, copy_path + '.part')
			os.replace(copy_path + '.part', copy_path)
		except OSError:
			return path
	elif copy_extension == '.mkv':
		if not rewrap_video(path, copy_path):
			return path
	elif copy_extension == '.mp4':
		if not animation_to_video(path, copy_path):
			return path
	else:
		if not convert_image(path, copy_path):
			return path
	write_original_note(folder, path)
	return copy_path


def convert_image(path : str, copy_path : str) -> bool:
	try:
		from PIL import Image, ImageOps
		# heic is read by the pillow-heif plugin, avif by pillow itself
		from pillow_heif import register_heif_opener

		register_heif_opener()
		with Image.open(path) as image:
			# phones store the turn of a photo apart from its pixels
			image = ImageOps.exif_transpose(image)
			image.convert('RGBA' if 'A' in image.getbands() else 'RGB').save(copy_path + '.part', 'PNG')
		os.replace(copy_path + '.part', copy_path)
	except (ImportError, OSError, ValueError):
		return False
	return True


def is_animated_webp(path : str) -> bool:
	if extension(path) != '.webp':
		return False
	try:
		return frame_count(path, os.path.getmtime(path)) > 1
	except OSError:
		return False


@lru_cache(maxsize = 64)
def frame_count(path : str, modified : float) -> int:
	try:
		from PIL import Image

		with Image.open(path) as image:
			return getattr(image, 'n_frames', 1)
	except (ImportError, OSError, ValueError):
		return 1


def animation_to_video(path : str, video_path : str) -> bool:
	"""the animated webp as a lossless h264 video, which facefusion and ffmpeg can read (ffmpeg 7.0 cannot decode animated
	webp, pillow can). frames shown longer than others are repeated, so the timing holds at one frame rate"""
	ffmpeg = shutil.which('ffmpeg')
	if not ffmpeg:
		return False
	try:
		from math import gcd

		from PIL import Image, ImageSequence

		with Image.open(path) as image:
			durations = []
			see_through = False
			for frame in ImageSequence.Iterator(image):
				# pillow only reads a frame's duration once the frame is loaded
				frame.load()
				see_through = see_through or frame.convert('RGBA').getchannel('A').getextrema()[0] < 255
				durations.append(max(10, int(frame.info.get('duration') or 100)))
			step = 0
			for duration in durations:
				step = gcd(step, duration)
			# beyond 50 frames a second the repeats only make the video bigger
			step = max(step, 20)
			width, height = image.size
			temp_path = video_path + '.part.mp4'
			# yuv444 at qp 0 is lossless and, unlike yuv420, takes odd sizes
			process = subprocess.Popen([ ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '%dx%d' % (width, height), '-r', '1000/%d' % step, '-i', '-', '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv444p', temp_path ], stdin = subprocess.PIPE, stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL, creationflags = CREATE_NO_WINDOW)
			# the see-through parts go into a second video beside the first, gif_run.py puts them back on the result
			alpha_path = video_path + ALPHA_SUFFIX
			alpha_process = None
			if see_through:
				alpha_process = subprocess.Popen([ ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'gray', '-s', '%dx%d' % (width, height), '-r', '1000/%d' % step, '-i', '-', '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'gray', '-f', 'mp4', alpha_path + '.part' ], stdin = subprocess.PIPE, stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL, creationflags = CREATE_NO_WINDOW)
			elif os.path.isfile(alpha_path):
				os.remove(alpha_path)
			try:
				for frame, duration in zip(ImageSequence.Iterator(image), durations):
					rgba = frame.convert('RGBA')
					# see-through parts become white, a video has no transparency
					canvas = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
					canvas.alpha_composite(rgba)
					data = canvas.convert('RGB').tobytes()
					alpha_data = rgba.getchannel('A').tobytes() if alpha_process else b''
					for _ in range(max(1, round(duration / step))):
						process.stdin.write(data)
						if alpha_process:
							alpha_process.stdin.write(alpha_data)
			finally:
				process.stdin.close()
				code = process.wait()
				if alpha_process:
					alpha_process.stdin.close()
					code = code or alpha_process.wait()
		if code or not os.path.isfile(temp_path):
			return False
		os.replace(temp_path, video_path)
		if alpha_process:
			os.replace(alpha_path + '.part', alpha_path)
	except (ImportError, OSError, ValueError):
		return False
	return True


def original_path(path : str) -> str:
	"""the file a copy in the converted folder was made from, the path itself for any other file"""
	if not is_converted(path):
		return path
	try:
		with open(os.path.join(os.path.dirname(path), ORIGINAL_NOTE), encoding = 'utf-8') as note_file:
			return note_file.read().strip() or path
	except OSError:
		return path


def is_animation(target : str, output : str = '') -> bool:
	"""whether the run goes through gif_run.py: a gif target, or a video turned into a gif or an animated webp, such as the
	video copy of an animated webp"""
	if extension(target) in GIF_EXTENSIONS:
		return True
	return media_kind(target) == 'video' and (extension(output) if output else animation_extension(target)) in [ '.gif', '.webp' ]


def animation_extension(target : str) -> str:
	"""the extension a target's result gets: .webp for the video copy of an animated webp, otherwise the target's own"""
	if media_kind(target) == 'video' and extension(original_path(target)) == '.webp':
		return '.webp'
	return extension(target)


def rewrap_video(path : str, copy_path : str) -> bool:
	"""the flash video's streams moved into an mkv as they are, which takes seconds. old flash codecs that mkv cannot hold
	are encoded again at near lossless quality instead"""
	ffmpeg = shutil.which('ffmpeg')
	if not ffmpeg:
		return False
	temp_path = copy_path + '.part.mkv'
	streams = [ '-map', '0:v:0', '-map', '0:a?' ]
	for codecs in [ [ '-c', 'copy' ], [ '-c:v', 'libx264', '-crf', '10', '-preset', 'fast', '-c:a', 'aac', '-b:a', '256k' ] ]:
		try:
			code = subprocess.run([ ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', path ] + streams + codecs + [ temp_path ], capture_output = True, creationflags = CREATE_NO_WINDOW).returncode
		except (OSError, subprocess.SubprocessError):
			return False
		if not code and os.path.isfile(temp_path) and read_frame(temp_path) is not None:
			try:
				os.replace(temp_path, copy_path)
			except OSError:
				return False
			return True
	try:
		os.remove(temp_path)
	except OSError:
		pass
	return False


# beside each copy, the path of the file it was made from
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
	"""a copy usable_path made, its folder is no place to browse from"""
	from ffstudio.config import CONVERTED_DIR

	return os.path.normcase(os.path.abspath(path)).startswith(os.path.normcase(os.path.abspath(CONVERTED_DIR)) + os.sep)


def browse_dir(path : str) -> Optional[str]:
	"""the folder a browse window should open in for this file: its own, or for a copy the folder of the file it was made from.
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
	video_path = os.path.join(work_dir, 'gif_%s.mp4' % hashlib.sha1(('%s|%f%s' % (os.path.abspath(path), modified, '|alpha' if copy_extension == '.mp4' else '')).encode()).hexdigest()[:16])
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


def format_seconds(seconds : float) -> str:
	"""how long something took: 4.2 s under a minute, 1:24 from a minute on"""
	return '%.1f s' % seconds if seconds < 59.95 else format_length(seconds)


def format_timecode(frame_index : int, fps : float) -> str:
	if fps <= 0:
		return str(frame_index)
	seconds = frame_index / fps
	return '%02d:%02d:%05.2f' % (seconds // 3600, seconds % 3600 // 60, seconds % 60)
