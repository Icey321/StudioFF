import hashlib
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QStandardPaths, QTimer, Qt
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QTableWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, CheckBox, ComboBox, FlowLayout, FluentIcon, MessageBox, PrimaryPushButton, ProgressBar, PushButton, SingleDirectionScrollArea, StrongBodyLabel, SubtitleLabel, TableWidget

from ffstudio import APP_VERSION
from ffstudio.benchmark_report import RESOLUTIONS, best_earlier, estimate_cards, format_processors, render_report, run_key, sort_by_resolution
from ffstudio.benchmark_widgets import FrameGrabber, FrameView, ResolutionTile, ResultsChart, SpeedDial, read_still, video_frame_total
from ffstudio.environment import read_facefusion_version
from ffstudio.hardware import HardwareProbe
from ffstudio.media import format_length
from ffstudio.pages.setup import form_row, section_card
from ffstudio.session import Session
from ffstudio.widgets import NumberBox, guard_wheel

MODES = { 'warm': 'Warm — models are loaded before timing starts', 'cold': 'Cold — caches are cleared before every cycle' }
DEFAULTS : Dict[str, Any] = { 'processors': [ 'face_swapper' ], 'resolutions': [ '240p', '540p', '1080p' ], 'mode': 'warm', 'cycles': 3, 'provider': None, 'threads': None, 'swapper_model': None }
# facefusion prints its result table as │ target_path │ cycle_count │ average_run │ fastest_run │ slowest_run │ relative_fps │
ROW_PATTERN = re.compile(r'│\s*(?P<target>\S*target-(?P<resolution>\d+p)\.\w+)\s*│\s*(?P<cycles>\d+)\s*│\s*(?P<average>[\d.]+)\s*│\s*(?P<fastest>[\d.]+)\s*│\s*(?P<slowest>[\d.]+)\s*│\s*(?P<fps>[\d.]+)\s*│')
RATE_PATTERN = re.compile(r'([\d.]+)\s*frame/s')
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
# results the table shows before it scrolls
TABLE_ROWS = 10
SWAPPED_COLOR = '#2fa86b'
ORIGINAL_COLOR = '#5f6670'


def output_path(resolution : str) -> str:
	"""where facefusion's benchmark writes the video of one resolution, it leaves it there"""
	target_path = '.assets/examples/target-%s.mp4' % resolution
	return os.path.join(tempfile.gettempdir(), hashlib.sha1(target_path.encode()).hexdigest() + '.mp4')


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
		# the live panel: when the current run started, the timed run lengths per resolution, the swapped frames shown
		self.run_started : Optional[float] = None
		self.timed_runs : Dict[str, List[float]] = {}
		self.tiles : Dict[str, ResolutionTile] = {}
		self.grabbers : List[FrameGrabber] = []
		self.grab_seconds : List[float] = []
		self.frames : List[Any] = []
		self.frame_index = 0
		self.frame_resolution = ''
		self.frame_total = 0
		self.original_shown = False
		self.slideshow = QTimer(self)
		self.slideshow.setInterval(1000)
		self.slideshow.timeout.connect(self.next_frame)
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

		# system info
		hardware_card, hardware_layout = section_card('System Info', '', content)
		self.hardware_layout = QVBoxLayout()
		self.hardware_layout.setSpacing(6)
		hardware_layout.addLayout(self.hardware_layout)
		self.hardware_labels : Dict[str, BodyLabel] = {}
		for name in [ 'Operating System', 'GPU', 'CPU', 'Memory', 'Python', 'ONNX Runtime', 'FFmpeg', 'FaceFusion', 'StudioFF' ]:
			value = BodyLabel('…', hardware_card)
			value.setWordWrap(True)
			value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
			self.hardware_labels[name] = value
			self.hardware_layout.addLayout(form_row(name, [ value ], hardware_card))
		layout.addWidget(hardware_card)

		# settings
		card, form = section_card('Test', 'Runs FaceFusion\'s own benchmark on its example videos and times the whole run, including reading frames, the processors and writing the video. Models and face settings come from Studio, except the face swapper model you pick here. The first run downloads all example videos (240p to 2160p) into the FaceFusion folder.', content)
		self.processor_widget = QWidget(card)
		self.processor_flow = FlowLayout(self.processor_widget, needAni = False)
		self.processor_flow.setContentsMargins(0, 0, 0, 0)
		self.processor_flow.setHorizontalSpacing(16)
		self.processor_flow.setVerticalSpacing(6)
		self.processor_boxes : Dict[str, CheckBox] = {}
		self.selected_processors = list(stored.get('processors') or [])
		form.addLayout(form_row('Processors', [ self.processor_widget ], card))

		self.model_row = QWidget(card)
		self.model_combo = ComboBox(self.model_row)
		self.model_combo.setFixedWidth(260)
		self.model_combo.currentIndexChanged.connect(self.save_settings)
		self.model_hint = self.hint('', self.model_row)
		model_layout = form_row('Face swapper model', [ self.model_combo, self.model_hint ], self.model_row)
		model_layout.setContentsMargins(0, 0, 0, 0)
		self.model_row.setLayout(model_layout)
		self.stored_model = stored.get('swapper_model')
		form.addWidget(self.model_row)

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
		self.start_button = PrimaryPushButton(FluentIcon.SPEED_HIGH, 'Run Benchmark', card)
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

		# live: the test face, the swapped frames, the speed and a tile per resolution
		self.live_card, live_layout = section_card('Live', 'Swapped frames appear when a resolution finishes, picked from the video FaceFusion just wrote. The dial shows the speed of the frame processing alone, so it reads higher than the results, which time the whole run.', content)
		media = QHBoxLayout()
		media.setSpacing(14)
		face_column = QVBoxLayout()
		face_column.setSpacing(6)
		self.face_view = FrameView(self.live_card, square = True)
		face_column.addWidget(self.face_view)
		face_caption = CaptionLabel('Test face', self.live_card)
		face_caption.setTextColor('#707070', '#9a9a9a')
		face_column.addWidget(face_caption, 0, Qt.AlignmentFlag.AlignHCenter)
		face_column.addStretch(1)
		media.addLayout(face_column)
		arrow = SubtitleLabel('➜', self.live_card)
		arrow.setTextColor('#0067c0', '#4cc2ff')
		media.addWidget(arrow)
		self.frame_view = FrameView(self.live_card)
		media.addWidget(self.frame_view)
		self.dial = SpeedDial(self.live_card)
		media.addStretch(1)
		media.addWidget(self.dial, 0, Qt.AlignmentFlag.AlignVCenter)
		media.addStretch(1)
		live_layout.addLayout(media)
		self.tile_row = QHBoxLayout()
		self.tile_row.setSpacing(10)
		live_layout.addLayout(self.tile_row)
		self.live_card.hide()
		layout.addWidget(self.live_card)

		# results
		results_card, results_layout = section_card('Results', 'Higher FPS is faster. Times are in seconds. Results are kept, so you can compare providers, models and settings: select rows of two runs to compare them in the chart. Select rows to export only those.', content)
		header = QHBoxLayout()
		self.summary_label = BodyLabel('', results_card)
		header.addWidget(self.summary_label, 1)
		self.export_button = PushButton(FluentIcon.SHARE, 'Export Results', results_card)
		self.export_button.setToolTip('Save the selected rows, or the latest run, as a JPG image for posting')
		self.export_button.clicked.connect(self.export_results)
		header.addWidget(self.export_button)
		self.delete_button = PushButton(FluentIcon.REMOVE, 'Delete Selected', results_card)
		self.delete_button.setToolTip('Delete the selected rows from the results, after asking (the Delete key does the same)')
		self.delete_button.setEnabled(False)
		self.delete_button.clicked.connect(self.delete_selected)
		header.addWidget(self.delete_button)
		clear_button = PushButton(FluentIcon.DELETE, 'Clear Results', results_card)
		clear_button.clicked.connect(self.clear_history)
		header.addWidget(clear_button)
		results_layout.addLayout(header)
		self.chart_title = StrongBodyLabel('', results_card)
		self.chart_title.setWordWrap(True)
		results_layout.addWidget(self.chart_title)
		self.chart_note = self.hint('', results_card)
		self.chart_note.setWordWrap(True)
		results_layout.addWidget(self.chart_note)
		self.chart = ResultsChart(results_card)
		results_layout.addWidget(self.chart)
		results_layout.addSpacing(6)
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
		self.table.horizontalHeader().setStretchLastSection(False)
		self.fit_table_rows()
		self.table.itemSelectionChanged.connect(self.refresh_chart)
		self.table.itemSelectionChanged.connect(lambda: self.delete_button.setEnabled(bool(self.table.selectionModel().selectedRows())))
		delete_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.table)
		delete_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
		delete_shortcut.activated.connect(self.delete_selected)
		results_layout.addWidget(self.table, 1)
		layout.addWidget(results_card, 1)

		session.schema_changed.connect(self.refresh_choices)
		session.processors_shown_changed.connect(self.refresh_choices)
		session.value_changed.connect(self.refresh_thread_hint)
		session.value_changed.connect(self.refresh_model_hint)
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
			self.probe = HardwareProbe(self.session.python_path, self.session.facefusion_root)
			self.probe.probed.connect(self.on_probed)
			self.probe.start()

	def on_probed(self, info : Dict[str, str]) -> None:
		info = dict(info, StudioFF = 'v' + APP_VERSION)
		self.hardware = { name: info.get(name) or 'Unknown' for name in self.hardware_labels }
		for name, label in self.hardware_labels.items():
			label.setText(self.hardware.get(name))

	def studio_threads(self) -> int:
		option = self.session.schema.options.get('execution_thread_count') if self.session.schema else None
		return int(self.session.values.get('execution_thread_count') or (option or {}).get('default') or 8)

	def studio_model(self) -> str:
		option = self.session.schema.options.get('face_swapper_model') if self.session.schema else None
		return str(self.session.values.get('face_swapper_model') or (option or {}).get('default') or '')

	def refresh_thread_hint(self, *_ : Any) -> None:
		self.thread_hint.setText('frames processed in parallel, Studio uses %d' % self.studio_threads())

	def refresh_model_hint(self, *_ : Any) -> None:
		box = self.processor_boxes.get('face_swapper')
		used = bool(box and box.isChecked())
		self.model_combo.setEnabled(used)
		self.model_hint.setText(('Studio uses %s' % self.studio_model()) if used else 'only used when face swapper is checked')

	def refresh_choices(self) -> None:
		schema = self.session.schema
		self.processor_flow.takeAllWidgets()
		self.processor_boxes = {}
		for processor in self.session.shown_processors():
			box = CheckBox(processor.replace('_', ' '), self.processor_widget)
			box.setChecked(processor in self.selected_processors)
			box.stateChanged.connect(self.save_settings)
			self.processor_flow.addWidget(box)
			self.processor_boxes[processor] = box

		# the face swapper model, picked here so models can be compared without changing Studio
		model_choices = list(((schema.options.get('face_swapper_model') if schema else None) or {}).get('choices') or [])
		self.model_combo.blockSignals(True)
		self.model_combo.clear()
		for model in model_choices:
			self.model_combo.addItem(model, userData = model)
		current = self.stored_model if self.stored_model in model_choices else self.studio_model()
		self.model_combo.setCurrentIndex(model_choices.index(current) if current in model_choices else 0)
		self.model_combo.blockSignals(False)
		self.model_row.setVisible(bool(model_choices) and 'face_swapper' in self.processor_boxes)
		self.refresh_model_hint()

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
			'provider': self.provider_combo.currentData(),
			'swapper_model': self.model_combo.currentData()
		}

	def save_settings(self, *_ : Any) -> None:
		settings = self.settings()
		if self.processor_boxes:
			self.selected_processors = settings.get('processors')
		self.stored_provider = settings.get('provider')
		self.stored_threads = settings.get('threads')
		if settings.get('swapper_model'):
			self.stored_model = settings.get('swapper_model')
		self.session.config.set('benchmark', settings)
		self.refresh_model_hint()

	def start(self) -> None:
		schema = self.session.schema
		settings = self.settings()
		if not schema:
			self.session.notify.emit('error', 'FaceFusion not set up', 'Open Settings and point the app at your FaceFusion v4 folder.')
			return
		if not settings.get('processors') or not settings.get('resolutions'):
			self.session.notify.emit('warning', 'Nothing to test', 'Pick at least one processor and one resolution.')
			return
		values = dict(self.session.values)
		# studio's trim and reference frame belong to its own target, not to the example videos
		values.update({ 'processors': settings.get('processors'), 'trim_frame_start': None, 'trim_frame_end': None, 'reference_frame_index': 0 })
		if 'face_swapper' in settings.get('processors') and settings.get('swapper_model'):
			values['face_swapper_model'] = settings.get('swapper_model')
			# options whose choices follow the model (pixel boost) fall back to the model's default when studio's value does not fit
			for dest in schema.dependencies:
				resolved = schema.resolve(dest, values)
				choices = resolved.get('choices')
				if choices and values.get(dest) not in choices:
					values[dest] = resolved.get('default') if resolved.get('default') in choices else choices[0]
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
			self.start_live(settings.get('resolutions'))
			self.refresh_status()

	# live panel

	def examples_path(self, name : str) -> str:
		return os.path.join(self.session.facefusion_root, '.assets', 'examples', name)

	def start_live(self, resolutions : List[str]) -> None:
		self.run_started = None
		self.timed_runs = { resolution: [] for resolution in resolutions }
		self.grab_seconds = []
		self.frames = []
		self.frame_index = 0
		self.frame_resolution = ''
		self.original_shown = False
		self.slideshow.stop()
		self.dial.reset()
		while self.tile_row.count():
			widget = self.tile_row.takeAt(0).widget()
			if widget:
				widget.deleteLater()
		self.tiles = {}
		for resolution in resolutions:
			tile = ResolutionTile(resolution, self.live_card)
			self.tile_row.addWidget(tile)
			self.tiles[resolution] = tile
		self.face_view.set_frame(read_still(self.examples_path('source.jpg')))
		self.frame_view.set_frame(QPixmap())
		self.show_original()
		self.live_card.show()

	def show_original(self) -> None:
		"""the untouched test video until the first swapped frames are ready, read again once the download put it there"""
		if self.original_shown or self.frames:
			return
		resolutions = self.run_settings.get('resolutions') or []
		pixmap = read_still(self.examples_path('target-%s.mp4' % resolutions[0])) if resolutions else QPixmap()
		if pixmap.isNull():
			return
		self.original_shown = True
		self.frame_view.set_frame(pixmap, 'ORIGINAL', 'Test video  ·  swapped frames show when %s is done' % resolutions[0], ORIGINAL_COLOR)

	def run_position(self, run_index : int) -> Dict[str, Any]:
		resolutions = self.run_settings.get('resolutions') or []
		cycles = int(self.run_settings.get('cycles') or 1)
		warm = self.run_settings.get('mode') == 'warm'
		per_resolution = cycles + (1 if warm else 0)
		resolution_index = min(run_index // per_resolution, max(0, len(resolutions) - 1))
		within = run_index % per_resolution
		return\
		{
			'resolution': resolutions[resolution_index] if resolutions else '',
			'within': within,
			'per_resolution': per_resolution,
			'warm_up': warm and within == 0,
			'cycle': within - (1 if warm else 0) + 1,
			'cycles': cycles,
			'last': within == per_resolution - 1
		}

	def refresh_tile(self) -> None:
		if self.runs_done >= self.run_total or self.stage not in STAGE_SPAN:
			return
		position = self.run_position(self.runs_done)
		tile = self.tiles.get(position.get('resolution'))
		if not tile:
			return
		start, end = STAGE_SPAN.get(self.stage)
		within = start + (end - start) * self.stage_ratio
		detail = 'warm-up (not timed)' if position.get('warm_up') else 'timed cycle %d of %d' % (position.get('cycle'), position.get('cycles'))
		tile.set_state('running', detail, ratio = (position.get('within') + within) / position.get('per_resolution'))

	def finish_run(self) -> None:
		"""a run ended: its length counts if it was timed, and a resolution's last run estimates its fps and shows its frames"""
		now = time.monotonic()
		position = self.run_position(self.runs_done)
		resolution = position.get('resolution')
		if self.run_started is not None and not position.get('warm_up'):
			self.timed_runs.setdefault(resolution, []).append(now - self.run_started)
		# facefusion starts the next run straight away
		self.run_started = now
		if not position.get('last'):
			return
		tile = self.tiles.get(resolution)
		times = self.timed_runs.get(resolution) or []
		frame_total = video_frame_total(self.examples_path('target-%s.mp4' % resolution))
		if tile:
			if times and frame_total:
				tile.set_state('estimate', 'estimate, exact at the end', fps = frame_total * len(times) / sum(times))
			else:
				tile.set_state('estimate', 'done')
		grabber = FrameGrabber(output_path(resolution), resolution, parent = self)
		grabber.grabbed.connect(self.on_grabbed)
		grabber.finished.connect(lambda grabber = grabber: self.grabbers.remove(grabber) if grabber in self.grabbers else None)
		self.grabbers.append(grabber)
		grabber.start()

	def on_grabbed(self, resolution : str, images : List[Any], total : int) -> None:
		grabber = self.sender()
		if isinstance(grabber, FrameGrabber):
			self.grab_seconds.append(grabber.seconds)
		if not self.running:
			self.remove_outputs()
		if not images:
			return
		self.frames = [ QPixmap.fromImage(image) for image in images ]
		self.frame_resolution = resolution
		self.frame_total = total
		self.frame_index = -1
		self.next_frame()
		self.slideshow.start()

	def next_frame(self) -> None:
		if not self.frames:
			return
		self.frame_index = (self.frame_index + 1) % len(self.frames)
		step = max(1, self.frame_total // len(self.frames)) if self.frame_total else 1
		number = self.frame_index * step + step // 2 + 1
		caption = '%s result  ·  frame %d of %d' % (self.frame_resolution, number, self.frame_total) if self.frame_total else '%s result' % self.frame_resolution
		self.frame_view.set_frame(self.frames[self.frame_index], '● SWAPPED', caption, SWAPPED_COLOR)

	def describe_run(self) -> str:
		"""which resolution and cycle the current run belongs to, warm mode adds an untimed warm up run per resolution"""
		resolutions = self.run_settings.get('resolutions') or []
		current = min(self.runs_done, self.run_total - 1)
		position = self.run_position(current)
		resolution_index = resolutions.index(position.get('resolution')) if position.get('resolution') in resolutions else 0
		cycle = 'warm-up run (not timed)' if position.get('warm_up') else 'timed cycle %d of %d' % (position.get('cycle'), position.get('cycles'))
		return 'Run %d of %d  ·  %s (resolution %d of %d)  ·  %s' % (current + 1, self.run_total, position.get('resolution'), resolution_index + 1, len(resolutions), cycle)

	def refresh_status(self) -> None:
		if not self.running:
			return
		elapsed = 'elapsed %s' % format_length(time.monotonic() - self.started_at)
		if self.stage == 'Downloading':
			self.status_label.setText('Downloading example videos and models (first run only)')
			self.detail_label.setText('  ·  '.join(part for part in [ '%d%%' % round(self.stage_ratio * 100), self.stage_status, elapsed ] if part))
			return
		self.show_original()
		if self.stage in STAGE_SPAN:
			self.status_label.setText(self.describe_run())
			start, end = STAGE_SPAN.get(self.stage)
			within = start + (end - start) * self.stage_ratio
			self.progress.setValue(int(1000 * min(1.0, (self.runs_done + within) / max(1, self.run_total))))
			stage = self.stage + ('  %d%%' % round(self.stage_ratio * 100) if self.stage in ('Processing frames', 'Checking content') else '')
			self.detail_label.setText('  ·  '.join(part for part in [ stage, self.stage_status, elapsed ] if part))
			self.refresh_tile()
			return
		self.status_label.setText(self.stage)
		self.detail_label.setText(elapsed)

	def set_stage(self, stage : str, ratio : float = 0.0, status : str = '') -> None:
		self.stage = stage
		self.stage_ratio = ratio
		self.stage_status = status
		# the first run starts with its first step, later ones when the run before them ended
		if stage in STAGE_SPAN and self.run_started is None:
			self.run_started = time.monotonic()
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
			rate = RATE_PATTERN.search(status)
			if rate and ratio > 0.05:
				self.dial.set_value(float(rate.group(1)))

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
			self.finish_run()
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
			self.status_label.setText('Finished: %d runs in %s' % (self.run_total, format_length(elapsed)))
			self.detail_label.setText('The chart and table below show the results.')
			for row in self.rows:
				tile = self.tiles.get(row.get('resolution'))
				if tile:
					tile.set_state('final', 'FaceFusion\'s result', fps = float(row.get('fps')))
			self.add_history(self.rows)
			return
		for tile in self.tiles.values():
			if tile.state == 'running':
				tile.set_state('waiting', 'stopped' if exit_code == -1 else 'failed')
		if exit_code == -1:
			self.progress.pause()
			self.status_label.setText('Stopped')
			self.detail_label.setText('after %d of %d runs, %s' % (self.runs_done, self.run_total, format_length(elapsed)))
		else:
			self.progress.error()
			self.status_label.setText('Benchmark failed (exit code %d)' % exit_code)
			self.detail_label.setText('See Console for FaceFusion\'s log.')

	def remove_outputs(self) -> None:
		# facefusion writes each benchmark video to the system temp folder and leaves it there, one being read for frames
		# stays until its frames are in
		busy = { grabber.path for grabber in self.grabbers if grabber.isRunning() }
		for resolution in RESOLUTIONS:
			path = output_path(resolution)
			if os.path.isfile(path) and path not in busy:
				try:
					os.remove(path)
				except OSError:
					pass

	# results

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

	def delete_selected(self) -> None:
		"""removes the selected result rows after asking, the rest of their runs stay"""
		history = list(self.session.config.get('benchmark_history') or [])
		rows = [ row for row in sorted({ index.row() for index in self.table.selectionModel().selectedRows() }) if row < len(history) ]
		if not rows:
			return
		listing = '\n'.join('%s  ·  %s  ·  %.2f fps  ·  %s  ·  %s' % (history[row].get('when'), history[row].get('resolution'), float(history[row].get('fps') or 0), format_processors(history[row]), history[row].get('provider') or 'default') for row in rows[:8])
		if len(rows) > 8:
			listing += '\n… and (%d) more' % (len(rows) - 8)
		box = MessageBox('Delete (%d) result%s?' % (len(rows), '' if len(rows) == 1 else 's'), listing + '\n\nThis cannot be undone.', self.window())
		box.yesButton.setText('Delete')
		box.cancelButton.setText('Cancel')
		if not box.exec():
			return
		self.session.config.set('benchmark_history', [ entry for row, entry in enumerate(history) if row not in rows ])
		self.session.save()
		self.refresh_table()

	def clear_history(self) -> None:
		self.session.config.set('benchmark_history', [])
		self.refresh_table()

	def fit_table_rows(self) -> None:
		"""tall enough for TABLE_ROWS results before the table scrolls"""
		header = self.table.horizontalHeader()
		header_height = max(header.height(), header.sizeHint().height())
		frame = self.table.frameWidth() * 2
		self.table.setMinimumHeight(header_height + self.table.verticalHeader().defaultSectionSize() * TABLE_ROWS + frame + 2)

	def refresh_table(self) -> None:
		history = self.session.config.get('benchmark_history') or []
		self.table.blockSignals(True)
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
		self.table.clearSelection()
		self.table.blockSignals(False)
		self.delete_button.setEnabled(False)
		self.table.resizeColumnsToContents()
		self.table.horizontalHeader().setSectionResizeMode(COLUMNS.index('Processors'), self.table.horizontalHeader().ResizeMode.Stretch)
		self.summary_label.setText(('%d results · latest %s' % (len(history), history[0].get('when'))) if history else 'No results yet')
		self.export_button.setEnabled(bool(history))
		self.refresh_chart()

	def selected_runs(self) -> List[List[Dict[str, Any]]]:
		"""the runs the selected rows belong to, newest first, every row of each run"""
		history = self.session.config.get('benchmark_history') or []
		keys : List[Any] = []
		for row in sorted({ index.row() for index in self.table.selectionModel().selectedRows() }):
			if row < len(history) and run_key(history[row]) not in keys:
				keys.append(run_key(history[row]))
		return [ [ entry for entry in history if run_key(entry) == key ] for key in keys ]

	def refresh_chart(self) -> None:
		history = self.session.config.get('benchmark_history') or []
		runs = self.selected_runs()
		if not runs and history:
			runs = [ [ entry for entry in history if run_key(entry) == run_key(history[0]) ] ]
		if not runs:
			self.chart.set_run([], {}, [])
			self.chart_title.setText('')
			self.chart_note.setText('')
			return
		rows = sort_by_resolution(runs[0])
		first = rows[0]
		if len(runs) == 2:
			other = runs[1][0]
			ghost = { entry.get('resolution'): float(entry.get('fps') or 0) for entry in runs[1] }
			note = 'Dashed outline: the run of %s (%s, %s).' % (other.get('when'), format_processors(other), other.get('provider') or 'default')
		else:
			ghost = best_earlier(rows, history)
			note = 'Dashed outline: the best other result with the same processors and models.' if ghost else 'Run again with another provider or setting, then select rows of both runs to compare them.'
			if len(runs) > 2:
				note = 'Select rows of two runs to compare them. ' + note
		settings = [ format_processors(first), first.get('provider') or 'default', '%s threads' % first.get('threads') if first.get('threads') else '', first.get('mode') or '', first.get('when') or '' ]
		self.chart_title.setText('  ·  '.join(part for part in settings if part))
		self.chart_note.setText(note)
		self.chart.set_run(rows, ghost, estimate_cards(rows, history))

	def export_entries(self) -> List[Dict[str, Any]]:
		history = self.session.config.get('benchmark_history') or []
		selected = sorted({ index.row() for index in self.table.selectionModel().selectedRows() })
		if selected:
			return [ history[row] for row in selected if row < len(history) ]
		if not history:
			return []
		# without a selection, export every row of the latest run
		latest = run_key(history[0])
		return [ entry for entry in history if run_key(entry) == latest ]

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
		version = read_facefusion_version(self.session.facefusion_root) or (self.session.schema.version if self.session.schema else '')
		image = render_report(hardware, version, entries)
		if not image.save(path, 'JPG', 95):
			self.session.notify.emit('error', 'Export failed', 'Could not write ' + path)
			return
		self.session.config.set('benchmark_export_dir', os.path.dirname(path))
		# discord takes a pasted image, so the clipboard saves a trip through the file picker
		QApplication.clipboard().setImage(image)
		self.session.notify.emit('success', 'Results exported', '%s — also copied to the clipboard, so you can paste it straight into Discord.' % os.path.basename(path))
