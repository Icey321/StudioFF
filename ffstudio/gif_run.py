"""
Runs inside the FaceFusion Python environment (NOT the UI environment).

FaceFusion does not accept GIF targets, so this converts the GIF to a lossless
MP4, runs FaceFusion on it and turns the result back into a looping GIF.
A video target (such as the copy studio makes of an animated WebP) is run as it is
and turned into a looping GIF or animated WebP, whichever the output path names.

usage: python gif_run.py <target_gif_or_video> <output_gif_or_webp> <work_dir> -- <facefusion run args without -t / -o>
preview_worker.py imports run_gif to make the same run on the models it keeps loaded
"""
import os
import shutil
import subprocess
import sys
from typing import Callable, List


def run_ffmpeg(ffmpeg : str, args) -> int:
	return subprocess.call([ ffmpeg, '-hide_banner', '-loglevel', 'error', '-y' ] + args)


def gif_to_video_args(gif_path : str, video_path : str) -> List[str]:
	"""the ffmpeg arguments that turn the gif into the video facefusion runs on, the nsfw check and previews use them too,
	since facefusion's nsfw filter can judge the converted frames differently from the gif's own"""
	# h264 needs even sizes, the padding is cropped off again at the end
	return [ '-i', gif_path, '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv444p', video_path ]


def main() -> int:
	return run_gif(sys.argv[1:], lambda args: subprocess.call([ sys.executable, 'facefusion.py' ] + args))


def run_gif(argv : List[str], run_facefusion : Callable[[List[str]], int]) -> int:
	"""argv as the usage above, run_facefusion makes the run from facefusion.py's arguments and returns its exit code"""
	separator = argv.index('--')
	target_path, output_path, work_dir = argv[:separator]
	facefusion_args = argv[separator + 1:]
	ffmpeg = shutil.which('ffmpeg')
	if not ffmpeg:
		print('ffmpeg was not found on PATH, it is needed to convert GIF files', flush = True)
		return 1

	os.makedirs(work_dir, exist_ok = True)
	gif_target = target_path.lower().endswith('.gif')
	temp_target = os.path.join(work_dir, 'target.mp4') if gif_target else target_path
	temp_output = os.path.join(work_dir, 'output.mp4')
	try:
		if gif_target:
			print('Converting GIF to video', flush = True)
			if run_ffmpeg(ffmpeg, gif_to_video_args(target_path, temp_target)):
				print('Could not convert ' + target_path, flush = True)
				return 1
		exit_code = run_facefusion(facefusion_args + [ '-t', temp_target, '-o', temp_output ])
		if exit_code or not os.path.isfile(temp_output):
			return exit_code or 1

		webp_output = output_path.lower().endswith('.webp')
		print('Converting result to ' + ('animated WebP' if webp_output else 'GIF'), flush = True)
		width, height = probe_size(ffmpeg, target_path)
		# facefusion may round the size down, so never crop wider or taller than the frame
		crop = 'crop=min(iw\\,%d):min(ih\\,%d):0:0,' % (width, height) if width and height else ''
		if webp_output:
			encode_args = [ '-vf', crop + 'null', '-c:v', 'libwebp_anim', '-quality', '90', '-loop', '0' ]
		else:
			encode_args = [ '-filter_complex', '[0:v]' + crop + 'split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a', '-loop', '0' ]
		if run_ffmpeg(ffmpeg, [ '-i', temp_output ] + encode_args + [ output_path ]):
			print('Could not write ' + output_path, flush = True)
			return 1
		return 0
	finally:
		shutil.rmtree(work_dir, ignore_errors = True)


def probe_size(ffmpeg : str, path : str):
	ffprobe = os.path.join(os.path.dirname(ffmpeg), 'ffprobe' + os.path.splitext(ffmpeg)[1])
	try:
		output = subprocess.check_output([ ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0:s=x', path ], text = True)
		width, height = output.strip().split('x')[:2]
		return int(width), int(height)
	except (OSError, ValueError, subprocess.CalledProcessError):
		return 0, 0


if __name__ == '__main__':
	sys.exit(main())
