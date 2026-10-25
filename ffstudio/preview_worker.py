"""
Runs inside the FaceFusion Python environment (NOT the UI environment).

Makes preview frames exactly the way `facefusion.py run` does, through the same code and the same
content check, but stays running, so the models the processors load stay loaded and the next preview
skips starting Python and loading them again.

Batch runs whole outputs on it the same way, so the models stay loaded from one output to the next.

usage: python preview_worker.py
	reads one request per line on stdin: {"id": 1, "script": "facefusion.py", "args": [<facefusion.py arguments, starting with run>]}
	with "script": "gif_run.py" the args are gif_run.py's, which converts the gif around a run made here
	prints facefusion's own log lines while it works, then one line per request: PREVIEW_DONE {"id": 1, "code": <exit code>}
	the exit code is the one facefusion.py would exit with. null means the worker itself failed and
	the caller makes the preview with facefusion.py instead, the worker quits after it
"""
import json
import os
import sys
import traceback

RESULT_PREFIX = 'PREVIEW_DONE '
# a preview is a single frame, the models it loads stay for the next one instead of being cleared after it
KEEP_MODELS_ARGS = [ '--video-memory-strategy', 'tolerant' ]


def reply(request_id : int, code) -> None:
	sys.stdout.write(RESULT_PREFIX + json.dumps({ 'id': request_id, 'code': code }) + '\n')
	sys.stdout.flush()


def forget_files() -> None:
	"""facefusion remembers what it read by file path, but the preview frame is written to the same path every time"""
	from facefusion import audio, config, content_analyser, face_store, ffprobe, vision

	for function in [ vision.read_static_image, vision.read_static_video_frame, content_analyser.analyse_image, content_analyser.analyse_video, ffprobe.extract_static_video_metadata, ffprobe.extract_static_audio_metadata, audio.read_static_audio, audio.read_static_voice, config.get_static_config_parser ]:
		function.cache_clear()
	face_store.clear()


def remember_checked_models() -> None:
	"""facefusion checks the hash of every model file before each run, which takes seconds. a file that passed and has not
	changed since passes again, so the worker checks it once. the nsfw models are still checked on every run"""
	from facefusion import content_analyser, download
	from facefusion.hash_helper import get_hash_path

	validate_hash = download.validate_hash
	passed = set()
	always_checked = set()

	def validate_hash_once(validate_path : str) -> bool:
		# their paths need the settings of a run, which are there by the time anything is checked
		if not always_checked:
			model_hash_set, model_source_set = content_analyser.collect_model_downloads()
			always_checked.update(os.path.normcase(os.path.abspath(file.get('path'))) for file in list(model_hash_set.values()) + list(model_source_set.values()))
		path = os.path.normcase(os.path.abspath(validate_path))
		if path in always_checked:
			return validate_hash(validate_path)
		try:
			key = tuple((stat.st_size, stat.st_mtime_ns) for stat in [ os.stat(validate_path), os.stat(get_hash_path(validate_path)) ]) + (path,)
		except (OSError, TypeError):
			return validate_hash(validate_path)
		if key in passed:
			return True
		if validate_hash(validate_path):
			passed.add(key)
			return True
		return False

	download.validate_hash = validate_hash_once


def run(argv : list) -> int:
	"""what facefusion's cli does for `run`, without exiting at the end"""
	from facefusion import logger, state_manager
	from facefusion.args_helper import apply_args
	from facefusion.core import process_headless
	from facefusion.jobs import job_manager
	from facefusion.program import create_program
	from facefusion.program_helper import validate_args

	forget_files()
	# some defaults depend on other options, such as the pixel boost on the swapper model, and facefusion reads those from sys.argv
	sys.argv[1:] = argv + KEEP_MODELS_ARGS
	program = create_program()
	if not validate_args(program):
		return 2
	try:
		args = vars(program.parse_args())
	except SystemExit as error:
		# argparse already printed what was wrong
		return error.code if isinstance(error.code, int) else 2
	if args.get('command') != 'run':
		print('The preview worker only makes runs', flush = True)
		return 2
	state_manager.clear()
	apply_args(args, state_manager.init_item)
	logger.init(state_manager.get_item('log_level'))
	if not job_manager.init_jobs(state_manager.get_jobs_path()):
		return 1
	return process_headless(args)


def run_request(request : dict) -> int:
	args = list(request.get('args') or [])
	if request.get('script') == 'gif_run.py':
		# it lives next to this script, which python put first on the path before main added the facefusion folder
		from gif_run import run_gif

		return run_gif(args, run)
	return run(args)


def main() -> int:
	# run from the facefusion folder, the script itself lives in the ui folder
	sys.path.insert(0, os.getcwd())
	# argparse names the program after sys.argv[0] in its errors
	sys.argv[0] = 'facefusion.py'

	from facefusion import content_store, core, face_store, inference_manager, process_manager, session_context, session_manager, state_manager, video_manager

	# the same setup as facefusion's own cli before it runs a command
	state_manager.init()
	if not core.pre_check():
		return 2
	session_manager.set_cli_session(session_context.get_session_id(), session_manager.create_cli_session())
	content_store.init()
	face_store.init()
	inference_manager.init()
	process_manager.init()
	video_manager.init()
	remember_checked_models()
	print('Preview worker ready', flush = True)

	for line in sys.stdin:
		if not line.strip():
			continue
		try:
			request = json.loads(line)
		except ValueError:
			continue
		try:
			code = run_request(request)
		except BaseException:
			# facefusion may be left half way, a fresh worker starts next time
			traceback.print_exc()
			sys.stderr.flush()
			reply(request.get('id'), None)
			return 1
		reply(request.get('id'), code)
	return 0


if __name__ == '__main__':
	sys.exit(main())
