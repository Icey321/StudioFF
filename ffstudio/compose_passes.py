"""
Runs inside the FaceFusion Python environment (NOT the UI environment).

Face mapping runs FaceFusion once per mapped person on the same target, each run changes only that
person's face, plus a base run that changes nothing. This puts the changed parts of every run onto
the base: base + (pass - base) for each pass. Every run reads and writes the frames the same way,
so outside the changed faces the passes equal the base pixel for pixel.

usage: python compose_passes.py <base> <output> <settings json> -- <pass outputs>
	settings: {"image_quality": 80, "video_encoder": "libx264", "video_quality": 80, "video_preset": "veryfast", "audio_encoder": "aac"}
	an image output is written with the image quality, a video output is encoded with the video settings
	and gets the audio of the base
"""
import json
import os
import shutil
import subprocess
import sys

import cv2
import numpy

IMAGE_EXTENSIONS = { '.bmp', '.jpeg', '.jpg', '.png', '.tif', '.tiff', '.webp' }
# exact color conversion both ways, ffmpeg's fast default shifts the colors a little on every round trip
SCALE_FLAGS = [ '-sws_flags', 'accurate_rnd+full_chroma_int+bitexact' ]


def read_frames(ffmpeg : str, path : str, width : int, height : int):
	"""the frames of a video as facefusion's ffmpeg decodes them, not as opencv does"""
	process = subprocess.Popen([ ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', path ] + SCALE_FLAGS + [ '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-' ], stdout = subprocess.PIPE)
	size = width * height * 3
	try:
		while True:
			data = process.stdout.read(size)
			if len(data) < size:
				break
			yield numpy.frombuffer(data, numpy.uint8).reshape(height, width, 3)
	finally:
		process.stdout.close()
		process.kill()
		process.wait()


def compose(base : numpy.ndarray, passes : list) -> numpy.ndarray:
	result = base.astype(numpy.int16)
	for frame in passes:
		if frame is not None and frame.shape == base.shape:
			result += frame.astype(numpy.int16) - base
	return result.clip(0, 255).astype(numpy.uint8)


def read_image(path : str):
	return cv2.imdecode(numpy.fromfile(path, dtype = numpy.uint8), cv2.IMREAD_COLOR)


def compose_image(base_path : str, output_path : str, settings : dict, pass_paths : list) -> int:
	base = read_image(base_path)
	if base is None:
		print('Could not read ' + base_path, flush = True)
		return 1
	result = compose(base, [ read_image(path) for path in pass_paths ])
	extension = os.path.splitext(output_path)[1].lower()
	quality = int(settings.get('image_quality') or 80)
	params = [ cv2.IMWRITE_JPEG_QUALITY, quality ] if extension in [ '.jpg', '.jpeg' ] else [ cv2.IMWRITE_WEBP_QUALITY, quality ] if extension == '.webp' else []
	success, data = cv2.imencode(extension, result, params)
	if not success:
		print('Could not write ' + output_path, flush = True)
		return 1
	data.tofile(output_path)
	return 0


def compose_video(base_path : str, output_path : str, settings : dict, pass_paths : list) -> int:
	# the encoder settings the way facefusion builds them
	sys.path.insert(0, os.getcwd())
	from facefusion import ffmpeg_builder
	from facefusion.ffmpeg import fix_audio_encoder, fix_video_encoder

	ffmpeg = shutil.which('ffmpeg')
	if not ffmpeg:
		print('ffmpeg was not found on PATH', flush = True)
		return 1
	capture = cv2.VideoCapture(base_path)
	width, height = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
	fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
	total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
	capture.release()
	base = read_frames(ffmpeg, base_path, width, height)
	passes = [ read_frames(ffmpeg, path, width, height) for path in pass_paths ]
	video_format = os.path.splitext(output_path)[1].lower().lstrip('.')
	encoder = fix_video_encoder(video_format, settings.get('video_encoder') or 'libx264')
	audio_encoder = fix_audio_encoder(video_format, settings.get('audio_encoder') or 'aac')
	commands = [ ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', '%dx%d' % (width, height), '-r', str(fps), '-i', '-', '-i', base_path, '-map', '0:v', '-map', '1:a?' ] + SCALE_FLAGS
	commands += ffmpeg_builder.set_video_encoder(encoder)
	commands += ffmpeg_builder.set_video_quality(encoder, int(settings.get('video_quality') if settings.get('video_quality') is not None else 80))
	commands += ffmpeg_builder.set_video_preset(encoder, settings.get('video_preset') or 'veryfast')
	# the same color conversion facefusion uses, without it the colors drift a little from the passes
	commands += ffmpeg_builder.convert_color_space('bt709')
	commands += ffmpeg_builder.set_pixel_format(encoder)
	commands += [ '-c:a', audio_encoder, '-shortest', output_path ]
	process = subprocess.Popen(commands, stdin = subprocess.PIPE)
	done = 0
	for frame in base:
		frames = [ next(reader, None) for reader in passes ]
		try:
			process.stdin.write(compose(frame, frames).tobytes())
		except OSError:
			break
		done += 1
		if done % 25 == 0 or done == total:
			print('Combining the passes: frame %d of %d' % (done, total), flush = True)
	process.stdin.close()
	exit_code = process.wait()
	for reader in [ base ] + passes:
		reader.close()
	if exit_code or not done:
		print('Could not encode ' + output_path, flush = True)
		return exit_code or 1
	return 0


def main() -> int:
	separator = sys.argv.index('--')
	base_path, output_path, settings = sys.argv[1:separator]
	pass_paths = sys.argv[separator + 1:]
	settings = json.loads(settings)
	print('Combining %d passes' % len(pass_paths), flush = True)
	if os.path.splitext(output_path)[1].lower() in IMAGE_EXTENSIONS:
		return compose_image(base_path, output_path, settings, pass_paths)
	return compose_video(base_path, output_path, settings, pass_paths)


if __name__ == '__main__':
	sys.exit(main())
