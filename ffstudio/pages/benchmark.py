import ctypes
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QStandardPaths, QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QTableWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, CheckBox, ComboBox, FlowLayout, FluentIcon, PrimaryPushButton, ProgressBar, PushButton, SingleDirectionScrollArea, StrongBodyLabel, SubtitleLabel, TableWidget

from ffstudio.benchmark_report import RESOLUTIONS, format_processors, render_report
from ffstudio.environment import CREATE_NO_WINDOW, create_process_environment, read_facefusion_version
from ffstudio.pages.setup import form_row, section_card
from ffstudio.session import Session
from ffstudio.widgets import NumberBox, guard_wheel

MODES = { 'warm': 'Warm — models are loaded before timing starts', 'cold': 'Cold — caches are cleared before every cycle' }
DEFAULTS : Dict[str, Any] = { 'processors': [ 'face_swapper' ], 'resolutions': [ '240p', '540p', '1080p' ], 'mode': 'warm', 'cycles': 3, 'provider': None, 'threads': None }
# facefusion prints its result table as | target_path | cycle_count | average_run | fastest_run | slowest_run | relative_fps |
ROW_PATTERN = re.compile(r'\|\s*(?P<target>\S*target-(?P<resolution>\d+p)\.\w+)\s*\|\s*(?P<cycles>\d+)\s*\|\s*(?P<average>[\d.]+)\s*\|\s*(?P<fastest>[\d.]+)\s*\|\s*(?P<slowest>[\d.]+)\s*\|\s*(?P<fps>[\d.]+)\s*\|')
COLUMNS = [ 'When', 'Resolution', 'FPS', 'Average (sec)', 'Fastest (sec)', 'Slowest (sec)', 'Processors', 'Provider', 'Threads', 'Mode', 'Cycles' ]
NUMBER_COLUMNS = [ 'FPS', 'Average (sec)', 'Fastest (sec)', 'Slowest (sec)', 'Threads', 'Cycles' ]
COLUMN_TIPS =\
{
	'FPS': 'Frames processed per second over all timed cycles. Higher is faster.',
	'Average (sec)': 'Average time in seconds for one complete run of the example video: reading frames, the processors and writing the video.',
	'Fastest (sec)': 'Quickest single cycle, in seconds.',
	'Slowest (sec)': 'Slowest single cycle, in seconds.',
	'Threads': 'Execution thread count, how many frames are processed in parallel.',
	'Cycles': 'Timed runs per resolution.'
}
# where each stage of one run sits within that run on the overall progress bar
STAGE_SPAN = { 'Checking content': (0.0, 0.03), 'Extracting frames': (0.03, 0.06), 'Processing frames': (0.06, 0.92), 'Merging video': (0.92, 0.97), 'Adding audio': (0.97, 1.0) }
HISTORY_LIMIT = 200


class MemoryStatus(ctypes.Structure):
	_fields_ = [ ('length', ctypes.c_ulong), ('memory_load', ctypes.c_ulong), ('total_physical', ctypes.c_ulonglong), ('available_physical', ctypes.c_ulonglong), ('total_page_file', ctypes.c_ulonglong), ('available_page_file', ctypes.c_ulonglong), ('total_virtual', ctypes.c_ulonglong), ('available_virtual', ctypes.c_ulonglong), ('available_extended_virtual', ctypes.c_ulonglong) ]


def read_cpu_name() -> str:
	try:
		import winreg
		with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
			return str(winreg.QueryValueEx(key, 'ProcessorNameString')[0]).strip()
	except OSError:
		return ''


def read_memory_total() -> int:
	try:
		status = MemoryStatus()
		status.length = ctypes.sizeof(MemoryStatus)
		ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
		return int(status.total_physical)
	except (AttributeError, OSError):
		return 0


def format_duration(seconds : float) -> str:
	seconds = int(seconds)
	if seconds >= 3600:
		return '%d:%02d:%02d' % (seconds // 3600, seconds // 60 % 60, seconds % 60)
	return '%d:%02d' % (seconds // 60, seconds % 60)


class HardwareProbe(QThread):
	"""collects gpu, cpu, memory and onnxruntime details without blocking the window"""
	probed = Signal(dict)

	def __init__(self, python_path : str) -> None:
		super().__init__()
		self.python_path = python_path

	def run(self) -> None:
		info : Dict[str, str] = {}
		cpu = read_cpu_name()
		info['CPU'] = '%s · %d threads' % (cpu or 'Unknown', os.cpu_count() or 0)
		memory = read_memory_total()
		info['Memory'] = '%.0f GB' % (memory / 1073741824) if memory else 'Unknown'
		info['GPU'] = 'No NVIDIA GPU found'
		if shutil.which('nvidia-smi'):
			try:
				output = subprocess.run([ 'nvidia-smi', '--query-gpu=name,memory.total,driver_version', '--format=csv,noheader,nounits' ], capture_output = True, text = True, timeout = 10, creationflags = CREATE_NO_WINDOW).stdout
				gpus = []
				for line in output.strip().splitlines():
					name, vram, driver = [ part.strip() for part in line.split(',')[:3] ]
					gpus.append('%s · %.0f GB · driver %s' % (name, float(vram) / 1024, driver))
				info['GPU'] = '\n'.join(gpus) or info['GPU']
			except (OSError, ValueError, subprocess.SubprocessError):
				pass
		if os.path.isfile(self.python_path):
			environment = create_process_environment(self.python_path)
			script = 'import onnxruntime; print(onnxruntime.__version__); print(", ".join(provider.replace("ExecutionProvider", "").replace("Tensorrt", "TensorRT") for provider in onnxruntime.get_available_providers()))'
			try:
				output = subprocess.run([ self.python_path, '-c', script ], capture_output = True, text = True, timeout = 60, creationflags = CREATE_NO_WINDOW, env = { key: environment.value(key) for key in environment.keys() }).stdout.strip().splitlines()
				if len(output) >= 2:
					info['ONNX Runtime'] = '%s · %s' % (output[0], output[1])
			except (OSError, subprocess.SubprocessError):
				pass
		self.probed.emit(info)


class BenchmarkPage(QWidget):
	def __init__(self, session : Session, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.setObjectName('benchmark')
		self.session = session
		self.running = False
		self.rows : List[Dict[str, Any]] = []
		self.run_settings : Dict[str, Any] = {}
		self.probe : Optional[HardwareProbe] = None
		self.probed = False
		self.hardware : Dict[str, str] = {}
		# progress of the current benchmark, facefusion runs every resolution (warm up +) cycles times
		self.run_total = 0
		self.runs_done = 0
		self.stage = ''
		self.stage_ratio = 0.0
		self.stage_status = ''
		self.started_at = 0.0
		self.elapsed_timer = QTimer(self)
		self.elapsed_timer.setInterval(1000)
		self.elapsed_timer.timeout.connect(self.refresh_status)
		stored = dict(DEFAULTS)
		stored.update(session.config.get('benchmark') or {})

		root = QVBoxLayout(self)
		root.setContentsMargins(24, 16, 24, 16)
		root.setSpacing(12)
		root.addWidget(SubtitleLabel('Benchmark', self))
		scroll = SingleDirectionScrollArea(self, Qt.Orientation.Vertical)
		scroll.setWidgetResizable(True)
		scroll.enableTransparentBackground()
		content = QWidget()
		content.setStyleSheet('background: transparent')
		layout = QVBoxLayout(content)
		layout.setContentsMargins(0, 0, 10, 0)
		layout.setSpacing(12)
		scroll.setWidget(content)
		root.addWidget(scroll, 1)

		# hardware
		hardware_card, hardware_layout = section_card('Hardware', '', content)
		self.hardware_layout = QVBoxLayout()
		self.hardware_layout.setSpacing(6)
		hardware_layout.addLayout(self.hardware_layout)
		self.hardware_labels : Dict[str, BodyLabel] = {}
		for name in [ 'GPU', 'CPU', 'Memory', 'ONNX Runtime' ]:
			value = BodyLabel('…', hardware_card)
			value.setWordWrap(True)
			value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
			self.hardware_labels[name] = value
			self.hardware_layout.addLayout(form_row(name, [ value ], hardware_card))
		layout.addWidget(hardware_card)

		# settings
		card, form = section_card('Test', 'Runs FaceFusion\'s own benchmark on its example videos and times the whole run, including reading frames, the processors and writing the video. Models and face settings come from Studio. The first run downloads all example videos (240p to 2160p) into the FaceFusion folder.', content)
		self.processor_widget = QWidget(card)
		self.processor_flow = FlowLayout(self.processor_widget, needAni = False)
		self.processor_flow.setContentsMargins(0, 0, 0, 0)
		self.processor_flow.setHorizontalSpacing(16)
		self.processor_flow.setVerticalSpacing(6)
		self.processor_boxes : Dict[str, CheckBox] = {}
		self.selected_processors = list(stored.get('processors') or [])
		form.addLayout(form_row('Processors', [ self.processor_widget ], card))

		resolution_widget = QWidget(card)
		resolution_flow = FlowLayout(resolution_widget, needAni = False)
		resolution_flow.setContentsMargins(0, 0, 0, 0)
		resolution_flow.setHorizontalSpacing(16)
		resolution_flow.setVerticalSpacing(6)
		self.resolution_boxes : Dict[str, CheckBox] = {}
		for resolution in RESOLUTIONS:
			box = CheckBox(resolution, resolution_widget)
			box.setChecked(resolution in stored.get('resolutions'))
			box.stateChanged.connect(self.save_settings)
			resolution_flow.addWidget(box)
			self.resolution_boxes[resolution] = box
		form.addLayout(form_row('Resolutions', [ resolution_widget ], card))

		self.mode_combo = ComboBox(card)
		self.mode_combo.setFixedWidth(420)
		for mode, text in MODES.items():
			self.mode_combo.addItem(text, userData = mode)
		self.mode_combo.setCurrentIndex(max(0, list(MODES).index(stored.get('mode')) if stored.get('mode') in MODES else 0))
		self.mode_combo.currentIndexChanged.connect(self.save_settings)
		form.addLayout(form_row('Mode', [ self.mode_combo ], card))

		self.cycle_spin = NumberBox(card)
		self.cycle_spin.setRange(1, 10)
		self.cycle_spin.setValue(int(stored.get('cycles') or 3))
		self.cycle_spin.setFixedWidth(96)
		self.cycle_spin.valueChanged.connect(self.save_settings)
		guard_wheel(self.cycle_spin)
		form.addLayout(form_row('Cycles', [ self.cycle_spin, self.hint('timed runs per resolution, the table shows their average', card) ], card))

		self.thread_spin = NumberBox(card)
		self.thread_spin.setRange(1, 32)
		self.thread_spin.setFixedWidth(96)
		self.stored_threads = stored.get('threads')
		self.thread_spin.valueChanged.connect(self.save_settings)
		guard_wheel(self.thread_spin)
		self.thread_hint = self.hint('', card)
		form.addLayout(form_row('Threads', [ self.thread_spin, self.thread_hint ], card))

		self.provider_combo = ComboBox(card)
		self.provider_combo.setFixedWidth(420)
		self.provider_combo.currentIndexChanged.connect(self.save_settings)
		self.stored_provider = stored.get('provider')
		form.addLayout(form_row('Execution provider', [ self.provider_combo ], card))

		buttons = QHBoxLayout()
		buttons.addSpacing(138)
		self.start_button = PrimaryPushButton(FluentIcon.SPEED_HIGH, 'Run benchmark', card)
		self.start_button.clicked.connect(self.start)
		self.stop_button = PushButton(FluentIcon.CLOSE, 'Stop', card)
		self.stop_button.setEnabled(False)
		self.stop_button.clicked.connect(session.runner.stop)
		buttons.addWidget(self.start_button)
		buttons.addWidget(self.stop_button)
		buttons.addStretch(1)
		form.addLayout(buttons)
		progress_row = QHBoxLayout()
		progress_row.addSpacing(138)
		status = QVBoxLayout()
		status.setSpacing(4)
		self.status_label = StrongBodyLabel('', card)
		self.status_label.setWordWrap(True)
		self.detail_label = CaptionLabel('', card)
		self.detail_label.setWordWrap(True)
		self.progress = ProgressBar(card)
		self.progress.setRange(0, 1000)
		status.addWidget(self.status_label)
		status.addWidget(self.detail_label)
		status.addWidget(self.progress)
		progress_row.addLayout(status, 1)
		form.addLayout(progress_row)
		layout.addWidget(card)

		# results
		results_card, results_layout = section_card('Results', 'Higher FPS is faster. Times are in seconds. Results are kept, so you can compare providers, models and settings. Select rows to export only those.', content)
		header = QHBoxLayout()
		self.summary_label = BodyLabel('', results_card)
		header.addWidget(self.summary_label, 1)
		self.export_button = PushButton(FluentIcon.SHARE, 'Export results', results_card)
		self.export_button.setToolTip('Save the selected rows, or the latest run, as a JPG image for posting')
		self.export_button.clicked.connect(self.export_results)
		header.addWidget(self.export_button)
		clear_button = PushButton(FluentIcon.DELETE, 'Clear results', results_card)
		clear_button.clicked.connect(self.clear_history)
		header.addWidget(clear_button)
		results_layout.addLayout(header)
		self.table = TableWidget(results_card)
		self.table.setColumnCount(len(COLUMNS))
		for column, title in enumerate(COLUMNS):
			item = QTableWidgetItem(title)
			if title in COLUMN_TIPS:
				item.setToolTip(COLUMN_TIPS.get(title))
			self.table.setHorizontalHeaderItem(column, item)
		self.table.verticalHeader().hide()
		self.table.setBorderVisible(True)
		self.table.setBorderRadius(6)
		self.table.setEditTriggers(TableWidget.EditTrigger.NoEditTriggers)
		self.table.setSelectionBehavior(TableWidget.SelectionBehavior.SelectRows)
		self.table.setSelectionMode(TableWidget.SelectionMode.ExtendedSelection)
		self.table.setMinimumHeight(260)
		self.table.horizontalHeader().setStretchLastSection(False)
		results_layout.addWidget(self.table, 1)
		layout.addWidget(results_card, 1)

		session.schema_changed.connect(self.refresh_choices)
		session.value_changed.connect(self.refresh_thread_hint)
		session.busy_changed.connect(lambda busy, _: self.start_button.setEnabled(not busy))
		session.runner.progress.connect(self.on_progress)
		session.runner.log.connect(self.on_log)
		session.runner.finished.connect(self.on_finished)
		self.refresh_choices()
		self.refresh_table()

	def hint(self, text : str, parent : QWidget) -> CaptionLabel:
		label = CaptionLabel(text, parent)
		label.setTextColor('#707070', '#9a9a9a')
		return label

	def showEvent(self, event) -> None:
		super().showEvent(event)
		if not self.probed:
			self.probed = True
			self.probe = HardwareProbe(self.session.python_path)
			self.probe.probed.connect(self.on_probed)
			self.probe.start()

	def on_probed(self, info : Dict[str, str]) -> None:
		self.hardware = { name: info.get(name) or 'Unknown' for name in self.hardware_labels }
		for name, label in self.hardware_labels.items():
			label.setText(self.hardware.get(name))

	def studio_threads(self) -> int:
		option = self.session.schema.options.get('execution_thread_count') if self.session.schema else None
		return int(self.session.values.get('execution_thread_count') or (option or {}).get('default') or 8)

	def refresh_thread_hint(self, *_ : Any) -> None:
		self.thread_hint.setText('frames processed in parallel, Studio uses %d' % self.studio_threads())

	def refresh_choices(self) -> None:
		schema = self.session.schema
		self.processor_flow.takeAllWidgets()
		self.processor_boxes = {}
		for processor in (schema.processors if schema else []):
			box = CheckBox(processor.replace('_', ' '), self.processor_widget)
			box.setChecked(processor in self.selected_processors)
			box.stateChanged.connect(self.save_settings)
			self.processor_flow.addWidget(box)
			self.processor_boxes[processor] = box

		self.provider_combo.blockSignals(True)
		self.provider_combo.clear()
		providers = list((schema.options.get('execution_providers', {}).get('choices') if schema else None) or [ 'cpu' ])
		for provider in providers:
			self.provider_combo.addItem(provider, userData = provider)
		current = self.stored_provider or (list(self.session.values.get('execution_providers') or []) or [ None ])[0]
		self.provider_combo.setCurrentIndex(providers.index(current) if current in providers else 0)
		self.provider_combo.blockSignals(False)

		thread_range = ((schema.options.get('execution_thread_count') if schema else None) or {}).get('range') or {}
		self.thread_spin.blockSignals(True)
		self.thread_spin.setRange(int(thread_range.get('min') or 1), int(thread_range.get('max') or 32))
		self.thread_spin.setValue(int(self.stored_threads or self.studio_threads()))
		self.thread_spin.blockSignals(False)
		self.refresh_thread_hint()

	def settings(self) -> Dict[str, Any]:
		return\
		{
			'processors': [ name for name, box in self.processor_boxes.items() if box.isChecked() ],
			'resolutions': [ name for name, box in self.resolution_boxes.items() if box.isChecked() ],
			'mode': self.mode_combo.currentData() or 'warm',
			'cycles': self.cycle_spin.value(),
			'threads': self.thread_spin.value(),
			'provider': self.provider_combo.currentData()
		}

	def save_settings(self, *_ : Any) -> None:
		settings = self.settings()
		if self.processor_boxes:
			self.selected_processors = settings.get('processors')
		self.stored_provider = settings.get('provider')
		self.stored_threads = settings.get('threads')
		self.session.config.set('benchmark', settings)

	def start(self) -> None:
		schema = self.session.schema
		settings = self.settings()
		if not schema:
			self.session.notify.emit('error', 'FaceFusion not set up', 'Open Setup and point the app at your FaceFusion v4 folder.')
			return
		if not settings.get('processors') or not settings.get('resolutions'):
			self.session.notify.emit('warning', 'Nothing to test', 'Pick at least one processor and one resolution.')
			return
		values = dict(self.session.values)
		# studio's trim and reference frame belong to its own target, not to the example videos
		values.update({ 'processors': settings.get('processors'), 'trim_frame_start': None, 'trim_frame_end': None, 'reference_frame_index': 0 })
		if settings.get('provider'):
			values['execution_providers'] = [ settings.get('provider') ]
		if 'execution_thread_count' in schema.options:
			values['execution_thread_count'] = settings.get('threads')
		# the progress steps are read from facefusion's info messages
		if values.get('log_level') in ('error', 'warn'):
			values['log_level'] = 'info'
		args = [ 'benchmark', '--benchmark-mode', settings.get('mode'), '--benchmark-resolutions' ] + settings.get('resolutions') + [ '--benchmark-cycle-count', str(settings.get('cycles')) ]
		args += self.session.option_args(values, [ group for group in schema.group_names() if group != 'paths' ])
		temp_option = schema.options.get('temp_path')
		if temp_option and values.get('temp_path'):
			args += [ temp_option.get('flag'), values.get('temp_path') ]
		self.run_settings = dict(settings)
		self.run_settings['models'] = { processor: values.get(processor + '_model') for processor in settings.get('processors') if values.get(processor + '_model') }
		self.rows = []
		self.running = self.session.run_facefusion('Benchmark', args)
		if self.running:
			self.run_total = len(settings.get('resolutions')) * (settings.get('cycles') + (1 if settings.get('mode') == 'warm' else 0))
			self.runs_done = 0
			self.stage = 'Starting FaceFusion'
			self.stage_ratio = 0.0
			self.stage_status = ''
			self.started_at = time.monotonic()
			self.elapsed_timer.start()
			self.stop_button.setEnabled(True)
			self.progress.setValue(0)
			self.progress.resume()
			self.refresh_status()

	def describe_run(self) -> str:
		"""which resolution and cycle the current run belongs to, warm mode adds an untimed warm up run per resolution"""
		resolutions = self.run_settings.get('resolutions') or []
		cycles = int(self.run_settings.get('cycles') or 1)
		warm = self.run_settings.get('mode') == 'warm'
		per_resolution = cycles + (1 if warm else 0)
		current = min(self.runs_done, self.run_total - 1)
		resolution_index = current // per_resolution
		within = current % per_resolution
		resolution = resolutions[resolution_index] if resolution_index < len(resolutions) else ''
		if warm and within == 0:
			cycle = 'warm-up run (not timed)'
		else:
			cycle = 'timed cycle %d of %d' % (within - (1 if warm else 0) + 1, cycles)
		return 'Run %d of %d  ·  %s (resolution %d of %d)  ·  %s' % (current + 1, self.run_total, resolution, resolution_index + 1, len(resolutions), cycle)

	def refresh_status(self) -> None:
		if not self.running:
			return
		elapsed = 'elapsed %s' % format_duration(time.monotonic() - self.started_at)
		if self.stage == 'Downloading':
			self.status_label.setText('Downloading example videos and models (first run only)')
			self.detail_label.setText('  ·  '.join(part for part in [ '%d%%' % round(self.stage_ratio * 100), self.stage_status, elapsed ] if part))
			return
		if self.stage in STAGE_SPAN:
			self.status_label.setText(self.describe_run())
			start, end = STAGE_SPAN.get(self.stage)
			within = start + (end - start) * self.stage_ratio
			self.progress.setValue(int(1000 * min(1.0, (self.runs_done + within) / max(1, self.run_total))))
			stage = self.stage + ('  %d%%' % round(self.stage_ratio * 100) if self.stage in ('Processing frames', 'Checking content') else '')
			self.detail_label.setText('  ·  '.join(part for part in [ stage, self.stage_status, elapsed ] if part))
			return
		self.status_label.setText(self.stage)
		self.detail_label.setText(elapsed)

	def set_stage(self, stage : str, ratio : float = 0.0, status : str = '') -> None:
		self.stage = stage
		self.stage_ratio = ratio
		self.stage_status = status
		self.refresh_status()

	def on_progress(self, title : str, ratio : float, status : str) -> None:
		if not self.running:
			return
		title = title.lower()
		if title.startswith('download'):
			self.progress.setValue(int(ratio * 1000))
			self.set_stage('Downloading', ratio, status)
		elif title.startswith('analys'):
			self.set_stage('Checking content', ratio, 'FaceFusion\'s NSFW check')
		elif title.startswith('processing'):
			self.set_stage('Processing frames', ratio, status)

	def on_log(self, line : str) -> None:
		if not self.running:
			return
		match = ROW_PATTERN.search(line)
		if match:
			self.rows.append(match.groupdict())
			return
		message = line.lower()
		if 'extracting frames' in message:
			self.set_stage('Extracting frames')
		elif 'merging video' in message:
			self.set_stage('Merging video')
		elif 'audio' in message and ('restoring' in message or 'skipping' in message or 'replacing' in message):
			self.set_stage('Adding audio')
		elif 'processing to video succeeded' in message:
			self.runs_done += 1
			if self.runs_done >= self.run_total:
				self.set_stage('Writing the results')
			else:
				self.set_stage('Starting the next run')

	def on_finished(self, exit_code : int, elapsed : float) -> None:
		if not self.running:
			return
		self.running = False
		self.elapsed_timer.stop()
		self.stop_button.setEnabled(False)
		self.remove_outputs()
		if exit_code == 0 and self.rows:
			self.progress.setValue(1000)
			self.status_label.setText('Finished: %d runs in %s' % (self.run_total, format_duration(elapsed)))
			self.detail_label.setText('The results are at the top of the table below.')
			self.add_history(self.rows)
		elif exit_code == -1:
			self.progress.pause()
			self.status_label.setText('Stopped')
			self.detail_label.setText('after %d of %d runs, %s' % (self.runs_done, self.run_total, format_duration(elapsed)))
		else:
			self.progress.error()
			self.status_label.setText('Benchmark failed (exit code %d)' % exit_code)
			self.detail_label.setText('See Console for FaceFusion\'s log.')

	def remove_outputs(self) -> None:
		# facefusion writes each benchmark video to the system temp folder and leaves it there
		for resolution in RESOLUTIONS:
			target_path = '.assets/examples/target-%s.mp4' % resolution
			output_path = os.path.join(tempfile.gettempdir(), hashlib.sha1(target_path.encode()).hexdigest() + '.mp4')
			if os.path.isfile(output_path):
				try:
					os.remove(output_path)
				except OSError:
					pass

	def add_history(self, rows : List[Dict[str, str]]) -> None:
		history = list(self.session.config.get('benchmark_history') or [])
		when = time.strftime('%Y-%m-%d %H:%M')
		run_id = int(time.time())
		entries = []
		for row in rows:
			entries.append(
			{
				'when': when,
				'run_id': run_id,
				'resolution': row.get('resolution'),
				'fps': float(row.get('fps')),
				'average': float(row.get('average')),
				'fastest': float(row.get('fastest')),
				'slowest': float(row.get('slowest')),
				'processors': self.run_settings.get('processors'),
				'models': self.run_settings.get('models'),
				'provider': self.run_settings.get('provider') or '',
				'threads': self.run_settings.get('threads'),
				'mode': self.run_settings.get('mode'),
				'cycles': int(row.get('cycles'))
			})
		self.session.config.set('benchmark_history', (entries + history)[:HISTORY_LIMIT])
		self.session.save()
		self.refresh_table()

	def clear_history(self) -> None:
		self.session.config.set('benchmark_history', [])
		self.refresh_table()

	def refresh_table(self) -> None:
		history = self.session.config.get('benchmark_history') or []
		self.table.setRowCount(len(history))
		for row, entry in enumerate(history):
			cells =\
			{
				'When': entry.get('when'),
				'Resolution': entry.get('resolution'),
				'FPS': '%.2f' % entry.get('fps', 0),
				'Average (sec)': '%.2f' % entry.get('average', 0),
				'Fastest (sec)': '%.2f' % entry.get('fastest', 0),
				'Slowest (sec)': '%.2f' % entry.get('slowest', 0),
				'Processors': format_processors(entry),
				'Provider': entry.get('provider'),
				'Threads': entry.get('threads'),
				'Mode': entry.get('mode'),
				'Cycles': entry.get('cycles')
			}
			for column, title in enumerate(COLUMNS):
				item = QTableWidgetItem(str(cells.get(title) or ''))
				if title in NUMBER_COLUMNS:
					item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
				self.table.setItem(row, column, item)
		self.table.resizeColumnsToContents()
		self.table.horizontalHeader().setSectionResizeMode(COLUMNS.index('Processors'), self.table.horizontalHeader().ResizeMode.Stretch)
		self.summary_label.setText(('%d results · latest %s' % (len(history), history[0].get('when'))) if history else 'No results yet')
		self.export_button.setEnabled(bool(history))

	def export_entries(self) -> List[Dict[str, Any]]:
		history = self.session.config.get('benchmark_history') or []
		selected = sorted({ index.row() for index in self.table.selectionModel().selectedRows() })
		if selected:
			return [ history[row] for row in selected if row < len(history) ]
		if not history:
			return []
		# without a selection, export every row of the latest run
		latest = history[0].get('run_id') or history[0].get('when')
		return [ entry for entry in history if (entry.get('run_id') or entry.get('when')) == latest ]

	def export_results(self) -> None:
		entries = self.export_entries()
		if not entries:
			return
		folder = self.session.config.get('benchmark_export_dir') or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
		name = 'facefusion-benchmark-%s.jpg' % time.strftime('%Y%m%d-%H%M')
		path, _ = QFileDialog.getSaveFileName(self, 'Export results', os.path.join(folder, name), 'JPEG image (*.jpg)')
		if not path:
			return
		if not path.lower().endswith(('.jpg', '.jpeg')):
			path += '.jpg'
		hardware = self.hardware or { name: label.text() for name, label in self.hardware_labels.items() }
		version = read_facefusion_version(self.session.facefusion_root) or (self.session.schema.version() if self.session.schema else '')
		image = render_report(hardware, version, entries)
		if not image.save(path, 'JPG', 95):
			self.session.notify.emit('error', 'Export failed', 'Could not write ' + path)
			return
		self.session.config.set('benchmark_export_dir', os.path.dirname(path))
		# discord takes a pasted image, so the clipboard saves a trip through the file picker
		QApplication.clipboard().setImage(image)
		self.session.notify.emit('success', 'Results exported', '%s — also copied to the clipboard, so you can paste it straight into Discord.' % os.path.basename(path))
