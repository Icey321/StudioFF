"""
Runs inside the FaceFusion Python environment (NOT the UI environment).

Runs FaceFusion's own NSFW content check on targets without processing them,
so the UI can tell beforehand whether a run will be refused, and show which
image or frames the check flagged. It only reads the targets, the check itself
is FaceFusion's and stays unchanged.

A video is checked the way FaceFusion does it, through its own video reader:
every 30th frame of the trimmed range, blocked when the flagged share (scaled
up by 30) is above 10%.

usage: python content_check.py <preview_dir> <providers> <device_ids> <trim_start> <trim_end> -- <target> [<target> ...]
	providers and device ids are comma separated, a trim of -1 means none
prints one line per target: CONTENT_RESULT {json}
"""
import json
import os
import sys

RESULT_PREFIX = 'CONTENT_RESULT '
# facefusion's content_store: one frame in STEP is checked, a rate above LIMIT blocks the video
STEP = 30
LIMIT = 10.0
MAX_PREVIEWS = 6
BAR_WIDTH = 30


def print_progress(title : str, done : int, total : int, status : str) -> None:
	# the same shape as facefusion's own bar, so the ui parses it the same way
	filled = int(BAR_WIDTH * done / total) if total else 0
	# the closing erase-line code ends the status, the ui would read on into the next line without it
	sys.stdout.write('\x1b[G%s \x1b[94m%s\x1b[90m%s\x1b[0m %s\x1b[K' % (title, '=' * filled, '=' * (BAR_WIDTH - filled), status))
	sys.stdout.flush()


def save_preview(preview_dir : str, index : int, name : str, frame) -> str:
	import cv2

	height, width = frame.shape[:2]
	scale = min(1.0, 480 / max(height, width))
	if scale < 1:
		frame = cv2.resize(frame, (int(width * scale), int(height * scale)), interpolation = cv2.INTER_AREA)
	path = os.path.join(preview_dir, 'flagged_%d_%s.jpg' % (index, name))
	cv2.imwrite(path, frame)
	return path


def check_image(target_path : str, preview_dir : str, index : int) -> dict:
	from facefusion import content_analyser
	from facefusion.vision import read_image

	result = { 'path': target_path, 'kind': 'image', 'blocked': False, 'rate': 0.0, 'checked': 1, 'flagged': [], 'previews': [] }
	frame = read_image(target_path)
	if frame is None:
		result['error'] = 'could not read the image'
		return result
	# facefusion's own three detectors and vote, as detect_nsfw runs them, keeping how many flagged the image
	votes = sum([ content_analyser.detect_with_nsfw_1(frame), content_analyser.detect_with_nsfw_2(frame), content_analyser.detect_with_nsfw_3(frame) ])
	result.update({ 'votes': votes, 'rate': round(votes / 3 * 100, 1) })
	if votes >= 2:
		result.update({ 'blocked': True, 'flagged': [ 0 ], 'previews': [ save_preview(preview_dir, index, 'image', frame) ] })
	return result


def check_video(target_path : str, preview_dir : str, index : int, trim_start : int, trim_end : int) -> dict:
	"""the same frames, the same reader and the same count as facefusion's content_analyser.analyse_video,
	only noting which frames were flagged. opencv decodes a little differently, enough to flip a borderline frame"""
	from facefusion import content_analyser, content_store, ffprobe, video_manager
	from facefusion.vision import restrict_video_range

	result = { 'path': target_path, 'kind': 'video', 'blocked': False, 'rate': 0.0, 'checked': 0, 'flagged': [], 'previews': [], 'fps': 0.0 }
	metadata = ffprobe.extract_static_video_metadata(target_path)
	if not metadata or not metadata.get('frame_total'):
		result['error'] = 'could not open the video'
		return result
	result['fps'] = metadata.get('fps') or 0.0
	start, end = restrict_video_range(target_path, trim_start if trim_start >= 0 else None, trim_end if trim_end >= 0 else None)
	video_reader = video_manager.get_reader(target_path, 'content_check')
	if start > 0:
		video_manager.seek_video_reader(video_reader, start)
	content_store.clear()
	name = os.path.basename(target_path)
	for frame_index in range(start, end):
		# a frame past the real end reads as nothing, facefusion still counts it
		frame = video_manager.read_video_frame(video_reader)
		if content_store.tick(STEP):
			result['checked'] += 1
			if content_analyser.analyse_frame(frame):
				content_store.set_hit()
				result['flagged'].append(frame_index)
				if len(result['previews']) < MAX_PREVIEWS:
					result['previews'].append(save_preview(preview_dir, index, str(frame_index), frame))
		print_progress('checking ' + name, frame_index - start + 1, end - start, 'rate = %.1f' % content_store.calculate_rate(STEP))
	# closes the reader's ffmpeg, a batch would otherwise leave one running per video
	video_manager.clear()
	result['rate'] = round(content_store.calculate_rate(STEP), 1)
	result['blocked'] = content_store.calculate_rate(STEP) > LIMIT
	return result


def check_gif(target_path : str, preview_dir : str, index : int, trim_start : int, trim_end : int) -> dict:
	"""studio runs a gif as the video gif_run.py converts it to, so that video is what gets checked"""
	import shutil
	import subprocess

	from gif_run import gif_to_video_args

	video_path = os.path.join(preview_dir, 'gif_%d.mp4' % index)
	ffmpeg = shutil.which('ffmpeg')
	if not ffmpeg or subprocess.call([ ffmpeg, '-hide_banner', '-loglevel', 'error', '-y' ] + gif_to_video_args(target_path, video_path)):
		return { 'path': target_path, 'kind': 'video', 'blocked': False, 'rate': 0.0, 'checked': 0, 'flagged': [], 'previews': [], 'error': 'could not convert the gif to video' }
	result = check_video(video_path, preview_dir, index, trim_start, trim_end)
	result['path'] = target_path
	os.remove(video_path)
	return result


def main() -> int:
	separator = sys.argv.index('--')
	preview_dir, providers, device_ids, trim_start, trim_end = sys.argv[1:separator]
	targets = sys.argv[separator + 1:]
	# run from the facefusion folder, the script itself lives in the ui folder
	sys.path.insert(0, os.getcwd())

	import facefusion.choices
	from facefusion import content_analyser, content_store, inference_manager, logger, process_manager, session_context, session_manager, state_manager, video_manager
	from facefusion.filesystem import is_image, is_video

	# the same setup as facefusion's own cli before it runs a command
	state_manager.init()
	logger.init('info')
	session_manager.set_cli_session(session_context.get_session_id(), session_manager.create_cli_session())
	content_store.init()
	inference_manager.init()
	process_manager.init()
	video_manager.init()
	state_manager.init_item('execution_providers', [ provider for provider in providers.split(',') if provider ] or [ 'cpu' ])
	state_manager.init_item('execution_device_ids', [ device_id for device_id in device_ids.split(',') if device_id ] or [ '0' ])
	state_manager.init_item('execution_thread_count', 1)
	state_manager.init_item('download_providers', list(facefusion.choices.download_providers))
	state_manager.init_item('download_scope', 'full')
	print('Loading the NSFW check models', flush = True)
	if not content_analyser.pre_check():
		print('Could not download the NSFW check models', flush = True)
		return 1

	os.makedirs(preview_dir, exist_ok = True)
	for index, target_path in enumerate(targets):
		print('Checking %s (%d of %d)' % (os.path.basename(target_path), index + 1, len(targets)), flush = True)
		if is_image(target_path):
			result = check_image(target_path, preview_dir, index)
		elif target_path.lower().endswith('.gif'):
			result = check_gif(target_path, preview_dir, index, int(trim_start), int(trim_end))
		elif is_video(target_path):
			result = check_video(target_path, preview_dir, index, int(trim_start), int(trim_end))
		else:
			result = { 'path': target_path, 'kind': 'other', 'blocked': False, 'rate': 0.0, 'checked': 0, 'flagged': [], 'previews': [], 'error': 'not an image or video FaceFusion can read' }
		sys.stdout.write('\n' + RESULT_PREFIX + json.dumps(result) + '\n')
		sys.stdout.flush()
	return 0


if __name__ == '__main__':
	sys.exit(main())
