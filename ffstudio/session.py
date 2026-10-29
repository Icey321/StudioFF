import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from ffstudio.config import APP_DIR, DATA_DIR, PREVIEW_DIR, SCHEMA_FILE, AppConfig
from ffstudio.environment import create_process_environment, is_facefusion_root, probe_python_version, version_tuple
from ffstudio.media import GIF_EXTENSIONS, extension, gif_as_video, media_kind, read_facefusion_frame
from ffstudio.runner import PreviewWorker, ProcessRunner
from ffstudio.schema import Schema, build_option_args, schema_is_stale

MINIMUM_PYTHON = (3, 13)
# facefusion's video content check reports its running result as "rate = <percent>", above 10 the target is blocked
CONTENT_RATE_PATTERN = re.compile(r'rate = ([\d.]+)')
CONTENT_RATE_LIMIT = 10.0
# content_check.py prints one of these per target
CONTENT_RESULT_PREFIX = 'CONTENT_RESULT '
# what preview_worker.py prints once it has started
WORKER_READY_LINE = 'Preview worker ready'
CONTENT_CHECK_DIR = os.path.join(PREVIEW_DIR, 'nsfw_check')
# the video a gif target is previewed from, the same one gif_run.py makes for a whole run
GIF_VIDEO_DIR = os.path.join(PREVIEW_DIR, 'gif_video')
# the last line of a python traceback, an error facefusion logs itself, or a command line mistake
EXCEPTION_PATTERN = re.compile(r'^[\w.]*(Error|Exception)\b(:|$)')
FACEFUSION_ERROR_PATTERN = re.compile(r'^\[FACEFUSION\.[\w.]+\] .*\b(failed|not found|not installed|not supported|could not be loaded|not implemented)\b')
USAGE_ERROR_PATTERN = re.compile(r'^\S+\.py: error: ')
# what a preview takes before there is a time of its own to go by, a warm one runs on models the worker already loaded
PREVIEW_SECONDS = { 'preview_seconds': 6.0, 'preview_seconds_warm': 2.0, 'batch_preview_seconds': 5.0, 'batch_preview_seconds_warm': 1.5 }


def is_error_line(line : str) -> bool:
	return bool(EXCEPTION_PATTERN.match(line) or FACEFUSION_ERROR_PATTERN.match(line) or USAGE_ERROR_PATTERN.match(line))


def explain_error(line : str) -> Optional[str]:
	"""a plain hint for the errors people can fix themselves"""
	lowered = line.lower()
	if line.startswith('PermissionError') or '[errno 13]' in lowered:
		return 'Windows did not allow saving there. Choose another output folder, such as Videos or Documents, or close the file if it is open in another app.'
	if '[errno 28]' in lowered or 'no space left' in lowered:
		return 'The drive is full. Free some space or choose an output folder on another drive.'
	if 'out of memory' in lowered or 'failed to allocate' in lowered or 'bad allocation' in lowered or line.startswith('MemoryError'):
		return 'Ran out of memory. Try fewer execution threads, a smaller video or closing other apps.'
	return None


def folder_is_writable(folder : str) -> bool:
	try:
		os.makedirs(folder, exist_ok = True)
		# one try, tempfile.mkstemp retries thousands of times on windows when access is denied
		path = os.path.join(folder, '.ffstudio-%s.tmp' % uuid.uuid4().hex)
		with open(path, 'xb'):
			pass
		os.remove(path)
		return True
	except OSError:
		return False


def load_values(schema : Optional[Schema], stored : Dict[str, Any]) -> Dict[str, Any]:
	"""a value for every option, the stored one unless it no longer exists in the installed facefusion"""
	values : Dict[str, Any] = {}
	if schema:
		for dest, option in schema.options.items():
			values[dest] = stored.get(dest, option.get('default'))
		for dest in list(values):
			resolved = schema.resolve(dest, values)
			value = values.get(dest)
			if resolved.get('choices') and value is not None and not resolved.get('multiple'):
				if str(value) not in [ str(choice) for choice in resolved.get('choices') ]:
					values[dest] = resolved.get('default')
	return values


class Session(QObject):
	"""shared state between pages: config, option schema, option values and the process runner"""
	# where the order of the processor buttons is kept
	ORDER_KEY = 'processor_order'
	schema_changed = Signal()
	schema_loading = Signal(bool)
	value_changed = Signal(str, object)
	favorites_changed = Signal()
	# a preset was saved or deleted, every page's preset list shows it
	presets_changed = Signal()
	busy_changed = Signal(bool, str)
	notify = Signal(str, str, str)
	# owner, one result per target, exit code
	content_checked = Signal(str, list, int)

	def __init__(self) -> None:
		super().__init__()
		self.config = AppConfig()
		self.schema : Optional[Schema] = Schema.load()
		self.values : Dict[str, Any] = {}
		self.runner = ProcessRunner(self)
		# keeps the models of the last preview loaded for the next one
		self.preview_worker = PreviewWorker(self)
		self.preview_worker.set_idle_minutes(int(self.config.get('preview_keep_loaded_minutes')))
		self.preview_warm = False
		self.task = ''
		# a preview shows its progress on its own page's preview card, not in Studio's run bar
		self.task_is_preview = False
		self.python_version : Optional[str] = None
		self.schema_runner = ProcessRunner(self)
		self.schema_runner.finished.connect(self.on_schema_finished)
		self.schema_log : List[str] = []
		self.schema_runner.log.connect(self.schema_log.append)
		self.runner.finished.connect(self.on_runner_finished)
		self.runner.started.connect(self.on_runner_started)
		self.runner.progress.connect(self.on_runner_progress)
		self.runner.log.connect(self.on_runner_log)
		self.facefusion_running = False
		# the output file of the running facefusion, its temp folder is named after it
		self.facefusion_output = ''
		# what the current run said, used to explain a failure that facefusion does not explain itself
		self.content_rate = 0.0
		self.run_messages : List[str] = []
		# the page that started the running nsfw check, and what it found so far
		self.content_check_owner = ''
		self.content_results : List[Dict[str, Any]] = []
		self.init_values()

	# configuration

	@property
	def facefusion_root(self) -> str:
		return self.config.get('facefusion_root') or ''

	@property
	def python_path(self) -> str:
		return self.config.get('python_path') or ''

	def is_configured(self) -> bool:
		return is_facefusion_root(self.facefusion_root) and os.path.isfile(self.python_path)

	def facefusion_video(self, path : str) -> str:
		"""the video facefusion runs on for a gif (made once and kept, see gif_run.py), the path itself for any other file,
		empty when the gif cannot be turned into one"""
		if extension(path) not in GIF_EXTENSIONS:
			return path
		ffmpeg = shutil.which('ffmpeg', path = create_process_environment(self.python_path).value('PATH'))
		return (gif_as_video(ffmpeg, path, GIF_VIDEO_DIR) or '') if ffmpeg else ''

	def run_frame(self, path : str, frame_index : int, shown_frame : Optional[Any]) -> Optional[Any]:
		"""the frame a preview runs on: a video or gif frame as facefusion itself reads it in a whole run, so the preview's
		nsfw verdict matches the run's. the shown frame when that cannot be read, or when it comes out another shape,
		such as a turned phone video that facefusion's reader would scramble"""
		if media_kind(path) != 'video' or not os.path.isfile(self.python_path):
			return shown_frame
		search_path = create_process_environment(self.python_path).value('PATH')
		frame = read_facefusion_frame(path, frame_index, search_path, GIF_VIDEO_DIR)
		if frame is None:
			return shown_frame
		if shown_frame is not None:
			# a gif's video is padded to even sizes
			if any(abs(size - shown_size) > 1 for size, shown_size in zip(frame.shape[:2], shown_frame.shape[:2])):
				return shown_frame
		return frame

	def check_python(self) -> Optional[str]:
		self.python_version = probe_python_version(self.python_path) if os.path.isfile(self.python_path) else None
		return self.python_version

	def python_supported(self) -> bool:
		return version_tuple(self.python_version) >= MINIMUM_PYTHON

	def save(self) -> None:
		self.config.set('options', self.values)
		self.config.save()

	# option values

	def init_values(self) -> None:
		self.values = load_values(self.schema, self.config.get('options') or {})

	def set_value(self, dest : str, value : Any) -> None:
		self.values[dest] = value
		self.value_changed.emit(dest, value)

	def processors(self) -> List[str]:
		return list(self.values.get('processors') or [])

	# favorite options, shown again in their own box on the studio page

	def favorites(self) -> List[str]:
		return list(self.config.get('favorite_options') or [])

	def toggle_favorite(self, dest : str) -> None:
		favorites = self.favorites()
		if dest in favorites:
			favorites.remove(dest)
		else:
			favorites.append(dest)
		self.config.set('favorite_options', favorites)
		self.favorites_changed.emit()

	# schema

	def refresh_schema(self, force : bool = False) -> bool:
		if not self.is_configured():
			return False
		if not force and not schema_is_stale(self.schema, self.facefusion_root):
			return False
		if self.schema_runner.is_running():
			return True
		os.makedirs(DATA_DIR, exist_ok = True)
		self.schema_log.clear()
		script = os.path.join(APP_DIR, 'schema_dump.py')
		self.schema_loading.emit(True)
		self.schema_runner.start(self.python_path, [ script, self.facefusion_root, SCHEMA_FILE ], self.facefusion_root, create_process_environment(self.python_path))
		return True

	def on_schema_finished(self, exit_code : int, _ : float) -> None:
		self.schema_loading.emit(False)
		if exit_code == 0:
			self.schema = Schema.load()
			self.init_values()
			self.schema_changed.emit()
			self.notify.emit('success', 'Options loaded', 'FaceFusion %s · %d options' % (self.schema.version, len(self.schema.options)))
		else:
			tail = '\n'.join(self.schema_log[-6:])
			self.notify.emit('error', 'Could not read FaceFusion options', tail or 'exit code %d' % exit_code)

	# running facefusion

	def set_task(self, task : str, preview : bool = False) -> None:
		self.task = task
		self.task_is_preview = preview and bool(task)
		self.busy_changed.emit(bool(task), task)

	def run_facefusion(self, task : str, command_args : List[str], script : str = 'facefusion.py', preview : bool = False, keep_loaded : bool = False) -> bool:
		"""a preview, or a run that keeps its models loaded for the next one such as a batch's, runs on the preview worker,
		everything else as a process of its own"""
		if self.runner.is_running():
			self.notify.emit('warning', 'Busy', 'FaceFusion is already running: ' + self.task)
			return False
		if not self.is_configured():
			self.notify.emit('error', 'FaceFusion not set up', 'Choose the FaceFusion folder and Python environment in Settings.')
			return False
		self.save()
		# before set_task, the pages start their preview estimate when they hear that facefusion is busy
		self.preview_warm = preview and self.preview_worker.is_running()
		self.set_task(task, preview)
		self.facefusion_running = True
		# gif_run.py hands facefusion an output.mp4 of its own
		self.facefusion_output = 'output.mp4' if os.path.basename(script) == 'gif_run.py' else next((command_args[index + 1] for index in reversed(range(len(command_args) - 1)) if command_args[index] in [ '-o', '--output-path' ]), '')
		environment = create_process_environment(self.python_path)
		if (preview or keep_loaded) and os.path.basename(script) in [ 'facefusion.py', 'gif_run.py' ] and self.preview_worker.is_usable():
			# Stop on a whole run quits the worker, it would go on making the run unseen
			return self.runner.start_on_worker(self.preview_worker, self.python_path, [ script ] + command_args, self.facefusion_root, environment, keeps_on_stop = preview)
		if not preview:
			# a whole run needs the memory the preview models take
			self.preview_worker.stop()
		return self.runner.start(self.python_path, [ script ] + command_args, self.facefusion_root, environment)

	def preview_key(self, name : str) -> str:
		"""the name the time of the preview that just started is kept under, previews on loaded models take far less"""
		return name + '_warm' if self.preview_warm else name

	def expected_preview_seconds(self, key : str) -> float:
		return float(self.config.get(key) or PREVIEW_SECONDS.get(key) or PREVIEW_SECONDS.get('preview_seconds'))

	def remember_preview_seconds(self, key : str, elapsed : float) -> None:
		# a running average, so one slow preview (a model download) does not throw the estimate off for good
		stored = self.config.get(key)
		self.config.set(key, round(elapsed if not stored else 0.6 * stored + 0.4 * elapsed, 2))

	def on_runner_started(self, _ : str) -> None:
		self.content_rate = 0.0
		self.run_messages = []

	def on_runner_progress(self, title : str, _ : float, status : str) -> None:
		match = CONTENT_RATE_PATTERN.search(status) if title == 'analysing' else None
		if match:
			# the last rate decides, the running one starts high when an early checked frame is flagged
			self.content_rate = float(match.group(1))

	def on_runner_log(self, line : str) -> None:
		if line.startswith(CONTENT_RESULT_PREFIX):
			try:
				self.content_results.append(json.loads(line[len(CONTENT_RESULT_PREFIX):]))
			except ValueError:
				pass
		# a preview that starts the worker gets its ready line too, it says nothing about the run
		elif not re.match(r'\[FACEFUSION\.\w+\] processing step \d+ of \d+$', line) and line != WORKER_READY_LINE:
			self.run_messages.append(line)

	def run_error(self) -> Optional[str]:
		"""the line that best explains why the last run failed, a python exception wins over facefusion's own summary"""
		errors = [ line for line in self.run_messages if is_error_line(line) ]
		exceptions = [ line for line in errors if EXCEPTION_PATTERN.match(line) ]
		return (exceptions or errors or [ None ])[-1]

	def describe_run_error(self) -> Tuple[str, str]:
		"""a short status and a notification text for a failed run"""
		line = self.run_error()
		if not line:
			return 'Failed', 'See Console for the full log.'
		hint = explain_error(line)
		short = line if len(line) <= 160 else line[:157] + '…'
		return 'Failed: ' + (hint.split('.')[0] if hint else short), (hint + '\n' if hint else '') + short + '\nSee Console for the full log.'

	def nsfw_block_message(self, image_target : bool) -> Optional[str]:
		"""explains a failed run that was stopped by facefusion's nsfw content check

		facefusion exits with code 1 and prints no error when its content check blocks a target.
		a video shows the check's rate in its progress line, which is proof enough,
		an image shows nothing at all, so a failure without any message only suggests a block
		and the caller confirms it with run_content_check
		"""
		if self.content_rate > CONTENT_RATE_LIMIT:
			return "FaceFusion's NSFW filter found explicit content in %.0f%% of the checked frames (the limit is 10%%) and refused to process this video." % min(self.content_rate, 100)
		if image_target and not self.run_messages:
			return "FaceFusion stopped without giving a reason, which usually means its NSFW filter refused the target image."
		return None

	def nsfw_block_is_certain(self) -> bool:
		return self.content_rate > CONTENT_RATE_LIMIT

	def run_content_check(self, owner : str, targets : List[str], trim_start : Optional[int] = None, trim_end : Optional[int] = None) -> bool:
		"""runs facefusion's own nsfw check on the targets without processing them, content_checked reports back to the owner"""
		if not self.is_configured():
			self.notify.emit('error', 'FaceFusion not set up', 'Choose the FaceFusion folder and Python environment in Settings.')
			return False
		if self.runner.is_running():
			self.notify.emit('warning', 'Busy', 'Already running: ' + self.task)
			return False
		shutil.rmtree(CONTENT_CHECK_DIR, ignore_errors = True)
		self.content_check_owner = owner
		self.content_results = []
		providers = ','.join(self.values.get('execution_providers') or [])
		device_ids = ','.join(str(device_id) for device_id in self.values.get('execution_device_ids') or [])
		trim = [ str(trim_start if trim_start is not None else -1), str(trim_end if trim_end is not None else -1) ]
		args = [ os.path.join(APP_DIR, 'content_check.py'), CONTENT_CHECK_DIR, providers, device_ids ] + trim + [ '--' ] + targets
		task = 'NSFW check: ' + (os.path.basename(targets[0]) if len(targets) == 1 else '%d targets' % len(targets))
		return self.run_python(task, args, self.facefusion_root)

	def on_runner_finished(self, exit_code : int, _ : float) -> None:
		if self.facefusion_running and exit_code != 0:
			self.clear_facefusion_temp()
		self.facefusion_running = False
		self.set_task('')
		if self.content_check_owner:
			owner, self.content_check_owner = self.content_check_owner, ''
			self.content_checked.emit(owner, list(self.content_results), exit_code)

	def facefusion_temp_dir(self) -> str:
		# mirrors facefusion: <temp_path>/<sha1 of the machine id>/facefusion/<output name>/
		temp_path = self.values.get('temp_path') or tempfile.gettempdir()
		return os.path.join(temp_path, hashlib.sha1(str(uuid.getnode()).encode()).hexdigest(), 'facefusion')

	def clear_facefusion_temp(self) -> None:
		# facefusion removes its extracted frames itself, except when Stop kills it or it crashes
		# only the folder of this run, other facefusion instances share the parent folder
		name = os.path.splitext(os.path.basename(self.facefusion_output))[0]
		if name:
			shutil.rmtree(os.path.join(self.facefusion_temp_dir(), name), ignore_errors = True)

	def clear_preview_files(self) -> None:
		shutil.rmtree(PREVIEW_DIR, ignore_errors = True)

	def run_python(self, task : str, args : List[str], working_dir : str, python_path : Optional[str] = None, preview : bool = False) -> bool:
		if self.runner.is_running():
			self.notify.emit('warning', 'Busy', 'Already running: ' + self.task)
			return False
		python_path = python_path or self.python_path
		self.set_task(task, preview)
		return self.runner.start(python_path, args, working_dir, create_process_environment(python_path))

	def run_program(self, task : str, program : str, args : List[str], working_dir : str, environment = None) -> bool:
		if self.runner.is_running():
			self.notify.emit('warning', 'Busy', 'Already running: ' + self.task)
			return False
		self.set_task(task)
		return self.runner.start(program, args, working_dir, environment)

	def option_args(self, values : Optional[Dict[str, Any]] = None, groups : Optional[List[str]] = None) -> List[str]:
		if not self.schema:
			return []
		values = values if values is not None else self.values
		args = [ '--processors' ] + list(values.get('processors') or self.schema.options.get('processors', {}).get('default') or [])
		return args + build_option_args(self.schema, values, groups)


class OptionStore(QObject):
	"""a page's own set of option values, shown and edited like the session's but kept apart from them

	option panels take it in place of the session, favorites and the schema stay shared
	"""
	value_changed = Signal(str, object)
	# the values were loaded again, panels rebuild their rows
	schema_changed = Signal()

	def __init__(self, session : Session, values_key : str, order_key : str) -> None:
		super().__init__(session)
		self.session = session
		self.config = session.config
		self.values_key = values_key
		self.ORDER_KEY = order_key
		self.favorites_changed = session.favorites_changed
		self.values : Dict[str, Any] = {}
		self.init_values()
		session.schema_changed.connect(self.on_schema_changed)

	@property
	def schema(self) -> Optional[Schema]:
		return self.session.schema

	def init_values(self) -> None:
		# the first time they start as a copy of the session's values
		stored = self.config.get(self.values_key)
		self.load(stored if stored is not None else self.session.values)

	def load(self, values : Dict[str, Any]) -> None:
		# rows keep a reference to this dict, so it is filled again rather than replaced
		self.values.clear()
		self.values.update(load_values(self.schema, values))
		self.config.set(self.values_key, dict(self.values))

	def on_schema_changed(self) -> None:
		self.init_values()
		self.schema_changed.emit()

	def copy_from(self, values : Dict[str, Any], order : List[str]) -> None:
		self.load(values)
		self.config.set(self.ORDER_KEY, list(order))
		self.schema_changed.emit()

	def set_value(self, dest : str, value : Any) -> None:
		self.values[dest] = value
		self.config.set(self.values_key, dict(self.values))
		self.value_changed.emit(dest, value)

	def processors(self) -> List[str]:
		return list(self.values.get('processors') or [])

	def favorites(self) -> List[str]:
		return self.session.favorites()

	def toggle_favorite(self, dest : str) -> None:
		self.session.toggle_favorite(dest)


def app_python() -> str:
	return sys.executable
