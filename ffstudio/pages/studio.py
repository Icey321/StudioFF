import json
import math
import os
import shutil
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import cv2
from PySide6.QtCore import QPoint, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QGridLayout, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import Action, BodyLabel, CaptionLabel, FlowLayout, FluentIcon, Flyout, FlyoutAnimationType, FlyoutViewBase, IndeterminateProgressBar, LineEdit, PrimaryPushButton, ProgressBar, PushButton, RoundMenu, SearchLineEdit, SegmentedWidget, SimpleCardWidget, SingleDirectionScrollArea, Slider, StrongBodyLabel, SubtitleLabel, TogglePushButton, ToolTipFilter, TransparentPushButton, TransparentToolButton

from ffstudio.config import APP_DIR, PREVIEW_DIR
from ffstudio.face_mapping import FACE_PROCESSORS, FRAME_PROCESSORS, LEAVE, PEOPLE_FILE, SELECTED, FaceMappingCard, box_overlap, is_mapped, merge_people, people_dir, person_color, person_name, plan_passes
from ffstudio.faces import FACE_GROUPS, MASK_GROUPS, FaceProbe, describe_faces, describe_mask, face_tag, read_mask
from ffstudio.media import GIF_EXTENSIONS, VideoReader, extension, format_timecode, media_kind, read_frame, read_image, thumbnail, to_pixmap, write_image
from ffstudio.nsfw import BLOCKED_STATUS, NsfwDialog, describe_result, show_blocked_bar
from ffstudio.option_panel import FavoritesPanel, OptionPanel
from ffstudio.output_name import AUTOMATIC_TEMPLATE, NAME_EXAMPLES, NAME_TAGS, build_output, build_output_path, unknown_tags
from ffstudio.session import Session, folder_is_writable
from ffstudio.widgets import PreviewView, ProcessorArea, ProcessorButton, Splitter, Thumbnail, add_edit_button, collect_drop_paths, guard_wheel

STUDIO_GROUPS = [ 'face_selector', 'face_masker', 'face_detector', 'face_aligner', 'face_tracker', 'output_creation', 'frame_extraction', 'frame_distribution', 'voice_extractor', 'workflow' ]
# settings that cannot change how a single preview frame looks
LIVE_IGNORED_GROUPS = [ 'paths', 'output_creation', 'frame_extraction', 'frame_distribution', 'voice_extractor', 'workflow', 'execution', 'download', 'memory', 'misc' ]
# bigger frames are previewed on a copy this many pixels high (or wide, when upright), which is a little quicker
PREVIEW_MAX_SIDE = 1080
# the outputs of the passes of a face mapping run before the last one
PASSES_DIR = os.path.join(PREVIEW_DIR, 'passes')
SOURCE_FILTERS = [ 'Images and audio (*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus)', 'All files (*)' ]


def elastic(label : CaptionLabel) -> CaptionLabel:
	# long status text must not set the minimum width of the window, it gets clipped instead
	label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
	label.setMinimumWidth(0)
	return label


def tool_button(icon : FluentIcon, tip : str, parent : QWidget) -> TransparentToolButton:
	button = TransparentToolButton(icon, parent)
	button.setToolTip(tip)
	button.installEventFilter(ToolTipFilter(button))
	return button


def estimated_progress(elapsed : float, expected : float, floor : float) -> float:
	"""how far a preview probably is, facefusion reports no progress for a single frame. reaches 95% at the expected time, then creeps towards 99%"""
	return max(floor, min(0.99, 1 - math.exp(-3 * elapsed / expected)))


def preview_step_floor(line : str, floor : float) -> float:
	"""the last log lines of a preview move its bar forward"""
	lowered = line.lower()
	if 'processing to image succeeded' in lowered:
		return 1.0
	if 'finalizing image' in lowered:
		return max(floor, 0.95)
	return floor


def card_header(title : str, parent : QWidget) -> QHBoxLayout:
	layout = QHBoxLayout()
	layout.setSpacing(4)
	layout.addWidget(StrongBodyLabel(title, parent))
	layout.addStretch(1)
	return layout


class StripScroll(SingleDirectionScrollArea):
	"""vertical scroll area that prefers its maximum height, so all rows show when there is room"""

	def __init__(self, parent : QWidget) -> None:
		super().__init__(parent, Qt.Orientation.Vertical)
		self.setWidgetResizable(True)
		self.enableTransparentBackground()
		self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

	def sizeHint(self) -> QSize:
		return QSize(super().sizeHint().width(), self.maximumHeight())

	def fit(self) -> None:
		"""asks for the height of every row, but can shrink to one row and scroll the rest"""
		if not self.widget():
			return
		row = Thumbnail.IMAGE_SIZE + Thumbnail.NAME_HEIGHT
		needed = max(row, self.widget().layout().heightForWidth(max(1, self.viewport().width())))
		self.setMinimumHeight(row)
		if needed != self.maximumHeight():
			self.setMaximumHeight(needed)
			self.updateGeometry()

	def resizeEvent(self, event) -> None:
		super().resizeEvent(event)
		QTimer.singleShot(0, self.fit)


def run_tag_values(session : Session) -> Dict[str, str]:
	"""the file name tags that come from the settings and the clock rather than from the files"""
	processors = session.processors()
	model = next((str(session.values.get(processor + '_model')) for processor in processors if session.values.get(processor + '_model')), '')
	now = datetime.now()
	return\
	{
		'processors': '-'.join(processor.split('_')[-1] if '_' in processor else processor for processor in processors[:3]) or 'out',
		'model': model,
		'date': now.strftime('%Y-%m-%d'),
		'time': now.strftime('%H-%M-%S')
	}


class SourceSelection:
	"""the sources behind a SourceCard: which there are, which are used and what they are called

	the page sets session and source_card, and gets on_sources_changed('list' | 'names' | 'selection') after each change
	"""
	SOURCE_KEYS = ('source_paths', 'unused_source_paths', 'source_names')

	def load_sources(self) -> None:
		paths_key, unused_key, names_key = self.SOURCE_KEYS
		config = self.session.config
		self.source_paths : List[str] = [ path for path in config.get(paths_key) or [] if os.path.exists(path) ]
		self.unused_sources : List[str] = [ path for path in config.get(unused_key) or [] if path in self.source_paths ]
		# names given to sources by clicking the name under a thumbnail, {source} in the file name uses them
		self.source_names : Dict[str, str] = { path: name for path, name in (config.get(names_key) or {}).items() if path in self.source_paths }
		self.source_anchor = ''

	def store_sources(self) -> None:
		paths_key, unused_key, names_key = self.SOURCE_KEYS
		self.session.config.set(paths_key, self.source_paths)
		self.session.config.set(unused_key, self.unused_sources)
		self.session.config.set(names_key, self.source_names)

	def on_sources_changed(self, change : str) -> None:
		pass

	def add_sources(self, paths : List[str]) -> None:
		accepted = [ path for path in paths if media_kind(path) in [ 'image', 'audio' ] and path not in self.source_paths ]
		rejected = [ path for path in paths if media_kind(path) not in [ 'image', 'audio' ] ]
		if rejected:
			self.session.notify.emit('warning', 'Skipped files', 'Sources must be images or audio: ' + ', '.join(os.path.basename(path) for path in rejected[:3]))
		# what was just added becomes the selection for its kind
		added_kinds = { media_kind(path) for path in accepted }
		self.unused_sources = list(dict.fromkeys(self.unused_sources + [ path for path in self.source_paths if media_kind(path) in added_kinds ]))
		if accepted:
			self.source_anchor = accepted[0]
		self.set_sources(self.source_paths + accepted)

	def remove_source(self, path : str) -> None:
		self.set_sources([ source for source in self.source_paths if source != path ])

	def set_sources(self, paths : List[str]) -> None:
		self.source_paths = paths
		self.unused_sources = [ path for path in self.unused_sources if path in paths ]
		self.source_names = { path: name for path, name in self.source_names.items() if path in paths }
		self.store_sources()
		self.source_card.refresh(paths)
		self.on_sources_changed('list')

	def source_name(self, path : str) -> str:
		return self.source_names.get(path) or os.path.splitext(os.path.basename(path))[0]

	def rename_source(self, path : str, name : str) -> None:
		if name:
			self.source_names[path] = name
		else:
			self.source_names.pop(path, None)
		self.store_sources()
		self.on_sources_changed('names')

	def active_sources(self) -> List[str]:
		return [ path for path in self.source_paths if path not in self.unused_sources ]

	def on_source_clicked(self, path : str, modifiers : Qt.KeyboardModifier) -> None:
		"""explorer style selection: click picks one, ctrl+click adds or removes, shift+click picks a range

		images and audio are picked separately, so one face and one audio file can be used together
		"""
		kind = media_kind(path)
		same_kind = [ source for source in self.source_paths if media_kind(source) == kind ]
		used = set(self.active_sources())
		ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
		if modifiers & Qt.KeyboardModifier.ShiftModifier and self.source_anchor in same_kind:
			start, end = sorted([ same_kind.index(self.source_anchor), same_kind.index(path) ])
			if not ctrl:
				used -= set(same_kind)
			used |= set(same_kind[start:end + 1])
		elif ctrl:
			used ^= { path }
			self.source_anchor = path
		else:
			used = (used - set(same_kind)) | { path }
			self.source_anchor = path
		self.set_unused_sources([ source for source in self.source_paths if source not in used ])

	def set_unused_sources(self, unused : List[str]) -> None:
		self.unused_sources = unused
		self.store_sources()
		self.source_card.refresh_selection()
		self.on_sources_changed('selection')

	def toggle_all_sources(self) -> None:
		# with any source left out this selects all of them, otherwise it clears the selection
		self.set_unused_sources([] if self.unused_sources else list(self.source_paths))


class SourceCard(SimpleCardWidget):
	def __init__(self, page : QWidget, title : str = 'Source', empty_text : str = 'Drop face images (or audio for lip sync) here\nor click to browse') -> None:
		super().__init__(page)
		self.page = page
		self.setAcceptDrops(True)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(16, 12, 12, 14)
		layout.setSpacing(8)
		# the filter the file picker opens on
		self.browse_filter = SOURCE_FILTERS[0]
		self.header = header = card_header(title, self)
		self.count_label = elastic(CaptionLabel('', self))
		self.count_label.installEventFilter(ToolTipFilter(self.count_label))
		self.count_label.setTextColor('#707070', '#9a9a9a')
		header.insertWidget(1, self.count_label, 1)
		self.select_button = tool_button(FluentIcon.CHECKBOX, 'Use all sources / none', self)
		self.select_button.clicked.connect(self.page.toggle_all_sources)
		add_button = tool_button(FluentIcon.ADD, 'Add face images or audio', self)
		add_button.clicked.connect(self.browse)
		clear_button = tool_button(FluentIcon.DELETE, 'Remove all sources', self)
		clear_button.clicked.connect(lambda: self.page.set_sources([]))
		header.addWidget(self.select_button)
		header.addWidget(add_button)
		header.addWidget(clear_button)
		layout.addLayout(header)

		self.empty = PreviewView(empty_text, self)
		self.empty.setMinimumHeight(96)
		self.empty.setMaximumHeight(96)
		self.empty.clicked.connect(self.browse)
		self.empty.files_dropped.connect(self.page.add_sources)
		layout.addWidget(self.empty)

		# the thumbnails wrap into rows, when the window is too short for all rows the strip scrolls
		self.strip_scroll = StripScroll(self)
		self.strip = QWidget()
		self.strip.setStyleSheet('background: transparent')
		self.flow = FlowLayout(self.strip, needAni = False)
		self.flow.setContentsMargins(0, 0, 0, 0)
		self.flow.setHorizontalSpacing(8)
		self.flow.setVerticalSpacing(8)
		self.strip_scroll.setWidget(self.strip)
		layout.addWidget(self.strip_scroll)

	def browse(self) -> None:
		paths, _ = QFileDialog.getOpenFileNames(self, 'Choose source faces or audio', self.page.last_dir(), ';;'.join(SOURCE_FILTERS), self.browse_filter)
		if paths:
			self.page.add_sources([ os.path.normpath(path) for path in paths ])

	def add_tool(self, button : QWidget) -> None:
		"""an extra button in the header, before the select all button"""
		self.header.insertWidget(self.header.indexOf(self.select_button), button)

	def refresh(self, paths : List[str]) -> None:
		# hidden at once, the delete only happens when the event loop gets to it
		for item in getattr(self, 'thumbnails', []):
			item.hide()
		self.flow.takeAllWidgets()
		self.thumbnails : List[Thumbnail] = []
		for path in paths:
			pixmap = thumbnail(path, 2 * Thumbnail.IMAGE_SIZE) if media_kind(path) == 'image' else None
			item = Thumbnail(path, pixmap if pixmap is not None else self.audio_pixmap(), path not in self.page.unused_sources, self.strip, self.page.source_names.get(path, ''))
			if pixmap is None:
				item.preview.hint = '♪\naudio'
			item.removed.connect(self.page.remove_source)
			item.renamed.connect(self.page.rename_source)
			item.clicked.connect(self.page.on_source_clicked)
			self.flow.addWidget(item)
			self.thumbnails.append(item)
		self.empty.setVisible(not paths)
		self.strip_scroll.setVisible(bool(paths))
		self.select_button.setVisible(len(paths) > 1)
		self.refresh_count()
		self.strip_scroll.fit()

	def refresh_selection(self) -> None:
		for item in self.thumbnails:
			item.set_selected(item.path not in self.page.unused_sources)
		self.refresh_count()

	def refresh_count(self) -> None:
		paths = self.page.source_paths
		used = self.page.active_sources()
		parts = []
		for kind, singular, plural in [ ('image', 'face', 'faces'), ('audio', 'audio', 'audio') ]:
			total = len([ path for path in paths if media_kind(path) == kind ])
			chosen = len([ path for path in used if media_kind(path) == kind ])
			if total:
				parts.append(('%d %s' % (total, singular if total == 1 else plural)) if chosen == total else '%d of %d %s used' % (chosen, total, plural))
		hint = 'Ctrl+click for several' if len(paths) > 1 else ''
		self.count_label.setText(' · '.join(parts + ([ hint ] if hint else [])))
		self.count_label.setToolTip('Click a thumbnail to use only that source, Ctrl+click to add or remove one, Shift+click to pick a range. Images and audio are picked separately.' if hint else '')

	@staticmethod
	def audio_pixmap():
		from PySide6.QtGui import QPixmap
		return QPixmap()

	def dragEnterEvent(self, event) -> None:
		if event.mimeData().hasUrls():
			event.acceptProposedAction()

	def dropEvent(self, event) -> None:
		self.page.add_sources(collect_drop_paths(event))


class TargetCard(SimpleCardWidget):
	def __init__(self, page : 'StudioPage') -> None:
		super().__init__(page)
		self.page = page
		self.reader : Optional[VideoReader] = None
		self.frame_timer = QTimer(self)
		self.frame_timer.setSingleShot(True)
		self.frame_timer.setInterval(40)
		self.frame_timer.timeout.connect(self.show_frame)

		layout = QVBoxLayout(self)
		layout.setContentsMargins(16, 12, 12, 14)
		layout.setSpacing(8)
		header = card_header('Target', self)
		self.info_label = elastic(CaptionLabel('', self))
		self.info_label.setTextColor('#707070', '#9a9a9a')
		header.insertWidget(1, self.info_label, 1)
		self.faces_button = TogglePushButton(FluentIcon.PEOPLE, 'Faces', self)
		self.faces_button.setToolTip('Show the faces FaceFusion detects in this frame with the current face settings.\nOutlined faces will be processed, dashed ones are skipped by the face selector.')
		self.faces_button.installEventFilter(ToolTipFilter(self.faces_button))
		self.faces_button.setChecked(bool(self.page.session.config.get('show_faces')))
		self.faces_button.toggled.connect(self.page.on_faces_toggled)
		header.addWidget(self.faces_button)
		self.mask_button = TogglePushButton(FluentIcon.PALETTE, 'Mask', self)
		self.mask_button.setToolTip('Shade the face mask FaceFusion would blend the new face through, with the current Face masker settings.\nIt follows the mask types, padding, blur, areas and regions as you change them.')
		self.mask_button.installEventFilter(ToolTipFilter(self.mask_button))
		self.mask_button.setChecked(bool(self.page.session.config.get('show_mask')))
		self.mask_button.toggled.connect(self.page.on_mask_toggled)
		header.addWidget(self.mask_button)
		header.addSpacing(6)
		self.view_switch = SegmentedWidget(self)
		self.view_switch.addItem('target', 'Target', lambda: self.pick_view('target'))
		self.view_switch.addItem('result', 'Result', lambda: self.pick_view('result'))
		self.view_switch.setCurrentItem('target')
		self.view_switch.setFixedWidth(170)
		header.addWidget(self.view_switch)
		header.addSpacing(6)
		browse_button = tool_button(FluentIcon.FOLDER, 'Choose target image or video', self)
		browse_button.clicked.connect(self.browse)
		clear_button = tool_button(FluentIcon.DELETE, 'Clear target', self)
		clear_button.clicked.connect(lambda: self.page.set_target(''))
		header.addWidget(browse_button)
		header.addWidget(clear_button)
		layout.addLayout(header)

		self.preview = PreviewView('Drop a target image or video here\nor click to browse', self)
		self.preview.setMinimumHeight(160)
		self.preview.files_dropped.connect(lambda paths: self.page.set_target(paths[0]))
		self.preview.clicked.connect(self.on_preview_clicked)
		self.preview.face_menu_requested.connect(self.page.show_face_menu)
		layout.addWidget(self.preview, 1)
		# what face detection found, and which setting to change when it found nothing usable
		self.faces_label = CaptionLabel('', self)
		self.faces_label.setWordWrap(True)
		self.faces_label.setVisible(self.faces_button.isChecked() or self.mask_button.isChecked())
		layout.addWidget(self.faces_label)

		self.timeline = QWidget(self)
		timeline_layout = QHBoxLayout(self.timeline)
		timeline_layout.setContentsMargins(0, 0, 0, 0)
		timeline_layout.setSpacing(8)
		self.slider = Slider(Qt.Orientation.Horizontal, self.timeline)
		self.slider.setMinimumWidth(120)
		self.slider.valueChanged.connect(lambda _: self.frame_timer.start())
		self.slider.valueChanged.connect(lambda _: self.page.schedule_live_preview())
		self.slider.valueChanged.connect(lambda _: self.page.refresh_name_preview())
		guard_wheel(self.slider)
		self.time_label = CaptionLabel('00:00:00.00', self.timeline)
		self.time_label.setFixedWidth(150)
		self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
		timeline_layout.addWidget(self.slider, 1)
		timeline_layout.addWidget(self.time_label)
		layout.addWidget(self.timeline)

		self.frame_tools = QWidget(self)
		tools_layout = QHBoxLayout(self.frame_tools)
		tools_layout.setContentsMargins(0, 0, 0, 0)
		tools_layout.setSpacing(6)
		self.reference_button = PushButton(FluentIcon.PEOPLE, 'Use as reference frame', self.frame_tools)
		self.reference_button.clicked.connect(self.use_reference_frame)
		self.trim_start_button = PushButton(FluentIcon.CUT, 'Trim start', self.frame_tools)
		self.trim_start_button.clicked.connect(lambda: self.set_trim('trim_frame_start'))
		self.trim_end_button = PushButton(FluentIcon.CUT, 'Trim end', self.frame_tools)
		self.trim_end_button.clicked.connect(lambda: self.set_trim('trim_frame_end'))
		self.trim_clear_button = tool_button(FluentIcon.CANCEL, 'Clear trim', self.frame_tools)
		self.trim_clear_button.clicked.connect(self.clear_trim)
		self.marker_label = CaptionLabel('', self.frame_tools)
		self.marker_label.setTextColor('#707070', '#9a9a9a')
		elastic(self.marker_label)
		for widget in [ self.reference_button, self.trim_start_button, self.trim_end_button, self.trim_clear_button ]:
			tools_layout.addWidget(widget)
		tools_layout.addSpacing(6)
		tools_layout.addWidget(self.marker_label, 1)
		layout.addWidget(self.frame_tools)

		# bottom row: preview progress while a preview runs, otherwise the last result, plus the result buttons
		result_row = QHBoxLayout()
		result_row.setContentsMargins(0, 2, 0, 0)
		result_row.setSpacing(8)
		# an indeterminate bar while facefusion starts up, the real bar once it reports progress
		self.busy_bar = IndeterminateProgressBar(self, start = False)
		self.progress_bar = ProgressBar(self)
		self.progress_bar.setRange(0, 1000)
		self.result_text = 'No result yet'
		self.result_label = elastic(CaptionLabel(self.result_text, self))
		self.result_label.setTextColor('#707070', '#9a9a9a')
		self.result_label.installEventFilter(ToolTipFilter(self.result_label))
		for widget in [ self.busy_bar, self.progress_bar, self.result_label ]:
			result_row.addWidget(widget, 1, Qt.AlignmentFlag.AlignVCenter)
		self.progress_label = CaptionLabel('', self)
		self.progress_label.setTextColor('#707070', '#9a9a9a')
		self.progress_label.installEventFilter(ToolTipFilter(self.progress_label))
		result_row.addWidget(self.progress_label)
		self.preview_stop_button = tool_button(FluentIcon.CLOSE, 'Stop making this preview', self)
		self.preview_stop_button.clicked.connect(self.page.stop)
		result_row.addWidget(self.preview_stop_button)
		# Auto previews the frame by itself whenever something changes, the refresh button previews it now
		self.live_button = TogglePushButton(FluentIcon.SYNC, 'Auto', self)
		self.live_button.setToolTip('Make the preview again by itself whenever a setting, source, processor or the frame changes')
		self.live_button.installEventFilter(ToolTipFilter(self.live_button))
		self.live_button.setChecked(bool(self.page.session.config.get('live_preview')))
		self.live_button.toggled.connect(self.page.on_live_toggled)
		result_row.addWidget(self.live_button)
		self.preview_button = tool_button(FluentIcon.UPDATE, 'Preview this frame now', self)
		self.preview_button.clicked.connect(self.page.preview_frame)
		result_row.addWidget(self.preview_button)
		self.open_result_button = tool_button(FluentIcon.VIEW, 'Open Preview', self)
		self.open_result_button.clicked.connect(lambda: self.page.open_path(self.page.last_output))
		self.open_result_button.setEnabled(False)
		self.show_result_button = tool_button(FluentIcon.FOLDER, 'Open Preview in Explorer', self)
		self.show_result_button.clicked.connect(self.page.reveal_output)
		self.show_result_button.setEnabled(False)
		result_row.addWidget(self.open_result_button)
		result_row.addWidget(self.show_result_button)
		layout.addLayout(result_row)
		self.set_preview_progress(None)

	def set_preview_progress(self, ratio : Optional[float], text : str = '', tip : str = '') -> None:
		"""None hides the bar, a negative ratio shows the indeterminate bar, 0 to 1 shows progress"""
		busy = ratio is not None and ratio < 0
		self.busy_bar.setVisible(busy)
		if busy:
			self.busy_bar.start()
		else:
			self.busy_bar.stop()
		self.progress_bar.setVisible(ratio is not None and ratio >= 0)
		if ratio is not None and ratio >= 0:
			self.progress_bar.setValue(int(ratio * 1000))
		self.progress_label.setVisible(ratio is not None)
		self.progress_label.setText(text or ('Previewing…' if busy else '%d%%' % round(ratio * 100) if ratio is not None else ''))
		self.progress_label.setToolTip(tip)
		# not on the full bar a finished preview shows for a moment
		self.preview_stop_button.setVisible(ratio is not None and ratio < 1)
		self.result_label.setVisible(ratio is None)

	def set_result(self, path : str, detail : str = '') -> None:
		self.result_text = ('Result: %s' % os.path.basename(path) + ('  ·  ' + detail if detail else '')) if path else 'No result yet'
		self.result_label.setText(self.result_text)
		self.result_label.setToolTip(path)
		self.open_result_button.setEnabled(bool(path))
		self.show_result_button.setEnabled(bool(path))

	def set_blocked(self, text : str) -> None:
		# a refused target replaces the result line until the target changes or a check finds nothing
		self.result_label.setText(text or self.result_text)

	def browse(self) -> None:
		path, _ = QFileDialog.getOpenFileName(self, 'Choose target image or video', self.page.last_dir(), 'Images, videos and GIFs (*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv *.gif);;All files (*)')
		if path:
			self.page.set_target(os.path.normpath(path))

	def on_preview_clicked(self) -> None:
		if not self.page.target_path:
			self.browse()

	def pick_view(self, view : str) -> None:
		"""clicking the view that is already shown flips to the other one, so target and result can be compared without moving the mouse"""
		if self.page.view == view:
			view = 'result' if view == 'target' else 'target'
		self.page.show_view(view)

	def load(self, path : str) -> None:
		if self.reader:
			self.reader.release()
			self.reader = None
		kind = media_kind(path) if path else None
		self.timeline.setVisible(kind == 'video')
		self.frame_tools.setVisible(kind == 'video')
		self.info_label.setText('')
		if kind == 'video':
			self.reader = VideoReader(path)
			self.slider.blockSignals(True)
			self.slider.setRange(0, max(0, self.reader.frame_count - 1))
			self.slider.setValue(int(self.page.session.values.get('reference_frame_index') or 0) if self.reader.frame_count else 0)
			self.slider.blockSignals(False)
			self.show_frame()
		elif kind == 'image':
			frame = read_image(path)
			self.preview.set_pixmap(to_pixmap(frame))
			self.preview.set_faces([])
			if frame is not None:
				self.info_label.setText('%d × %d · image' % (frame.shape[1], frame.shape[0]))
		else:
			self.preview.set_pixmap(to_pixmap(None))
			self.preview.set_faces([])
		self.refresh_markers()

	def current_frame_index(self) -> int:
		return self.slider.value() if self.reader else 0

	def current_frame(self):
		if self.reader:
			return self.reader.read(self.current_frame_index())
		if self.page.target_path:
			return read_image(self.page.target_path)
		return None

	def show_frame(self) -> None:
		if not self.reader:
			return
		index = self.slider.value()
		frame = self.reader.read(index)
		if self.page.view == 'target':
			self.preview.set_pixmap(to_pixmap(frame))
			self.preview.set_faces([])
		else:
			self.page.on_result_scrubbed()
		self.time_label.setText('%s · #%d' % (format_timecode(index, self.reader.fps), index))
		if frame is not None:
			self.info_label.setText('%d × %d · %.2f fps · %d frames' % (frame.shape[1], frame.shape[0], self.reader.fps, self.reader.frame_count))

	def use_reference_frame(self) -> None:
		self.page.session.set_value('reference_frame_index', self.current_frame_index())
		self.page.session.notify.emit('success', 'Reference frame set', 'Faces will be matched against frame #%d.' % self.current_frame_index())

	def set_trim(self, dest : str) -> None:
		self.page.session.set_value(dest, self.current_frame_index())

	def clear_trim(self) -> None:
		self.page.session.set_value('trim_frame_start', None)
		self.page.session.set_value('trim_frame_end', None)

	def refresh_markers(self) -> None:
		values = self.page.session.values
		parts = [ 'Reference #%s' % (values.get('reference_frame_index') or 0) ]
		start, end = values.get('trim_frame_start'), values.get('trim_frame_end')
		if start is not None or end is not None:
			parts.append('Trim %s → %s' % ('start' if start is None else '#%d' % start, 'end' if end is None else '#%d' % end))
		self.marker_label.setText('   ·   '.join(parts))
		self.marker_label.setToolTip(' · '.join(parts))


class NameTagsView(FlyoutViewBase):
	"""explains the file name tags with what each one stands for right now, a click on a tag or an example uses it"""

	def __init__(self, page : 'StudioPage') -> None:
		super().__init__()
		self.page = page
		self.setFixedWidth(580)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(20, 16, 20, 18)
		layout.setSpacing(8)
		layout.addWidget(StrongBodyLabel('File name tags', self))
		intro = CaptionLabel('Type tags in curly braces in the File name box, each one is replaced when the run starts. The extension comes from the target unless you type one. Leave the box empty for the automatic name, %s.' % AUTOMATIC_TEMPLATE, self)
		intro.setWordWrap(True)
		layout.addWidget(intro)

		values = page.name_tag_values()
		# without a target or source the examples would lose those parts, so placeholders stand in
		for tag in [ 'target', 'source' ]:
			values[tag] = values.get(tag) or tag
		grid = QGridLayout()
		grid.setHorizontalSpacing(12)
		grid.setVerticalSpacing(4)
		grid.setColumnStretch(1, 1)
		for row, (tag, description) in enumerate(NAME_TAGS):
			button = PushButton('{%s}' % tag, self)
			button.setToolTip('Insert {%s} at the cursor' % tag)
			button.installEventFilter(ToolTipFilter(button))
			button.clicked.connect(lambda _ = False, text = '{%s}' % tag: self.insert(text))
			label = CaptionLabel(description, self)
			label.setWordWrap(True)
			value = CaptionLabel('1, 2, 3 …' if tag == 'n' else values.get(tag) or '(empty)', self)
			value.setTextColor('#707070', '#9a9a9a')
			value.setMaximumWidth(150)
			grid.addWidget(button, row, 0)
			grid.addWidget(label, row, 1)
			grid.addWidget(value, row, 2, Qt.AlignmentFlag.AlignRight)
		layout.addLayout(grid)

		layout.addSpacing(4)
		layout.addWidget(StrongBodyLabel('Examples', self))
		hint = CaptionLabel('Click one to use it. Shown with your current target, sources and settings.', self)
		hint.setTextColor('#707070', '#9a9a9a')
		layout.addWidget(hint)
		for example in NAME_EXAMPLES:
			result = os.path.basename(build_output_path(example, values, page.output_dir(), page.output_extension())[0])
			button = TransparentPushButton('%s   →   %s' % (example, result), self)
			button.clicked.connect(lambda _ = False, text = example: self.use(text))
			layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)

	def insert(self, text : str) -> None:
		edit = self.page.output_name_edit
		edit.insert(text)
		edit.setFocus()

	def use(self, text : str) -> None:
		self.page.output_name_edit.setText(text)
		self.page.output_name_edit.setFocus()


class SettingsColumn(QWidget):
	"""search, processors and option groups for one set of option values, the session's or a page's OptionStore"""

	def __init__(self, store, parent : Optional[QWidget] = None, header_widgets : Tuple[QWidget, ...] = (), expansion_key : str = '') -> None:
		super().__init__(parent)
		self.store = store
		column = QVBoxLayout(self)
		column.setContentsMargins(0, 0, 0, 0)
		column.setSpacing(10)
		self.search_edit = SearchLineEdit(self)
		self.search_edit.setPlaceholderText('Search settings')
		self.search_edit.textChanged.connect(self.apply_filter)
		search_row = QHBoxLayout()
		search_row.setContentsMargins(0, 0, 10, 0)
		search_row.addWidget(self.search_edit, 1)
		for widget in header_widgets:
			search_row.addWidget(widget)
		column.addLayout(search_row)
		self.scroll = SingleDirectionScrollArea(self, Qt.Orientation.Vertical)
		self.scroll.setWidgetResizable(True)
		self.scroll.enableTransparentBackground()
		self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
		content = QWidget()
		content.setStyleSheet('background: transparent')
		# clicking empty space takes focus away from a control, so the wheel scrolls the page again
		content.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
		layout = QVBoxLayout(content)
		layout.setContentsMargins(0, 0, 10, 0)
		layout.setSpacing(10)
		self.processor_card = self.create_processor_card(content)
		layout.addWidget(self.processor_card)
		self.favorites_panel = FavoritesPanel(store, content)
		layout.addWidget(self.favorites_panel)
		self.processor_panel = OptionPanel(store, [], expanded = [], parent = content, favorites = True, info_icons = True, short_labels = True, expansion_key = expansion_key)
		layout.addWidget(self.processor_panel)
		self.option_panel = OptionPanel(store, STUDIO_GROUPS, expanded = [ 'face_selector' ], parent = content, favorites = True, info_icons = True, short_labels = True, expansion_key = expansion_key)
		layout.addWidget(self.option_panel)
		self.no_match_label = CaptionLabel('No settings match your search. Options of processors that are switched off are hidden.', content)
		self.no_match_label.setWordWrap(True)
		self.no_match_label.setTextColor('#707070', '#9a9a9a')
		self.no_match_label.hide()
		layout.addWidget(self.no_match_label)
		layout.addStretch(1)
		self.scroll.setWidget(content)
		column.addWidget(self.scroll, 1)
		store.value_changed.connect(self.on_value_changed)
		store.schema_changed.connect(self.rebuild)
		self.rebuild()

	def create_processor_card(self, parent : QWidget) -> QWidget:
		card = SimpleCardWidget(parent)
		layout = QVBoxLayout(card)
		layout.setContentsMargins(16, 12, 12, 14)
		layout.setSpacing(10)
		header = card_header('Processors', card)
		self.processor_hint = elastic(CaptionLabel('', card))
		self.processor_hint.setTextColor('#707070', '#9a9a9a')
		header.insertWidget(1, self.processor_hint, 1)
		layout.addLayout(header)
		order_hint = CaptionLabel('Each frame goes through the processors that are on, left to right. Drag a button to change the order.', card)
		order_hint.setWordWrap(True)
		order_hint.setTextColor('#707070', '#9a9a9a')
		layout.addWidget(order_hint)
		self.processor_area = ProcessorArea(card)
		self.processor_area.reordered.connect(self.reorder_processors)
		layout.addWidget(self.processor_area)
		self.processor_buttons = {}
		return card

	def apply_filter(self, text : str) -> None:
		self.processor_panel.set_filter(text)
		self.option_panel.set_filter(text)
		searching = bool(text.strip())
		# while searching the matches show in their own groups, the copies in favorites would only repeat them
		self.favorites_panel.setVisible(not searching)
		self.processor_card.setVisible(not searching)
		self.no_match_label.setVisible(searching and not self.processor_panel.has_visible_cards() and not self.option_panel.has_visible_cards())
		self.scroll.verticalScrollBar().setValue(0)

	def rebuild(self) -> None:
		schema = self.store.schema
		self.rebuild_processors()
		self.processor_panel.set_groups(list(schema.processors) if schema else [])
		self.processor_panel.expanded = self.store.processors()
		self.processor_panel.rebuild()
		self.option_panel.rebuild()
		self.favorites_panel.rebuild()

	def processor_order(self) -> List[str]:
		"""every installed processor in the order shown, the ones switched on run in this order"""
		schema = self.store.schema
		if not schema:
			return []
		# without a stored order the current run order leads, so nothing changes for existing setups
		stored = self.store.config.get(self.store.ORDER_KEY) or self.store.processors()
		order = [ name for name in stored if name in schema.processors ]
		return order + [ name for name in schema.processors if name not in order ]

	def rebuild_processors(self) -> None:
		self.processor_area.clear()
		self.processor_buttons = {}
		order = self.processor_order()
		for processor in order:
			button = ProcessorButton(processor, processor.replace('_', ' ').title(), self.processor_area)
			button.setChecked(processor in self.store.processors())
			button.toggled.connect(self.apply_processor_buttons)
			self.processor_buttons[processor] = button
		self.processor_area.set_buttons([ self.processor_buttons[name] for name in order ])
		if order:
			# keep the run order in line with the buttons
			processors = [ name for name in order if name in self.store.processors() ]
			if processors != self.store.processors():
				self.store.set_value('processors', processors)
		self.refresh_processor_hint()

	def reorder_processors(self, order : List[str]) -> None:
		self.store.config.set(self.store.ORDER_KEY, order)
		self.processor_area.set_buttons([ self.processor_buttons[name] for name in order ])
		self.apply_processor_buttons()

	def apply_processor_buttons(self) -> None:
		processors = [ button.name for button in self.processor_area.buttons if button.isChecked() ]
		self.store.config.set(self.store.ORDER_KEY, [ button.name for button in self.processor_area.buttons ])
		if processors != self.store.processors():
			self.store.set_value('processors', processors)

	def refresh_processor_hint(self) -> None:
		processors = self.store.processors()
		for button in self.processor_buttons.values():
			button.set_position(processors.index(button.name) + 1 if button.name in processors else 0)
		self.processor_hint.setText(' → '.join(name.replace('_', ' ') for name in processors) if processors else 'Switch on at least one processor')
		self.processor_hint.setToolTip(self.processor_hint.text())

	def on_value_changed(self, dest : str, _ : object) -> None:
		if dest == 'processors':
			for name, button in self.processor_buttons.items():
				button.blockSignals(True)
				button.setChecked(name in self.store.processors())
				button.blockSignals(False)
			self.refresh_processor_hint()


class StudioPage(SourceSelection, QWidget):
	def __init__(self, session : Session, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.setObjectName('studio')
		self.session = session
		self.load_sources()
		self.target_path : str = session.config.get('target_path') or ''
		if self.target_path and not os.path.exists(self.target_path):
			self.target_path = ''
		self.view = 'target'
		self.last_output = ''
		self.pending_output = ''
		self.preview_mode = False
		# what the shown result was made from, so the compare slider pairs it with the right target frame
		self.result_info : dict = {}
		self.pending_info : dict = {}
		self.result_reader : Optional[VideoReader] = None
		self.blocked_target = ''
		# the last nsfw check result for the target, and whether that check followed a refused run
		self.content_result : Optional[dict] = None
		self.check_after_run = False
		self.check_target_is_frame = False
		self.check_path = ''
		# each target is checked once when it is loaded, a check that has to wait for a running task follows it
		self.checked_targets : set = set()
		self.auto_check = False
		self.auto_check_pending = False
		self.live_after_check = False
		# live preview: changes restart a short timer, a change during a running preview queues one more
		self.live_timer = QTimer(self)
		self.live_timer.setSingleShot(True)
		self.live_timer.setInterval(600)
		self.live_timer.timeout.connect(self.run_live_preview)
		self.live_pending = False
		# facefusion prints no progress for a single image, so a preview's bar follows how long previews took before
		self.preview_clock = QTimer(self)
		self.preview_clock.setInterval(100)
		self.preview_clock.timeout.connect(self.tick_preview_progress)
		self.preview_started = 0.0
		self.preview_floor = 0.0
		self.preview_estimate = 'preview_seconds'
		# face detection on the shown frame, rerun shortly after the frame or a face setting changes
		self.face_probe = FaceProbe(session, self)
		self.face_probe.scanned.connect(self.on_faces_scanned)
		self.face_probe.loading.connect(self.on_faces_loading)
		self.faces_timer = QTimer(self)
		self.faces_timer.setSingleShot(True)
		self.faces_timer.setInterval(250)
		self.faces_timer.timeout.connect(self.scan_faces)
		self.face_probe.people_found.connect(self.on_people_found)
		self.face_probe.people_progress.connect(lambda done, total: self.mapping_card.set_finding(done, total))
		# the faces of the last frame scan, a right click on a box names the face it belongs to
		self.shown_faces : List[dict] = []
		# a face mapping run: one facefusion run per mapped person, each on the original target, composed into one at the end
		self.chain : Optional[dict] = None
		self.chain_id = 0

		root = QVBoxLayout(self)
		root.setContentsMargins(24, 16, 24, 16)
		root.setSpacing(12)

		title_row = QHBoxLayout()
		title_row.addWidget(SubtitleLabel('Studio', self))
		self.version_label = CaptionLabel('', self)
		self.version_label.setTextColor('#707070', '#9a9a9a')
		title_row.addWidget(self.version_label)
		title_row.addStretch(1)
		root.addLayout(title_row)

		self.splitter = Splitter(Qt.Orientation.Horizontal, self)
		root.addWidget(self.splitter, 1)

		# left column: media
		left_widget = QWidget(self.splitter)
		left_widget.setMinimumWidth(480)
		left = QVBoxLayout(left_widget)
		left.setContentsMargins(0, 0, 0, 0)
		left.setSpacing(12)
		self.source_card = SourceCard(self)
		self.target_card = TargetCard(self)
		self.mapping_card = FaceMappingCard(self, self.find_people)
		self.mapping_card.changed.connect(self.on_mapping_changed)
		left.addWidget(self.source_card)
		left.addWidget(self.target_card, 1)
		left.addWidget(self.mapping_card)
		left.addWidget(self.create_output_card())
		self.splitter.addWidget(left_widget)

		# right column: search, processors + options
		self.settings_column = SettingsColumn(session, self.splitter, expansion_key = 'studio_groups_expanded')
		self.settings_column.setMinimumWidth(340)
		self.splitter.addWidget(self.settings_column)
		self.splitter.setStretchFactor(0, 11)
		self.splitter.setStretchFactor(1, 9)
		self.splitter.setSizes(session.config.get('studio_split') or [ 1100, 900 ])
		self.splitter.splitterMoved.connect(lambda *_: self.session.config.set('studio_split', self.splitter.sizes()))

		root.addWidget(self.create_run_bar())

		session.value_changed.connect(self.on_value_changed)
		session.schema_changed.connect(self.on_schema_changed)
		session.busy_changed.connect(self.on_busy_changed)
		session.runner.progress.connect(self.on_progress)
		session.runner.log.connect(self.on_log)
		session.runner.finished.connect(self.on_finished)
		session.content_checked.connect(self.on_content_checked)

		self.source_card.refresh(self.source_paths)
		self.target_card.load(self.target_path)
		self.load_mapping()
		self.on_schema_changed()
		self.refresh_name_preview()
		self.schedule_face_scan()

	# layout pieces

	def create_output_card(self) -> QWidget:
		card = SimpleCardWidget(self)
		column = QVBoxLayout(card)
		column.setContentsMargins(16, 10, 12, 10)
		column.setSpacing(4)
		layout = QHBoxLayout()
		layout.setSpacing(8)
		column.addLayout(layout)
		layout.addWidget(StrongBodyLabel('Output', card))
		layout.addSpacing(8)
		self.output_dir_edit = LineEdit(card)
		self.output_dir_edit.setText(self.session.config.get('output_dir') or '')
		self.output_dir_edit.setPlaceholderText('Output folder')
		self.output_dir_edit.editingFinished.connect(lambda: self.session.config.set('output_dir', self.output_dir_edit.text().strip()))
		self.output_dir_edit.textChanged.connect(lambda _: self.refresh_name_preview())
		layout.addWidget(self.output_dir_edit, 3)
		browse_button = add_edit_button(self.output_dir_edit, FluentIcon.FOLDER, 'Choose output folder')
		browse_button.clicked.connect(self.browse_output_dir)
		self.output_name_edit = LineEdit(card)
		self.output_name_edit.setPlaceholderText('File name (automatic)')
		self.output_name_edit.setClearButtonEnabled(True)
		self.output_name_edit.setText(self.session.config.get('output_name') or '')
		self.output_name_edit.textChanged.connect(self.on_output_name_changed)
		layout.addWidget(self.output_name_edit, 2)
		self.tags_button = add_edit_button(self.output_name_edit, FluentIcon.TAG, 'File name tags and examples')
		self.tags_button.clicked.connect(self.show_name_tags)
		self.subfolders_button = TogglePushButton('Sub-Folders', card)
		self.subfolders_button.setToolTip('Save into <output folder>\\<source name>\\Images or Videos, created as needed')
		self.subfolders_button.installEventFilter(ToolTipFilter(self.subfolders_button))
		self.subfolders_button.setChecked(bool(self.session.config.get('output_subfolders')))
		self.subfolders_button.toggled.connect(self.on_subfolders_toggled)
		layout.addWidget(self.subfolders_button)
		open_button = tool_button(FluentIcon.FOLDER_ADD, 'Open output folder', card)
		open_button.clicked.connect(self.open_output_folder)
		layout.addWidget(open_button)
		# what the file will be called, so tags can be checked before a run
		self.name_preview_label = elastic(CaptionLabel('', card))
		self.name_preview_label.setTextColor('#707070', '#9a9a9a')
		self.name_preview_label.installEventFilter(ToolTipFilter(self.name_preview_label))
		column.addWidget(self.name_preview_label)
		return card

	def create_run_bar(self) -> QWidget:
		card = SimpleCardWidget(self)
		layout = QHBoxLayout(card)
		layout.setContentsMargins(16, 12, 16, 12)
		layout.setSpacing(10)
		self.start_button = PrimaryPushButton(FluentIcon.PLAY, 'Start', card)
		self.start_button.setFixedWidth(130)
		self.start_button.clicked.connect(self.start)
		self.stop_button = PushButton(FluentIcon.CLOSE, 'Stop', card)
		self.stop_button.clicked.connect(self.stop)
		self.stop_button.setEnabled(False)
		self.check_button = PushButton(FluentIcon.CERTIFICATE, 'NSFW Check', card)
		self.check_button.setToolTip("Run FaceFusion's NSFW filter on the target (only the trimmed part of a video) without processing it, to find out before a long run whether it will be refused")
		self.check_button.installEventFilter(ToolTipFilter(self.check_button))
		self.check_button.clicked.connect(lambda: self.check_content(False))
		layout.addWidget(self.start_button)
		layout.addWidget(self.stop_button)
		layout.addWidget(self.check_button)
		layout.addSpacing(10)

		status_layout = QVBoxLayout()
		status_layout.setSpacing(6)
		labels = QHBoxLayout()
		self.status_label = elastic(BodyLabel('Ready', card))
		self.detail_label = elastic(CaptionLabel('', card))
		self.detail_label.setTextColor('#707070', '#9a9a9a')
		self.detail_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
		labels.addWidget(self.status_label, 1)
		labels.addWidget(self.detail_label, 1)
		status_layout.addLayout(labels)
		self.progress_bar = ProgressBar(card)
		self.progress_bar.setRange(0, 1000)
		self.progress_bar.setValue(0)
		status_layout.addWidget(self.progress_bar)
		layout.addLayout(status_layout, 1)
		layout.addSpacing(10)
		self.open_output_button = PushButton(FluentIcon.FOLDER, 'Open Output Folder', card)
		self.open_output_button.setToolTip('Open the folder the results are saved to in File Explorer')
		self.open_output_button.installEventFilter(ToolTipFilter(self.open_output_button))
		self.open_output_button.clicked.connect(self.open_output_folder)
		layout.addWidget(self.open_output_button)
		return card

	# state

	def last_dir(self) -> str:
		for path in [ self.target_path ] + self.source_paths:
			if path:
				return os.path.dirname(path)
		return os.path.expanduser('~')

	def on_sources_changed(self, change : str) -> None:
		if change == 'selection':
			self.mapping_card.refresh_text()
		else:
			self.mapping_card.refresh_sources()
		self.refresh_name_preview()
		if change != 'names':
			self.schedule_live_preview()

	def set_target(self, path : str) -> None:
		if path and media_kind(path) not in [ 'image', 'video' ]:
			self.session.notify.emit('warning', 'Unsupported target', 'Choose an image or a video.')
			return
		if path != self.target_path:
			self.session.set_value('reference_frame_index', 0)
			self.session.set_value('trim_frame_start', None)
			self.session.set_value('trim_frame_end', None)
		self.target_path = path
		self.content_result = None
		self.target_card.set_blocked('')
		self.session.config.set('target_path', path)
		# a typed name belongs to the old target, a name made of tags works for any target
		if '{' not in self.output_name_edit.text():
			self.output_name_edit.clear()
		self.refresh_name_preview()
		self.show_view('target')
		self.target_card.load(path)
		self.load_mapping()
		self.auto_check_content()
		self.schedule_live_preview()

	def show_view(self, view : str) -> None:
		self.view = view
		self.target_card.view_switch.setCurrentItem(view)
		if view == 'result':
			self.target_card.preview.set_faces([])
			self.refresh_result()
		else:
			self.release_result_reader()
			self.target_card.preview.hint = 'Drop a target image or video here\nor click to browse'
			self.target_card.preview.set_badge('')
			self.schedule_face_scan()
			if self.target_card.reader:
				self.target_card.show_frame()
			else:
				self.target_card.load(self.target_path)

	def refresh_result(self) -> None:
		preview = self.target_card.preview
		info = self.result_info
		before = after = None
		hint = 'No result yet — press Start, or the refresh button for a preview'
		if self.last_output and os.path.exists(self.last_output):
			same_target = info.get('target') == self.target_path
			if info.get('preview'):
				before = info.get('frame')
				after = read_image(self.last_output)
			elif media_kind(self.last_output) == 'video':
				if not self.result_reader or self.result_reader.path != self.last_output:
					self.release_result_reader()
					self.result_reader = VideoReader(self.last_output)
				frame_index = self.target_card.current_frame_index() - info.get('trim_start', 0)
				after = self.result_reader.read(frame_index) if frame_index >= 0 else None
				before = self.target_card.current_frame() if same_target else None
				hint = 'This frame is outside the trimmed range of the result'
			else:
				after = read_image(self.last_output)
				before = read_image(self.target_path) if same_target else None
		if after is not None and before is not None:
			preview.set_compare(to_pixmap(before), to_pixmap(after))
			preview.set_badge('')
		else:
			preview.set_pixmap(to_pixmap(after))
			preview.set_badge('Result' if after is not None else '')
			preview.hint = hint

	def on_result_scrubbed(self) -> None:
		# a preview shows one frame only, scrubbing away from it goes back to the target
		if self.result_info.get('preview'):
			self.show_view('target')
		else:
			self.refresh_result()

	def release_result_reader(self) -> None:
		# an open reader keeps the output file locked on windows
		if self.result_reader:
			self.result_reader.release()
			self.result_reader = None

	def on_schema_changed(self) -> None:
		schema = self.session.schema
		self.version_label.setText(('FaceFusion %s' % schema.version) if schema else 'FaceFusion is not set up yet — open Setup')

	def on_value_changed(self, dest : str, _ : object) -> None:
		if self.session.schema and self.session.schema.options.get(dest, {}).get('group') not in LIVE_IGNORED_GROUPS and dest not in [ 'reference_frame_index', 'trim_frame_start', 'trim_frame_end' ]:
			self.schedule_live_preview()
		if dest == 'processors' or dest.endswith('_model'):
			self.refresh_name_preview()
		if dest in [ 'reference_frame_index', 'trim_frame_start', 'trim_frame_end' ]:
			self.target_card.refresh_markers()
		if dest == 'reference_frame_index' or self.session.schema and self.session.schema.options.get(dest, {}).get('group') in FACE_GROUPS + MASK_GROUPS:
			self.schedule_face_scan()

	# running

	def output_dir(self) -> str:
		return self.output_dir_edit.text().strip() or self.session.config.get('output_dir') or os.path.expanduser('~')

	def name_tag_values(self) -> Dict[str, str]:
		"""what each file name tag stands for right now"""
		sources = self.active_sources()
		ordered = [ path for path in sources if media_kind(path) == 'image' ] + [ path for path in sources if media_kind(path) != 'image' ]
		values = run_tag_values(self.session)
		values.update(
		{
			'target': os.path.splitext(os.path.basename(self.target_path))[0],
			'source': '+'.join(self.source_name(path) for path in ordered),
			'frame': str(self.target_card.current_frame_index())
		})
		return values

	def output_extension(self) -> str:
		if media_kind(self.target_path) == 'image' and any(media_kind(path) == 'audio' for path in self.active_sources()):
			return '.mp4'
		return os.path.splitext(self.target_path)[1] or '.mp4'

	def build_output(self) -> Tuple[str, bool]:
		"""the output path and whether it replaces a file, inside <source name>\\Images or Videos when Sub-Folders is on"""
		return build_output(self.output_name_edit.text(), self.name_tag_values(), self.output_dir(), self.output_extension(), self.subfolders_button.isChecked())

	def suggest_output_path(self) -> str:
		return self.build_output()[0]

	def on_output_name_changed(self, text : str) -> None:
		self.session.config.set('output_name', text.strip())
		self.refresh_name_preview()

	def on_subfolders_toggled(self, checked : bool) -> None:
		self.session.config.set('output_subfolders', checked)
		self.refresh_name_preview()

	def open_output_folder(self) -> None:
		# with Sub-Folders on, the folder the next result goes to once a run has made it
		folder = os.path.dirname(self.suggest_output_path()) if self.target_path else ''
		self.open_folder(folder if os.path.isdir(folder) else self.output_dir())

	def refresh_name_preview(self) -> None:
		if not hasattr(self, 'name_preview_label'):
			return
		template = self.output_name_edit.text()
		if not self.target_path:
			self.name_preview_label.setText('Choose a target to see the file name')
			self.name_preview_label.setToolTip('')
			return
		path, replaces = self.build_output()
		notes = [ 'automatic name, click the tag button to build your own' ] if not template.strip() else []
		if replaces:
			notes.append('replaces the existing file, add {n} to keep both')
		unknown = unknown_tags(template)
		if unknown:
			notes.append('unknown tag %s is kept as typed' % ', '.join(unknown))
		name = os.path.relpath(path, self.output_dir()) if self.subfolders_button.isChecked() else os.path.basename(path)
		self.name_preview_label.setText('Saves As:  %s%s' % (name, ('   ·   ' + '   ·   '.join(notes)) if notes else ''))
		self.name_preview_label.setToolTip(path)

	def show_name_tags(self) -> None:
		Flyout.make(NameTagsView(self), self.tags_button, self.window(), FlyoutAnimationType.PULL_UP)

	def find_problem(self) -> Optional[tuple]:
		if not self.session.schema:
			return 'error', 'FaceFusion not set up', 'Open Setup and point the app at your FaceFusion v4 folder.'
		if not self.target_path:
			return 'warning', 'No target', 'Choose a target image or video first.'
		if not self.session.processors():
			return 'warning', 'No processor', 'Switch on at least one processor.'
		needs_source = any(processor in self.session.processors() for processor in [ 'face_swapper', 'deep_swapper', 'lip_syncer' ])
		if needs_source and not self.active_sources() and 'deep_swapper' not in self.session.processors() and not self.mapping_passes():
			if self.source_paths:
				return 'warning', 'No source selected', 'Click a source thumbnail to use it.'
			return 'warning', 'No source', 'Add at least one source face image.'
		return None

	def validate(self) -> bool:
		problem = self.find_problem()
		if problem:
			self.session.notify.emit(*problem)
		return problem is None

	# live preview

	def on_live_toggled(self, checked : bool) -> None:
		self.session.config.set('live_preview', checked)
		self.live_pending = False
		if checked:
			problem = self.find_problem()
			if problem:
				self.session.notify.emit('info', 'Live preview is on', 'It starts once this is fixed: ' + problem[2])
			self.schedule_live_preview()
		else:
			self.live_timer.stop()

	def schedule_live_preview(self) -> None:
		# whatever can change a preview can change which faces are found
		self.schedule_face_scan()
		if self.session.config.get('live_preview'):
			self.live_timer.start()

	def run_live_preview(self) -> None:
		if self.chain:
			# between two passes nothing runs, the preview follows the last one
			self.live_pending = True
			return
		if self.session.runner.is_running():
			# let the running preview finish and show, then run once more with the latest settings
			self.live_pending = self.preview_mode and self.session.facefusion_running
			# a check of a newly loaded target runs first, the preview follows unless the target is refused
			self.live_after_check = self.live_after_check or self.session.content_check_owner == 'studio'
			return
		if self.find_problem() is None:
			self.preview_frame()

	def stop(self) -> None:
		self.live_pending = False
		self.live_timer.stop()
		if self.chain and not self.session.runner.is_running():
			self.end_chain()
			self.status_label.setText('Stopped')
			self.detail_label.setText('')
			return
		self.session.runner.stop()

	def start(self) -> None:
		if not self.validate():
			return
		self.live_timer.stop()
		self.live_pending = False
		output_path = self.suggest_output_path()
		# facefusion only finds out at the very end, after processing the whole target
		if not folder_is_writable(os.path.dirname(output_path)):
			self.session.notify.emit('error', 'Cannot save to this folder', 'Windows does not allow saving to %s. Choose another output folder, such as Videos or Documents.' % os.path.dirname(output_path))
			return
		passes = self.mapping_passes()
		if passes:
			if extension(self.target_path) in GIF_EXTENSIONS:
				self.session.notify.emit('warning', 'Face mapping', 'Face mapping does not work on GIF targets yet. Clear the mapping to run as usual.')
				return
			info = { 'target': self.target_path, 'trim_start': int(self.session.values.get('trim_frame_start') or 0) }
			self.start_chain(passes, self.target_path, output_path, info, preview = False)
			return
		args = [ 'run' ]
		if self.active_sources():
			args += [ '-s' ] + self.active_sources()
		self.pending_output = output_path
		self.pending_info = { 'target': self.target_path, 'trim_start': int(self.session.values.get('trim_frame_start') or 0) }
		self.preview_mode = False
		task = 'Processing ' + os.path.basename(self.target_path)
		if extension(self.target_path) in GIF_EXTENSIONS:
			# facefusion has no gif support, the helper converts to video and back around the run
			work_dir = os.path.join(PREVIEW_DIR, 'gif')
			args = [ self.target_path, output_path, work_dir, '--' ] + args + self.session.option_args()
			script = os.path.join(APP_DIR, 'gif_run.py')
		else:
			args += [ '-t', self.target_path, '-o', output_path ] + self.session.option_args()
			script = 'facefusion.py'
		# a run that never started has no finish, the next one must not be taken for it
		if not self.session.run_facefusion(task, args, script = script):
			self.pending_output = ''

	def preview_frame(self) -> None:
		if not self.validate():
			return
		frame = self.target_card.current_frame()
		if frame is None:
			self.session.notify.emit('error', 'Preview', 'Could not read the current frame.')
			return
		height, width = frame.shape[:2]
		scale = PREVIEW_MAX_SIDE / min(height, width)
		if scale < 1:
			frame = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation = cv2.INTER_AREA)
		os.makedirs(PREVIEW_DIR, exist_ok = True)
		frame_path = os.path.join(PREVIEW_DIR, 'frame.png')
		output_path = os.path.join(PREVIEW_DIR, 'result.png')
		for path in [ frame_path, output_path ]:
			if os.path.exists(path):
				os.remove(path)
		write_image(frame_path, frame)
		info = { 'preview': True, 'target': self.target_path, 'frame': frame, 'frame_index': self.target_card.current_frame_index() }
		passes = self.mapping_passes()
		if passes:
			self.start_chain(passes, frame_path, output_path, info, preview = True)
			return
		values = dict(self.session.values)
		values.update({ 'reference_frame_index': 0, 'trim_frame_start': None, 'trim_frame_end': None, 'workflow_mode': 'auto' })
		# a single frame has no sound to sync lips to
		processors = self.session.processors()
		values['processors'] = [ processor for processor in processors if processor != 'lip_syncer' ]
		if processors and not values.get('processors'):
			self.session.notify.emit('warning', 'Preview', 'Lip sync needs the whole video, a single frame has nothing to preview. Click Start instead.')
			return
		args = [ 'run' ]
		image_sources = [ path for path in self.active_sources() if media_kind(path) == 'image' ]
		if image_sources:
			args += [ '-s' ] + image_sources
		args += [ '-t', frame_path, '-o', output_path ] + self.session.option_args(values)
		self.pending_output = output_path
		self.pending_info = info
		self.preview_mode = True
		if not self.session.run_facefusion('Previewing frame #%d' % self.target_card.current_frame_index(), args, preview = True):
			self.pending_output = ''

	def on_busy_changed(self, busy : bool, task : str) -> None:
		self.start_button.setEnabled(not busy and not self.chain)
		self.check_button.setEnabled(not busy)
		self.target_card.preview_button.setEnabled(not busy)
		# a preview, Studio's or Batch's, shows its progress on its own preview card and leaves the run bar as it was
		run_bar = not self.session.task_is_preview
		self.stop_button.setEnabled(busy and (run_bar or bool(self.pending_output)))
		if busy and run_bar:
			self.status_label.setText(task)
			self.detail_label.setText('Starting…')
			self.progress_bar.setValue(0)
			self.progress_bar.resume()
		if busy and self.preview_mode and self.pending_output:
			self.preview_started = time.monotonic()
			self.preview_floor = 0.0
			self.preview_estimate = self.session.preview_key('preview_seconds')
			self.preview_clock.start()
			self.tick_preview_progress()
		elif self.preview_clock.isActive():
			# show the bar full for a moment so a finished preview does not look cut off
			self.preview_clock.stop()
			self.target_card.set_preview_progress(1.0)
			QTimer.singleShot(400, lambda: None if self.preview_clock.isActive() else self.target_card.set_preview_progress(None))
		else:
			self.target_card.set_preview_progress(None)
		if not busy and self.auto_check_pending:
			QTimer.singleShot(0, self.auto_check_content)

	def tick_preview_progress(self) -> None:
		"""moves the preview bar along the time previews usually take, slowing down near the end instead of stopping"""
		elapsed = time.monotonic() - self.preview_started
		expected = self.session.expected_preview_seconds(self.preview_estimate)
		ratio = estimated_progress(elapsed, expected, self.preview_floor)
		left = expected - elapsed
		text = '%d%%' % (ratio * 100) + ('  ·  about %d s left' % round(left) if left >= 1 and self.preview_floor < 0.9 else '')
		self.target_card.set_preview_progress(ratio, text, 'Estimated from how long your previews took before (about %.1f s), FaceFusion does not report progress for a single frame' % expected)

	def on_progress(self, title : str, ratio : float, status : str) -> None:
		if not self.session.task or self.session.task_is_preview:
			return
		self.status_label.setText('%s · %s' % (self.session.task, title) if title else self.session.task)
		self.detail_label.setText('%d%%  %s' % (round(ratio * 100), status))
		if not self.preview_clock.isActive():
			self.progress_bar.setValue(int(ratio * 1000))

	def on_log(self, line : str) -> None:
		if self.session.task and not self.session.task_is_preview and not line.startswith('Traceback'):
			self.detail_label.setText(line[-110:])
		# the last steps of a preview move its bar forward
		if self.preview_clock.isActive():
			self.preview_floor = preview_step_floor(line, self.preview_floor)
			self.tick_preview_progress()

	def on_finished(self, exit_code : int, elapsed : float) -> None:
		if not self.pending_output:
			return
		output_path, self.pending_output = self.pending_output, ''
		if self.live_pending:
			self.live_pending = False
			self.schedule_live_preview()
		if self.chain:
			chain = self.chain
			if exit_code == 0 and os.path.exists(output_path) and chain.get('index') < len(chain.get('steps')) - 1:
				chain['index'] += 1
				QTimer.singleShot(0, self.run_chain_step)
				return
			kind = chain.get('steps')[chain.get('index')].get('kind')
			# the whole face mapping run took this long, not only its last step
			elapsed = time.monotonic() - chain.get('started')
			self.end_chain()
			if kind == 'compose' and exit_code not in [ 0, -1 ]:
				if not self.preview_mode:
					self.progress_bar.error()
				status, message = self.session.describe_run_error()
				self.status_label.setText(status if self.session.run_error() else 'Could not combine the passes (exit code %d)' % exit_code)
				self.session.notify.emit('error', 'Could not combine the passes', message)
				return
		if exit_code == 0 and os.path.exists(output_path):
			if self.preview_mode:
				self.session.remember_preview_seconds(self.preview_estimate, elapsed)
			self.release_result_reader()
			self.last_output = output_path
			self.result_info = self.pending_info
			if not self.preview_mode:
				self.progress_bar.setValue(1000)
				self.status_label.setText('Done — ' + os.path.basename(output_path))
				self.detail_label.setText('%.1f s' % elapsed)
			self.target_card.set_result(output_path, ('preview of frame #%d' % self.pending_info.get('frame_index', 0) if self.preview_mode else 'done') + ' in %.1f s' % elapsed)
			self.show_view('result')
			if not self.preview_mode:
				self.session.notify.emit('success', 'Finished', 'Saved %s in %.1f s' % (os.path.basename(output_path), elapsed))
				if self.session.config.get('open_output_when_done'):
					self.open_path(output_path)
		elif exit_code == -1:
			if not self.preview_mode:
				self.status_label.setText('Stopped')
				self.detail_label.setText('')
				self.progress_bar.pause()
			else:
				self.target_card.result_label.setText('Preview stopped')
		else:
			if not self.preview_mode:
				self.progress_bar.error()
			image_target = self.preview_mode or media_kind(self.pending_info.get('target') or '') == 'image'
			blocked = self.session.nsfw_block_message(image_target)
			if blocked:
				self.status_label.setText(BLOCKED_STATUS)
				self.detail_label.setText('')
				self.target_card.set_blocked(BLOCKED_STATUS)
				# live preview would repeat the same warning on every change
				if self.preview_mode and self.blocked_target == self.target_path:
					return
				self.blocked_target = self.target_path
				if self.session.nsfw_block_is_certain():
					show_blocked_bar(self.window(), blocked, 'Show flagged frames', lambda: self.check_content(True))
				else:
					# an image gives no reason when it is refused, the check confirms it and finds what was flagged
					self.detail_label.setText('Confirming with the NSFW check…')
					self.check_content(True, os.path.join(PREVIEW_DIR, 'frame.png') if self.preview_mode else '')
			else:
				status, message = self.session.describe_run_error()
				self.status_label.setText(status if self.session.run_error() else 'Failed (exit code %d)' % exit_code)
				self.detail_label.setText('')
				self.session.notify.emit('error', 'FaceFusion failed', message)

	# face detection

	def on_faces_toggled(self, checked : bool) -> None:
		self.session.config.set('show_faces', checked)
		self.on_overlay_toggled()

	def on_mask_toggled(self, checked : bool) -> None:
		self.session.config.set('show_mask', checked)
		self.on_overlay_toggled()

	def overlays_on(self) -> bool:
		return self.target_card.faces_button.isChecked() or self.target_card.mask_button.isChecked()

	def on_overlay_toggled(self) -> None:
		self.target_card.faces_label.setVisible(self.overlays_on())
		self.target_card.preview.set_faces([])
		self.shown_faces = []
		if self.overlays_on():
			self.schedule_face_scan()
		elif not self.chain and not self.face_probe.is_finding_people():
			# the probe holds its models in memory, let them go while nobody looks
			self.faces_timer.stop()
			self.face_probe.stop()

	def schedule_face_scan(self) -> None:
		if self.overlays_on():
			self.faces_timer.start()

	def scan_faces(self) -> None:
		card = self.target_card
		if not self.overlays_on() or self.view != 'target' or self.chain:
			return
		if not self.target_path:
			card.preview.set_faces([])
			card.faces_label.setText('Choose a target to see the faces FaceFusion finds in it.')
			return
		if not self.session.is_configured() or not self.session.schema:
			card.faces_label.setText('Set up FaceFusion first, face detection runs in its environment.')
			return
		frame = card.current_frame()
		if frame is None:
			return
		frame_index = card.current_frame_index()
		reference = None
		reference_index = int(self.session.values.get('reference_frame_index') or 0)
		if card.reader and self.session.values.get('face_selector_mode') == 'reference' and reference_index != frame_index:
			reference = card.reader.read(reference_index)
		sources = [ path for path in self.active_sources() if media_kind(path) == 'image' ]
		mask = card.mask_button.isChecked()
		args = self.session.option_args(groups = FACE_GROUPS + (MASK_GROUPS if mask else []))
		self.face_probe.scan(frame, reference, sources, args, tag = (self.target_path, frame_index), mask = mask, people_file = self.people_file())

	def on_faces_loading(self, loading : bool) -> None:
		if loading and self.overlays_on():
			self.target_card.faces_label.setText('Loading the face models…')

	def on_faces_scanned(self, result : dict) -> None:
		tag = result.get('tag')
		if isinstance(tag, tuple) and tag[0] == 'pass':
			self.on_pass_located(tag, result)
			return
		card = self.target_card
		if not self.overlays_on() or tag != (self.target_path, card.current_frame_index()) or self.view != 'target':
			return
		faces = (result.get('faces') or []) if card.faces_button.isChecked() else []
		mask = read_mask(result.get('mask')) if card.mask_button.isChecked() else None
		people = self.mapping_card.people
		mapped = is_mapped(people)
		boxes = []
		for index, face in enumerate(faces):
			person = face.get('person')
			if person is not None and person < len(people):
				# with a mapping a face is processed when its person has a source, the face selector is set per pass
				label = '%s → %s' % (person_name(people, person), self.source_label(people[person].get('source'))) if people[person].get('source') else person_name(people, person)
				boxes.append((face.get('box'), label, bool(people[person].get('source')) if mapped else bool(face.get('selected')), person_color(person)))
			else:
				boxes.append((face.get('box'), face_tag(index, face), bool(face.get('selected')) and not mapped))
		self.shown_faces = faces
		card.preview.set_faces(boxes, (result.get('width') or 0, result.get('height') or 0), mask)
		lines = []
		problem = bool(result.get('error'))
		if card.faces_button.isChecked() and mapped and not problem:
			found = '1 face detected' if len(faces) == 1 else '%d faces detected' % len(faces)
			named = [ face for face in faces if face.get('person') is not None and face.get('person') < len(people) ]
			swapped = [ face for face in named if people[face.get('person')].get('source') ]
			lines.append('%s, %d of them mapped. Face mapping runs one pass per mapped person, the face selector is set for each pass.' % (found, len(swapped)) + ('' if len(named) == len(faces) else ' Faces without a name are left as they are, Find people again if someone is missing.'))
		elif card.faces_button.isChecked() or problem:
			reference_index = int(self.session.values.get('reference_frame_index') or 0) if card.reader else None
			lines.append(describe_faces(result, reference_index))
			problem = problem or not any(face.get('selected') for face in result.get('faces') or [])
		if card.mask_button.isChecked() and not result.get('error'):
			schema = self.session.schema
			values = { dest: self.session.values.get(dest, schema.options.get(dest, {}).get('default')) for dest in [ 'face_mask_padding', 'face_mask_blur', 'face_mask_areas', 'face_mask_regions' ] } if schema else {}
			lines.append(describe_mask(result, values))
			problem = problem or not result.get('masked')
		card.faces_label.setText('\n'.join(lines))
		card.faces_label.setTextColor('#b45309' if problem else '#707070', '#f0b35a' if problem else '#9a9a9a')

	def show_face_menu(self, index : int, position : QPoint) -> None:
		if not 0 <= index < len(self.shown_faces):
			return
		person = self.shown_faces[index].get('person')
		people = self.mapping_card.people
		menu = RoundMenu(parent = self)
		if person is None or person >= len(people):
			menu.addAction(Action(FluentIcon.PEOPLE, 'Find people to name this face', triggered = self.find_people))
			menu.exec(position)
			return
		row = self.mapping_card.row(person)
		menu.addAction(Action(FluentIcon.EDIT, 'Rename %s' % person_name(people, person), triggered = lambda: (row.name_edit.setFocus(), row.name_edit.selectAll())))
		sources = RoundMenu('Use source', self)
		sources.setIcon(FluentIcon.PEOPLE)
		choices = [ (SELECTED, 'Selected sources') ] + [ (path, self.source_name(path)) for path in self.source_paths if media_kind(path) == 'image' ]
		for value, label in choices:
			action = Action(label, triggered = lambda _ = False, value = value: row.set_source(value))
			action.setCheckable(True)
			action.setChecked(people[person].get('source') == value)
			sources.addAction(action)
		menu.addMenu(sources)
		menu.addAction(Action(FluentIcon.CANCEL, 'Leave unchanged', triggered = lambda: row.set_source(LEAVE)))
		menu.exec(position)

	# face mapping

	def load_mapping(self) -> None:
		stored = (self.session.config.get('face_maps') or {}).get(self.target_path) if self.target_path else None
		self.mapping_card.set_people([ dict(person) for person in (stored or {}).get('people') or [] ], int((stored or {}).get('samples') or 0))

	def on_mapping_changed(self) -> None:
		maps = dict(self.session.config.get('face_maps') or {})
		if self.mapping_card.people:
			maps[self.target_path] = { 'people': self.mapping_card.people, 'samples': self.mapping_card.samples }
		else:
			maps.pop(self.target_path, None)
			shutil.rmtree(people_dir(self.target_path), ignore_errors = True)
		# a target that is gone takes its mapping along
		maps = { path: value for path, value in maps.items() if os.path.exists(path) }
		self.session.config.set('face_maps', maps)
		self.schedule_face_scan()
		self.schedule_live_preview()

	def people_file(self) -> Optional[str]:
		path = os.path.join(people_dir(self.target_path), PEOPLE_FILE) if self.target_path and self.mapping_card.people else ''
		return path if path and os.path.isfile(path) else None

	def source_label(self, choice : str) -> str:
		if choice == SELECTED:
			return 'selected sources'
		return self.source_name(choice) if choice in self.source_paths else 'missing source'

	def find_people(self) -> None:
		if not self.target_path:
			self.session.notify.emit('warning', 'No target', 'Choose a target image or video first.')
			return
		if not self.session.is_configured() or not self.session.schema:
			self.session.notify.emit('error', 'FaceFusion not set up', 'Open Setup first, face detection runs in its environment.')
			return
		start = end = None
		if media_kind(self.target_path) == 'video':
			# a later pass works on the trimmed output, so the reference frames have to be inside the trim
			start = self.session.values.get('trim_frame_start')
			end = self.session.values.get('trim_frame_end')
		crops = people_dir(self.target_path)
		shutil.rmtree(crops, ignore_errors = True)
		self.mapping_card.set_finding(0, 0)
		self.face_probe.find_people(self.target_path, start, end, self.session.option_args(groups = FACE_GROUPS), crops, tag = self.target_path)

	def on_people_found(self, result : dict) -> None:
		if result.get('tag') != self.target_path:
			return
		if result.get('error'):
			self.mapping_card.refresh_text()
			self.session.notify.emit('error', 'Find people', result.get('error'))
			return
		people = merge_people(self.mapping_card.people, result.get('people') or [])
		self.mapping_card.set_people(people, int(result.get('samples') or 0))
		self.mapping_card.set_expanded(True)
		self.on_mapping_changed()
		if not people:
			self.session.notify.emit('info', 'Find people', 'No face was found in the target. Try a lower Face detector Score or another Model.')

	def mapping_passes(self) -> List[dict]:
		"""the runs a face mapping needs, none when there is no mapping or no processor follows the face selector"""
		people = self.mapping_card.people
		if not people or not any(processor in FACE_PROCESSORS for processor in self.session.processors()):
			return []
		images = [ path for path in self.active_sources() if media_kind(path) == 'image' ]
		return plan_passes(people, images, self.source_paths, float(self.session.values.get('reference_face_distance') or 0.3))

	def start_chain(self, passes : List[dict], target : str, output_path : str, info : dict, preview : bool) -> None:
		"""finds each mapped person in the target first, then runs the steps that swap them"""
		video = not preview and media_kind(target) == 'video'
		trim_start = int(self.session.values.get('trim_frame_start') or 0) if video else 0
		trim_end = self.session.values.get('trim_frame_end')
		for item in passes:
			if not item.get('many') and video and (item.get('frame') < trim_start or trim_end is not None and item.get('frame') >= trim_end):
				self.session.notify.emit('warning', 'Face mapping', "%s's reference frame #%d is outside the trimmed part. Click Find people again." % (item.get('label'), item.get('frame')))
				return
		shutil.rmtree(PASSES_DIR, ignore_errors = True)
		os.makedirs(PASSES_DIR, exist_ok = True)
		self.chain_id += 1
		self.chain = { 'id': self.chain_id, 'passes': passes, 'located': [], 'steps': [], 'index': 0, 'target': target, 'output': output_path, 'info': info, 'preview': preview, 'video': video, 'started': time.monotonic() }
		self.start_button.setEnabled(False)
		self.locate_next()

	def end_chain(self) -> None:
		self.chain = None
		shutil.rmtree(PASSES_DIR, ignore_errors = True)
		self.start_button.setEnabled(not self.session.runner.is_running())
		if self.auto_check_pending:
			QTimer.singleShot(0, self.auto_check_content)

	def fail_chain(self, message : str) -> None:
		self.end_chain()
		self.status_label.setText('Face mapping stopped')
		self.detail_label.setText('')
		self.progress_bar.error()
		self.session.notify.emit('error', 'Face mapping', message)

	def locate_next(self) -> None:
		"""asks the probe where the next person is in their reference frame, the position depends on the sources"""
		chain = self.chain
		done = len(chain.get('located'))
		if done == len(chain.get('passes')):
			self.build_steps()
			return
		item = chain.get('passes')[done]
		if item.get('many'):
			chain['located'].append(item)
			self.locate_next()
			return
		self.status_label.setText('Face mapping · finding %s' % item.get('label'))
		self.detail_label.setText('')
		frame = read_frame(chain.get('target'), item.get('frame') if chain.get('video') else 0)
		if frame is None:
			self.fail_chain('Could not read the reference frame of %s.' % item.get('label'))
			return
		self.face_probe.scan(frame, None, item.get('sources'), self.session.option_args(groups = FACE_GROUPS), tag = ('pass', chain.get('id'), done), people_file = self.people_file())

	def on_pass_located(self, tag : tuple, result : dict) -> None:
		chain = self.chain
		if not chain or tag != ('pass', chain.get('id'), len(chain.get('located'))):
			return
		item = chain.get('passes')[len(chain.get('located'))]
		if result.get('error'):
			self.fail_chain(result.get('error'))
			return
		faces = result.get('faces') or []
		# the face of this person, where several look like them the one at the place it was found
		candidates = [ face for face in faces if face.get('person') == item.get('person') ]
		if not chain.get('preview'):
			candidates = candidates or [ face for face in faces if box_overlap(face.get('box'), item.get('box')) > 0.3 ]
			candidates.sort(key = lambda face: -box_overlap(face.get('box'), item.get('box')))
		if not candidates and chain.get('preview'):
			# this person is not in the previewed frame, there is nothing to do for them
			chain['passes'].pop(len(chain.get('located')))
			self.locate_next()
			return
		if not candidates:
			self.fail_chain('Could not find %s in frame #%d any more. Click Find people again.' % (item.get('label'), item.get('frame')))
			return
		position = candidates[0].get('position', -1)
		if position < 0:
			self.fail_chain('The face selector Gender, Race or Age filter skips %s. Change the filter, or pick another source.' % item.get('label'))
			return
		chain['located'].append(dict(item, position = position))
		self.locate_next()

	def build_steps(self) -> None:
		"""one pass runs straight to the output, several run on the target side by side and are put together

		each pass works on the original target, so a face swapped in one pass can never be picked up by the
		reference face of another (swapping two people with each other works). a base run that changes no
		face gives the frames as facefusion writes them, the changes of every pass are added onto it
		"""
		chain = self.chain
		passes = chain.get('located')
		output_path = chain.get('output')
		if not passes:
			# nobody mapped is in the previewed frame: show it as it is
			write_image(output_path, read_image(chain.get('target')))
			info = chain.get('info')
			self.end_chain()
			self.release_result_reader()
			self.last_output = output_path
			self.result_info = info
			self.status_label.setText('Preview ready')
			self.detail_label.setText('No mapped face in this frame')
			self.target_card.set_result(output_path, 'preview of frame #%d, no mapped face in it' % info.get('frame_index', 0))
			self.show_view('result')
			return
		if len(passes) == 1:
			chain['steps'] = [ { 'kind': 'pass', 'item': passes[0], 'output': output_path, 'all': True } ]
		else:
			image_target = not chain.get('video')
			# lossless in between: png, or h264 at quality 100 in mkv
			middle = '.png' if image_target else '.mkv'
			frame_processors = [ processor for processor in self.session.processors() if processor in FRAME_PROCESSORS ]
			steps = [ { 'kind': 'base', 'item': passes[0], 'output': os.path.join(PASSES_DIR, 'base' + middle) } ]
			steps += [ { 'kind': 'pass', 'item': item, 'output': os.path.join(PASSES_DIR, 'pass_%d%s' % (index + 1, middle)) } for index, item in enumerate(passes) ]
			if frame_processors:
				steps.append({ 'kind': 'compose', 'output': os.path.join(PASSES_DIR, 'combined' + middle), 'lossless': True })
				steps.append({ 'kind': 'finish', 'output': output_path, 'processors': frame_processors })
			else:
				steps.append({ 'kind': 'compose', 'output': output_path })
			chain['steps'] = steps
		self.run_chain_step()

	def chain_task(self, step : dict) -> str:
		chain = self.chain
		kind = step.get('kind')
		item = step.get('item') or {}
		count = len(chain.get('located'))
		if kind == 'pass':
			names = ', '.join(self.source_name(path) for path in item.get('sources'))
			number = chain.get('located').index(item) + 1
			what = ('pass %d of %d · ' % (number, count) if count > 1 else '') + '%s → %s' % (names, item.get('label'))
		elif kind == 'base':
			what = 'reading the frames'
		elif kind == 'compose':
			what = 'combining %d passes' % count
		else:
			what = ' → '.join(name.replace('_', ' ') for name in step.get('processors'))
		if chain.get('preview'):
			return 'Previewing frame #%d · %s' % (chain.get('info').get('frame_index', 0), what)
		return 'Processing %s · %s' % (os.path.basename(self.target_path), what[0].upper() + what[1:])

	def run_chain_step(self) -> None:
		chain = self.chain
		if not chain:
			return
		step = chain.get('steps')[chain.get('index')]
		self.pending_output = step.get('output')
		self.pending_info = chain.get('info')
		self.preview_mode = chain.get('preview')
		if step.get('kind') == 'compose':
			outputs = [ other.get('output') for other in chain.get('steps') if other.get('kind') == 'pass' ]
			base = chain.get('steps')[0].get('output')
			values = self.session.values
			settings = { 'image_quality': values.get('output_image_quality'), 'video_encoder': values.get('output_video_encoder'), 'video_quality': values.get('output_video_quality'), 'video_preset': values.get('output_video_preset'), 'audio_encoder': values.get('output_audio_encoder'), 'audio_quality': values.get('output_audio_quality'), 'audio_volume': values.get('output_audio_volume') }
			if step.get('lossless'):
				settings.update({ 'image_quality': 100, 'video_encoder': 'libx264', 'video_quality': 100, 'audio_encoder': 'flac', 'audio_volume': 100 })
			args = [ os.path.join(APP_DIR, 'compose_passes.py'), base, step.get('output'), json.dumps(settings), '--' ] + outputs
			started = self.session.run_python(self.chain_task(step), args, self.session.facefusion_root, preview = chain.get('preview'))
		else:
			started = self.launch_step(step)
		# nothing will finish this step, so the chain must not wait for it
		if not started:
			self.pending_output = ''
			self.end_chain()

	def launch_step(self, step : dict) -> bool:
		chain = self.chain
		kind = step.get('kind')
		item = step.get('item') or {}
		preview = chain.get('preview')
		values = dict(self.session.values)
		if preview:
			values.update({ 'reference_frame_index': 0, 'trim_frame_start': None, 'trim_frame_end': None, 'workflow_mode': 'auto' })
		processors = list(values.get('processors') or [])
		sources = list(item.get('sources') or [])
		target = chain.get('target')
		# lip sync needs a video target, a preview or an image has none
		audio = [ path for path in self.active_sources() if media_kind(path) == 'audio' ] if chain.get('video') else []
		if not chain.get('video'):
			processors = [ processor for processor in processors if processor != 'lip_syncer' ]
		if kind == 'finish':
			# the combined passes get the whole frame processors, already trimmed and scaled
			target = chain.get('steps')[chain.get('index') - 1].get('output')
			processors = step.get('processors')
			values.update({ 'trim_frame_start': None, 'trim_frame_end': None, 'output_video_scale': 1.0, 'output_image_scale': 1.0 })
			sources = []
			audio = []
		else:
			if not step.get('all'):
				# whole frame processors run once at the end, and nothing is lost in between
				processors = [ processor for processor in processors if processor not in FRAME_PROCESSORS ]
				# the audio too, it is encoded and its volume set once, in the last step
				values.update({ 'output_video_encoder': 'libx264', 'output_video_quality': 100, 'output_image_quality': 100, 'output_audio_encoder': 'flac', 'output_audio_volume': 100 })
			if item.get('many'):
				values['face_selector_mode'] = 'many'
			else:
				values.update({ 'face_selector_mode': 'reference', 'reference_frame_index': item.get('frame') if chain.get('video') else 0, 'reference_face_position': item.get('position'), 'reference_face_distance': item.get('distance') })
			if kind == 'base':
				# a distance of 0 matches no face, the frames go through unchanged
				values['reference_face_distance'] = 0.0
		values['processors'] = processors
		args = [ 'run' ] + ([ '-s' ] + sources + audio if sources or audio else []) + [ '-t', target, '-o', step.get('output') ] + self.session.option_args(values)
		return self.session.run_facefusion(self.chain_task(step), args, preview = preview)

	# nsfw check

	def auto_check_content(self) -> None:
		"""checks a newly loaded target once, as soon as nothing else is running"""
		self.auto_check_pending = False
		if not self.target_path or self.target_path in self.checked_targets or not self.session.is_configured():
			return
		if self.session.runner.is_running() or self.chain:
			self.auto_check_pending = True
			return
		self.checked_targets.add(self.target_path)
		self.check_content(False, auto = True)

	def check_content(self, after_run : bool, target : str = '', auto : bool = False) -> None:
		"""runs facefusion's nsfw filter on the target, or on the given file (a preview frame), without processing it"""
		target = target or self.target_path
		if not target:
			self.session.notify.emit('warning', 'No target', 'Choose a target image or video first.')
			return
		self.auto_check = auto
		self.check_path = self.target_path
		self.check_after_run = after_run
		self.check_target_is_frame = target != self.target_path
		trim_start = trim_end = None
		if media_kind(target) == 'video':
			trim_start = self.session.values.get('trim_frame_start')
			trim_end = self.session.values.get('trim_frame_end')
		self.session.run_content_check('studio', [ target ], trim_start, trim_end)

	def on_content_checked(self, owner : str, results : list, exit_code : int) -> None:
		if owner != 'studio':
			return
		result = results[0] if results else None
		live_after_check, self.live_after_check = self.live_after_check, False
		if self.check_path != self.target_path:
			# the target changed while it was checked, the new target gets its own check
			self.status_label.setText('Ready')
			self.detail_label.setText('')
			if live_after_check:
				self.schedule_live_preview()
			return
		if exit_code == -1:
			self.status_label.setText('NSFW check stopped')
			self.progress_bar.pause()
			return
		if exit_code != 0 or not result:
			self.progress_bar.error()
			self.status_label.setText('NSFW check failed (exit code %d)' % exit_code)
			self.session.notify.emit('error', 'NSFW check failed', 'See Console for the full log.')
			if live_after_check:
				self.schedule_live_preview()
			return
		if self.check_target_is_frame:
			# a preview checks the extracted frame, the dialog should name the frame on the target
			result = dict(result, path = self.target_path, kind = 'image')
			if media_kind(self.target_path) == 'video':
				result['frame_label'] = self.pending_info.get('frame_index')
		self.content_result = result
		title, message = describe_result(result, self.check_after_run)
		if result.get('blocked'):
			self.progress_bar.error()
			self.status_label.setText(BLOCKED_STATUS if self.check_after_run else '⛔ ' + title)
			self.detail_label.setText('')
			self.target_card.set_blocked(BLOCKED_STATUS if self.check_after_run else '⛔ ' + title)
			# a live preview of this target would only be refused, its warning would repeat this one
			self.blocked_target = self.target_path
			if self.auto_check:
				# loading a target should not open a dialog, the bar leads to it
				show_blocked_bar(self.window(), message, 'Show flagged frames', self.show_content_result)
			else:
				self.show_content_result()
			return
		self.target_card.set_blocked('')
		if live_after_check:
			self.schedule_live_preview()
		if self.check_after_run:
			# the run failed for some other reason
			self.status_label.setText('Failed: FaceFusion stopped without a reason')
			self.detail_label.setText('The NSFW check found nothing')
			self.session.notify.emit('error', 'FaceFusion failed', "It stopped without giving a reason, and its NSFW filter found nothing in the target. See Console for the full log.")
		else:
			self.progress_bar.setValue(1000)
			self.status_label.setText('✓ ' + title)
			self.detail_label.setText('')
			# the automatic check on loading only speaks up when something was flagged
			if not self.auto_check or result.get('flagged'):
				self.session.notify.emit('success' if not result.get('flagged') else 'info', title, message)

	def show_content_result(self) -> None:
		if self.content_result:
			NsfwDialog(self.content_result, self.check_after_run, self.window(), self.jump_to_frame).exec()

	def jump_to_frame(self, frame_index : int) -> None:
		self.show_view('target')
		self.target_card.slider.setValue(frame_index)

	def browse_output_dir(self) -> None:
		path = QFileDialog.getExistingDirectory(self, 'Choose output folder', self.output_dir())
		if path:
			self.output_dir_edit.setText(os.path.normpath(path))
			self.session.config.set('output_dir', os.path.normpath(path))

	def reveal_output(self) -> None:
		if self.last_output and os.path.exists(self.last_output):
			import subprocess
			subprocess.Popen([ 'explorer', '/select,', self.last_output ])

	@staticmethod
	def open_path(path : str) -> None:
		if path:
			QDesktopServices.openUrl(QUrl.fromLocalFile(path))

	@staticmethod
	def open_folder(path : str) -> None:
		# made first, a folder the first run has not made yet still opens
		if path:
			os.makedirs(path, exist_ok = True)
			QDesktopServices.openUrl(QUrl.fromLocalFile(path))
