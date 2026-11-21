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
# how many of a video's checked frames that passed, the closest calls, get a picture
MAX_NEAR = 3
BAR_WIDTH = 30
# each detector's score as facefusion's detect_with_nsfw_N works it out from its model's output,
# the line is matched against facefusion's own source before it is used, the limit is read from there
DETECTORS = [
	('nsfw_1', 'detection_score = numpy.max(numpy.amax(detection[:, 4:], axis = 1))', lambda detection: numpy_max(detection)),
	('nsfw_2', 'detection_score = detection[0] - detection[1]', lambda detection: detection[0] - detection[1]),
	('nsfw_3', 'detection_score = (detection[2] + detection[3]) - (detection[0] + detection[1])', lambda detection: (detection[2] + detection[3]) - (detection[0] + detection[1]))
]
LIMIT_PATTERN = r'return bool\(detection_score > ([0-9.]+)\)'


def numpy_max(detection) -> float:
	import numpy

	return numpy.max(numpy.amax(detection[:, 4:], axis = 1))


def read_limits() -> list:
	"""each detector's limit, read from facefusion's own code. empty when that code no longer works out
	the score the way DETECTORS does, the check then falls back to facefusion's vote alone, without scores"""
	import inspect
	import re

	from facefusion import content_analyser

	limits = []
	for model_name, score_line, _ in DETECTORS:
		source = inspect.getsource(getattr(content_analyser, 'detect_with_' + model_name))
		match = re.search(LIMIT_PATTERN, source)
		if score_line not in source or not match:
			return []
		limits.append(float(match.group(1)))
	# the vote, two of three, must be facefusion's too
	vote = inspect.getsource(content_analyser.detect_nsfw)
	if 'is_nsfw_1 and is_nsfw_2 or is_nsfw_1 and is_nsfw_3 or is_nsfw_2 and is_nsfw_3' not in vote:
		return []
	return limits


def detector_shares(frame, limits : list) -> list:
	"""each detector's score as a share of its limit, above 1 means it flags the frame.
	the same models and the same frame preparation as facefusion's detect_with_nsfw_N"""
	from facefusion import content_analyser

	shares = []
	for (model_name, _, score), limit in zip(DETECTORS, limits):
		detection = content_analyser.forward_nsfw(content_analyser.prepare_detect_frame(frame, model_name), model_name)
		value = float(score(detection))
		# facefusion flags on value > limit, the share keeps that exact edge
		shares.append(round(value / limit, 4) if value <= limit else max(round(value / limit, 4), 1.0001))
	return shares


def is_flagged(shares : list) -> bool:
	"""facefusion's vote: two of the three detectors past their limit"""
	return sum(share > 1 for share in shares) >= 2


def closeness(shares : list) -> float:
	"""how near a frame that passed came to being flagged: the second highest share, two must pass their limit"""
	return sorted(shares)[-2]


def analyse(frame, limits : list):
	"""whether facefusion flags the frame, and each detector's share when the scores can be read"""
	from facefusion import content_analyser
	from facefusion.vision import is_vision_frame

	if not limits:
		return content_analyser.analyse_frame(frame), None
	if not is_vision_frame(frame):
		return False, None
	shares = detector_shares(frame, limits)
	return is_flagged(shares), shares


def print_progress(title : str, done : int, total : int, status : str) -> None:
	# the same shape as facefusion's own bar, so the ui parses it the same way
	filled = int(BAR_WIDTH * done / total) if total else 0
	# the closing erase-line code ends the status, the ui would read on into the next line without it
	sys.stdout.write('\x1b[G\x1b[K\x1b[38;5;250mcli.0   •\x1b[0m \x1b[38;5;244manalyse       \x1b[0m  %s \x1b[38;5;250m%s\x1b[0m\x1b[38;5;244m%s\x1b[0m %s\x1b[G\x1b[K' % (title, '=' * filled, '=' * (BAR_WIDTH - filled), status))
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


def check_image(target_path : str, preview_dir : str, index : int, limits : list) -> dict:
	from facefusion import content_analyser
	from facefusion.vision import read_image

	result = { 'path': target_path, 'kind': 'image', 'blocked': False, 'rate': 0.0, 'checked': 1, 'flagged': [], 'previews': [] }
	frame = read_image(target_path)
	if frame is None:
		result['error'] = 'could not read the image'
		return result
	# facefusion's own three detectors and vote, as detect_nsfw runs them, keeping how many flagged the image
	if limits:
		shares = detector_shares(frame, limits)
		votes = sum(share > 1 for share in shares)
		result['scores'] = shares
	else:
		votes = sum([ content_analyser.detect_with_nsfw_1(frame), content_analyser.detect_with_nsfw_2(frame), content_analyser.detect_with_nsfw_3(frame) ])
	result.update({ 'votes': votes, 'rate': round(votes / 3 * 100, 1) })
	if votes >= 2:
		result.update({ 'blocked': True, 'flagged': [ 0 ], 'previews': [ save_preview(preview_dir, index, 'image', frame) ] })
	return result


def check_video(target_path : str, preview_dir : str, index : int, trim_start : int, trim_end : int, limits : list) -> dict:
	"""the same frames, the same reader and the same count as facefusion's content_analyser.analyse_video,
	only noting which frames were flagged. opencv decodes a little differently, enough to flip a borderline frame"""
	from facefusion import content_store, ffprobe, video_manager
	from facefusion.vision import restrict_video_range

	# flagged_scores goes with flagged, near holds the closest frames that passed
	result = { 'path': target_path, 'kind': 'video', 'blocked': False, 'rate': 0.0, 'checked': 0, 'flagged': [], 'previews': [], 'fps': 0.0, 'flagged_scores': [], 'near': [] }
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
			flagged, shares = analyse(frame, limits)
			if flagged:
				content_store.set_hit()
				result['flagged'].append(frame_index)
				result['flagged_scores'].append(shares)
				if len(result['previews']) < MAX_PREVIEWS:
					result['previews'].append(save_preview(preview_dir, index, str(frame_index), frame))
			elif shares:
				keep_near(result['near'], frame_index, shares, frame, preview_dir, index)
		print_progress('checking ' + name, frame_index - start + 1, end - start, 'rate = %.1f' % content_store.calculate_rate(STEP))
	# closes the reader's ffmpeg, a batch would otherwise leave one running per video
	video_manager.clear()
	result['rate'] = round(content_store.calculate_rate(STEP), 1)
	result['blocked'] = content_store.calculate_rate(STEP) > LIMIT
	return result


def keep_near(near : list, frame_index : int, shares : list, frame, preview_dir : str, index : int) -> None:
	"""keeps the MAX_NEAR closest calls among the frames that passed, closest first, with a picture each"""
	value = closeness(shares)
	if len(near) >= MAX_NEAR and value <= closeness(near[-1]['scores']):
		return
	near.append({ 'frame': frame_index, 'scores': shares, 'preview': save_preview(preview_dir, index, 'near_%d' % frame_index, frame) })
	near.sort(key = lambda item: closeness(item['scores']), reverse = True)
	for dropped in near[MAX_NEAR:]:
		if os.path.isfile(dropped['preview']):
			os.remove(dropped['preview'])
	del near[MAX_NEAR:]


def check_gif(target_path : str, preview_dir : str, index : int, trim_start : int, trim_end : int, limits : list) -> dict:
	"""studio runs a gif as the video gif_run.py converts it to, so that video is what gets checked"""
	import shutil
	import subprocess

	from gif_run import gif_to_video_args

	video_path = os.path.join(preview_dir, 'gif_%d.mp4' % index)
	ffmpeg = shutil.which('ffmpeg')
	if not ffmpeg or subprocess.call([ ffmpeg, '-hide_banner', '-loglevel', 'error', '-y' ] + gif_to_video_args(target_path, video_path)):
		# ffmpeg may have written part of the video before it failed
		if os.path.isfile(video_path):
			os.remove(video_path)
		return { 'path': target_path, 'kind': 'video', 'blocked': False, 'rate': 0.0, 'checked': 0, 'flagged': [], 'previews': [], 'error': 'could not convert the gif to video' }
	try:
		result = check_video(video_path, preview_dir, index, trim_start, trim_end, limits)
	finally:
		if os.path.isfile(video_path):
			os.remove(video_path)
	result['path'] = target_path
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
	limits = read_limits()
	if not limits:
		print('This FaceFusion works out its NSFW scores differently, the check runs without them', flush = True)

	os.makedirs(preview_dir, exist_ok = True)
	for index, target_path in enumerate(targets):
		print('Checking %s (%d of %d)' % (os.path.basename(target_path), index + 1, len(targets)), flush = True)
		try:
			if is_image(target_path):
				result = check_image(target_path, preview_dir, index, limits)
			elif target_path.lower().endswith('.gif'):
				result = check_gif(target_path, preview_dir, index, int(trim_start), int(trim_end), limits)
			elif is_video(target_path):
				result = check_video(target_path, preview_dir, index, int(trim_start), int(trim_end), limits)
			else:
				result = { 'path': target_path, 'kind': 'other', 'blocked': False, 'rate': 0.0, 'checked': 0, 'flagged': [], 'previews': [], 'error': 'not an image or video FaceFusion can read' }
		except Exception as exception:
			# one target FaceFusion chokes on must not leave the ones after it unchecked, nor leave its video reader running
			try:
				video_manager.clear()
			except Exception:
				pass
			result = { 'path': target_path, 'kind': 'other', 'blocked': False, 'rate': 0.0, 'checked': 0, 'flagged': [], 'previews': [], 'error': '%s: %s' % (type(exception).__name__, exception) }
		sys.stdout.write('\n' + RESULT_PREFIX + json.dumps(result) + '\n')
		sys.stdout.flush()
	return 0


if __name__ == '__main__':
	sys.exit(main())
