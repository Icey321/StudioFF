import glob
import os
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import cv2
from PySide6.QtCore import QEvent, QFileSystemWatcher, QItemSelectionModel, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import QAbstractItemView, QFileDialog, QHBoxLayout, QListWidgetItem, QToolTip, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, FluentIcon, Flyout, FlyoutAnimationType, LineEdit, ListItemDelegate, ListWidget, MessageBox, PrimaryPushButton, ProgressBar, PushButton, SimpleCardWidget, Slider, StrongBodyLabel, SubtitleLabel, TogglePushButton, ToggleToolButton, ToolTipFilter, TransparentPushButton, getFont, isDarkTheme, themeColor

from ffstudio.config import APP_DIR, PREVIEW_DIR
from ffstudio.faces import FACE_GROUPS, MASK_GROUPS, FaceProbe, describe_faces, describe_mask, face_tag, read_mask
from ffstudio.media import GIF_EXTENSIONS, VideoReader, extension, format_length, format_timecode, media_kind, read_frame, thumbnail, to_pixmap, video_seconds, write_image
from ffstudio.nsfw import BLOCKED_TITLE, NsfwDialog, show_blocked_bar
from ffstudio.output_name import AUTOMATIC_TEMPLATE, build_output, unknown_tags
from ffstudio.pages.models import format_size
from ffstudio.pages.setup import SYSTEM_GROUPS, form_row, section_card
from ffstudio.pages.studio import LIVE_IGNORED_GROUPS, PREVIEW_MAX_SIDE, NameTagsView, SettingsColumn, SourceCard, SourceSelection, StudioPage, elastic, estimated_progress, preview_step_floor, run_tag_values, tool_button
from ffstudio.session import OptionStore, Session, explain_error, folder_is_writable
from ffstudio.widgets import PreviewView, Splitter, add_edit_button, collect_drop_paths, guard_wheel

TARGET_FILTERS = [ ('*.*', 'All files'), ('*.mp4', 'MP4 videos'), ('*.jpg', 'JPG images'), ('*.png', 'PNG images'), ('*.mov', 'MOV videos'), ('*.webm', 'WebM videos'), ('*.gif', 'GIF animations') ]
DEFAULT_TEMPLATE = '{target}_{source}'
# the placeholders of the first batch file names, read as the tags that replaced them
OLD_PLACEHOLDERS = { '{target_name}': '{target}', '{source_name}': '{source}', '{target_extension}': '', '{index}': '{n}' }
LIST_LIMIT = 500
TARGET_FILES_FILTER = 'Images, videos and GIFs (*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv *.gif);;All files (*)'
# a mark, the words before the names, and the mark's color in the light and dark theme (none follows the theme color)
STATUS_LOOK =\
{
	'done': ('✓', '', '#0f7b0f', '#6ccb5f'),
	'blocked': ('⛔', 'NSFW content detected  ·  ', '#c42b1c', '#ff99a4'),
	'skip': ('⛔', 'NSFW content detected, will be skipped  ·  ', '#9d5d00', '#fce100'),
	'failed': ('✗', 'Failed  ·  ', '#c42b1c', '#ff99a4'),
	'skipped': ('↷', 'Skipped  ·  ', '#9d5d00', '#fce100'),
	'running': ('▶', '', None, None)
}
# a run that ended keeps its row until something the user changes plans the batch again
FINISHED = ('done', 'blocked', 'failed', 'skipped')
# the outputs a hovered row offers to run again
RETRY_STATUSES = ('skipped', 'failed')
# facefusion's title for the step that makes the frames, the steps around it are quick or report nothing
PROCESSING_TITLE = 'processing'
CLOCK_TAGS = re.compile(r'\{(date|time)\}')
# the picked frame shows scaled down to this many pixels on its long side, so scrubbing stays quick. the preview runs
# on the frame at studio's preview size instead, a smaller one can change what facefusion's nsfw filter decides
PREVIEW_SIZE = 640
PREVIEW_WIDTH = 340
PREVIEW_MIN_WIDTH = 280
# at most this many removed runs and picked video frames are kept across restarts
REMOVED_LIMIT = 2000
FRAMES_LIMIT = 2000


def upgrade_template(template : str) -> str:
	for old, new in OLD_PLACEHOLDERS.items():
		template = template.replace(old, new)
	return template


class OutputList(ListWidget):
	"""the planned outputs, image and video files dropped on it are added as targets

	the row under the mouse shows a retry button over its mark when it can run again, and a remove button at its right end
	"""
	files_dropped = Signal(list)
	retry_clicked = Signal(int)
	remove_clicked = Signal(int)
	BUTTON_TIPS = { 'retry': 'Retry this output', 'remove': 'Remove this output from the list' }

	def __init__(self, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.drag_active = False
		self.viewport().setAcceptDrops(True)
		self.hover_row = -1
		# which button of the hovered row the mouse is on, it is drawn stronger
		self.hover_button = ''
		self.setMouseTracking(True)

	def button_at(self, pos) -> Tuple[int, str]:
		index = self.indexAt(pos)
		if not index.isValid():
			return -1, ''
		return index.row(), self.itemDelegate().button_at(self.visualRect(index), pos, index.row())

	def set_hover(self, row : int, button : str) -> None:
		if (row, button) != (self.hover_row, self.hover_button):
			self.hover_row, self.hover_button = row, button
			self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if button else Qt.CursorShape.ArrowCursor)
			self.viewport().update()

	def refresh_hover(self) -> None:
		"""after the rows changed under a still mouse"""
		pos = self.viewport().mapFromGlobal(QCursor.pos())
		self.set_hover(*self.button_at(pos)) if self.viewport().rect().contains(pos) else self.set_hover(-1, '')

	def mouseMoveEvent(self, event) -> None:
		self.set_hover(*self.button_at(event.position().toPoint()))
		super().mouseMoveEvent(event)

	def leaveEvent(self, event) -> None:
		self.set_hover(-1, '')
		super().leaveEvent(event)

	def mousePressEvent(self, event) -> None:
		row, button = self.button_at(event.position().toPoint())
		if button and event.button() == Qt.MouseButton.LeftButton:
			# a button press neither selects the row nor starts a drag
			event.accept()
			(self.retry_clicked if button == 'retry' else self.remove_clicked).emit(row)
			self.refresh_hover()
			return
		super().mousePressEvent(event)

	def mouseDoubleClickEvent(self, event) -> None:
		if self.button_at(event.position().toPoint())[1]:
			event.accept()
			return
		super().mouseDoubleClickEvent(event)

	def viewportEvent(self, event) -> bool:
		if event.type() == QEvent.Type.ToolTip:
			button = self.button_at(event.pos())[1]
			if button:
				QToolTip.showText(event.globalPos(), self.BUTTON_TIPS.get(button), self.viewport())
				return True
		return super().viewportEvent(event)

	def dragEnterEvent(self, event) -> None:
		if event.mimeData().hasUrls():
			event.acceptProposedAction()
			self.drag_active = True
			self.viewport().update()

	def dragMoveEvent(self, event) -> None:
		if event.mimeData().hasUrls():
			event.acceptProposedAction()

	def dragLeaveEvent(self, event) -> None:
		self.drag_active = False
		self.viewport().update()

	def dropEvent(self, event) -> None:
		self.drag_active = False
		self.viewport().update()
		paths = collect_drop_paths(event)
		if paths:
			event.acceptProposedAction()
			self.files_dropped.emit(paths)

	def paintEvent(self, event) -> None:
		super().paintEvent(event)
		if self.count() and not self.drag_active:
			return
		painter = QPainter(self.viewport())
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		rect = QRectF(self.viewport().rect()).adjusted(4, 4, -4, -4)
		if self.drag_active:
			color = QColor(themeColor())
			painter.setPen(color)
			color.setAlpha(30)
			painter.setBrush(color)
			painter.drawRoundedRect(rect, 8, 8)
		if not self.count():
			painter.setPen(QColor('#9a9a9a') if isDarkTheme() else QColor('#707070'))
			painter.setFont(getFont(13))
			painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, 'Choose a target folder above,\nor drop image and video files here')


class OutputDelegate(ListItemDelegate):
	"""a planned output: its status mark, thumbnails of the target (of the result once it is made) and the sources, then the names"""
	ROW_HEIGHT = 52
	THUMB_HEIGHT = 40
	# the running output's ring, a little wider than the marks, it reaches into the gap before the thumbnails
	RING_SIZE = 24
	THUMB_MAX_WIDTH = 72
	# the names get at least this much of a row before thumbnails are left out
	MIN_TEXT_WIDTH = 200
	# enough for every row of a long list, the oldest go first
	CACHE_LIMIT = 500

	def __init__(self, page : 'BatchPage') -> None:
		super().__init__(page.match_list)
		self.page = page
		self.pixmaps : 'OrderedDict[Tuple[str, float, int, int], QPixmap]' = OrderedDict()
		# a target's length and size by its path and change time, a video is opened once to read its length
		self.sizes : Dict[Tuple[str, float], str] = {}

	def sizeHint(self, option, index) -> QSize:
		return QSize(option.rect.width(), self.ROW_HEIGHT)

	@staticmethod
	def mark_rect(row_rect) -> QRectF:
		rect = QRectF(row_rect).adjusted(12, 0, -8, 0)
		return QRectF(rect.x() - 4, rect.y() + rect.height() / 2 - 14, 28, 28)

	@staticmethod
	def remove_rect(row_rect) -> QRectF:
		rect = QRectF(row_rect).adjusted(12, 0, -8, 0)
		return QRectF(rect.right() - 28, rect.y() + rect.height() / 2 - 14, 28, 28)

	def buttons(self, row : int) -> List[str]:
		"""the buttons the row shows while the mouse is over it"""
		if row >= len(self.page.items):
			return []
		status = self.page.item_status(self.page.items[row])
		buttons = [ 'retry' ] if status in RETRY_STATUSES else []
		if status != 'running':
			buttons.append('remove')
		return buttons

	def button_at(self, row_rect, pos, row : int) -> str:
		for button in self.buttons(row):
			rect = self.mark_rect(row_rect) if button == 'retry' else self.remove_rect(row_rect)
			if rect.contains(pos.x(), pos.y()):
				return button
		return ''

	@staticmethod
	def draw_button_back(painter : QPainter, rect : QRectF, color : QColor) -> None:
		back = QColor(color)
		back.setAlpha(40)
		painter.setPen(Qt.PenStyle.NoPen)
		painter.setBrush(back)
		painter.drawRoundedRect(rect, 6, 6)

	def pixmap(self, path : str, size : QSize) -> QPixmap:
		"""the thumbnail already scaled to fill the box, so painting a row only draws it"""
		# keyed by the change time too, so a result made again shows its new look
		try:
			key = (path, os.path.getmtime(path), size.width(), size.height())
		except OSError:
			return QPixmap()
		if key in self.pixmaps:
			self.pixmaps.move_to_end(key)
			return self.pixmaps.get(key)
		pixmap = thumbnail(path, 2 * self.THUMB_MAX_WIDTH) if media_kind(path) in [ 'image', 'video' ] else QPixmap()
		if not pixmap.isNull():
			pixmap = pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
		self.pixmaps[key] = pixmap
		while len(self.pixmaps) > self.CACHE_LIMIT:
			self.pixmaps.popitem(last = False)
		return pixmap

	def size_text(self, path : str) -> str:
		"""how long the target plays and how big it is, such as 0:42  ·  18.5 MB"""
		try:
			key = (path, os.path.getmtime(path))
			if key not in self.sizes:
				parts = [ format_size(os.path.getsize(path)) ]
				seconds = video_seconds(path) if media_kind(path) == 'video' else 0.0
				if seconds > 0:
					parts.insert(0, format_length(seconds))
				self.sizes[key] = '  ·  '.join(parts)
			return self.sizes.get(key)
		except OSError:
			return ''

	def draw_thumbnail(self, painter : QPainter, path : str, x : float, y : float, width : float) -> float:
		height = self.THUMB_HEIGHT
		box = QRectF(x, y, width, height)
		pixmap = self.pixmap(path, box.size().toSize())
		clip = QPainterPath()
		clip.addRoundedRect(box, 4, 4)
		painter.save()
		painter.setClipPath(clip)
		if pixmap.isNull():
			shade = 255 if isDarkTheme() else 0
			painter.fillRect(box, QColor(shade, shade, shade, 20))
			painter.setPen(QColor('#9a9a9a' if isDarkTheme() else '#707070'))
			painter.drawText(box, Qt.AlignmentFlag.AlignCenter, '♪' if media_kind(path) == 'audio' else '')
		else:
			# fills the box and crops what is left over
			painter.drawPixmap(int(x + (width - pixmap.width()) / 2), int(y + (height - pixmap.height()) / 2), pixmap)
		painter.restore()
		return width

	def draw_ring(self, painter : QPainter, x : float, middle : float, ratio : float) -> None:
		pen_width = 2.5
		size = self.RING_SIZE - pen_width
		box = QRectF(x - 2 + pen_width / 2, middle - size / 2, size, size)
		shade = 255 if isDarkTheme() else 0
		painter.setBrush(Qt.BrushStyle.NoBrush)
		painter.setPen(QPen(QColor(shade, shade, shade, 40), pen_width))
		painter.drawEllipse(box)
		if ratio > 0:
			pen = QPen(themeColor(), pen_width)
			pen.setCapStyle(Qt.PenCapStyle.RoundCap)
			painter.setPen(pen)
			# clockwise from the top, qt counts sixteenths of a degree
			painter.drawArc(box, 90 * 16, -int(min(ratio, 1.0) * 360 * 16))

	def paint(self, painter : QPainter, option, index) -> None:
		# the background, hover and selection, the row itself has no text
		super().paint(painter, option, index)
		row = index.row()
		if row >= len(self.page.items):
			return
		item = self.page.items[row]
		painter.save()
		painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
		rect = QRectF(option.rect).adjusted(12, 0, -8, 0)
		status = self.page.item_status(item)
		mark, _, light, dark = STATUS_LOOK.get(status, ('', '', None, None))
		words = self.page.status_words(item)
		buttons = self.buttons(row) if self.page.match_list.hover_row == row else []
		if 'retry' in buttons:
			# the mark turns into the retry button
			button_rect = self.mark_rect(option.rect)
			if self.page.match_list.hover_button == 'retry':
				self.draw_button_back(painter, button_rect, themeColor())
			FluentIcon.SYNC.render(painter, button_rect.adjusted(6, 6, -6, -6).toRect(), fill = themeColor().name())
		elif status == 'running':
			self.draw_ring(painter, rect.x(), rect.y() + rect.height() / 2, item.get('progress', 0.0))
			words = self.page.progress_words(item)
		elif status == 'skipped':
			FluentIcon.SYNC.render(painter, self.mark_rect(option.rect).adjusted(6, 6, -6, -6).toRect(), fill = dark if isDarkTheme() else light)
		elif mark:
			painter.setPen(QColor(dark if isDarkTheme() else light) if light else themeColor())
			painter.setFont(getFont(16, QFont.Weight.DemiBold))
			painter.drawText(QRectF(rect.x(), rect.y(), 20, rect.height()), Qt.AlignmentFlag.AlignCenter, mark)
		x = rect.x() + 28
		y = rect.y() + (rect.height() - self.THUMB_HEIGHT) / 2
		made = item.get('status') == 'done' and os.path.isfile(item.get('output'))
		# a narrow list leaves out the source thumbnails, then the target's, so the names keep room to show
		sources = item.get('sources')
		room = rect.right() - x - 8
		show_sources = room - (self.THUMB_MAX_WIDTH + 6) - len(sources) * (self.THUMB_HEIGHT + 6) >= self.MIN_TEXT_WIDTH
		show_target = room - (self.THUMB_MAX_WIDTH + 6) >= self.MIN_TEXT_WIDTH
		if 'remove' in buttons:
			# a red cross at the right end, the size and names keep clear of it
			red = QColor('#ff99a4' if isDarkTheme() else '#c42b1c')
			button_rect = self.remove_rect(option.rect)
			if self.page.match_list.hover_button == 'remove':
				self.draw_button_back(painter, button_rect, red)
			painter.setPen(QPen(red, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
			cross = button_rect.adjusted(9, 9, -9, -9)
			painter.drawLine(cross.topLeft(), cross.bottomRight())
			painter.drawLine(cross.topRight(), cross.bottomLeft())
			rect.setRight(button_rect.left() - 6)
		# the target gets a wide box and each source a square one, so the names line up from row to row
		if show_target:
			x += self.draw_thumbnail(painter, item.get('output') if made else item.get('target'), x, y, self.THUMB_MAX_WIDTH) + 6
		for path in sources if show_sources else []:
			x += self.draw_thumbnail(painter, path, x, y, self.THUMB_HEIGHT) + 6
		x += 8
		# the output name on its own line, so a narrow list still shows most of it, what it is made from below
		name, detail = self.page.item_lines(item)
		width = int(rect.right() - x)
		middle = rect.y() + rect.height() / 2
		# the target's length and size at the end of the name line, quiet, a hint of how long the output takes
		size = self.size_text(item.get('target'))
		name_width = width
		font = getFont(12)
		size_width = QFontMetrics(font).horizontalAdvance(size)
		if size and width - size_width - 12 >= self.MIN_TEXT_WIDTH / 2:
			painter.setFont(font)
			painter.setPen(QColor('#9a9a9a' if isDarkTheme() else '#707070'))
			painter.drawText(QRectF(rect.right() - size_width, middle - 21, size_width, 21), Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight, size)
			name_width -= size_width + 12
		font = getFont(13)
		painter.setFont(font)
		painter.setPen(QColor(255, 255, 255) if isDarkTheme() else QColor(0, 0, 0))
		painter.drawText(QRectF(x, middle - 21, name_width, 21), Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft, QFontMetrics(font).elidedText(name, Qt.TextElideMode.ElideMiddle, name_width))
		font = getFont(12)
		painter.setFont(font)
		metrics = QFontMetrics(font)
		if status == 'running' and words:
			# how far the output is in the theme color, what it is made from after it as usual
			painter.setPen(themeColor())
			painter.drawText(QRectF(x, middle + 1, width, 20), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, metrics.elidedText(words, Qt.TextElideMode.ElideRight, width))
			x += metrics.horizontalAdvance(words)
			width = int(rect.right() - x)
			words = ''
		painter.setPen(QColor(dark if isDarkTheme() else light) if words and light else QColor('#9a9a9a' if isDarkTheme() else '#707070'))
		if width > 0:
			painter.drawText(QRectF(x, middle + 1, width, 20), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, metrics.elidedText(words + detail, Qt.TextElideMode.ElideRight, width))
		painter.restore()


class BatchPage(SourceSelection, QWidget):
	SOURCE_KEYS = ('batch_source_paths', 'batch_unused_source_paths', 'batch_source_names')
	# Batch makes whole files, there is no timeline frame to name them after
	TAG_DESCRIPTIONS = { 'frame': 'Always 0 here, Batch makes whole videos rather than one frame' }

	def __init__(self, session : Session, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.setObjectName('batch')
		self.session = session
		self.running = False
		# one entry per output: sources, target, output path, status
		self.items : List[Dict[str, Any]] = []
		self.pairs_count = 0
		# nsfw check results by target, from NSFW Check or from confirming refused images after a batch
		self.check_results : Dict[str, Dict[str, Any]] = {}
		self.current = -1
		# the running output's line in the status, the percent follows it
		self.run_task = ''
		self.started_count = 0
		self.confirming = False
		# Run Batch checks the targets the nsfw check has not seen yet before the first output
		self.prechecking = False
		self.stopped = False
		# the time the batch started, the clock counts up from it while it runs
		self.run_started = 0.0
		self.run_clock = QTimer(self)
		self.run_clock.setInterval(1000)
		self.run_clock.timeout.connect(self.show_run_time)
		# Skip ends the output that is running and the batch goes on with the next one
		self.skipping = False
		# the outputs Retry runs on their own, empty when the whole batch runs
		self.retry_only : set = set()
		# a retry that waits for the batch preview to stop
		self.retry_waiting = False
		# the outputs a run would make right now, items is the same list with the runs that already ended kept in it
		self.plan_items : List[Dict[str, Any]] = []
		# the preview of the selected output: the run it shows, and whether one is running or waits for facefusion to be free
		self.preview_run : Optional[Dict[str, Any]] = None
		self.preview_running = False
		self.preview_waiting = False
		self.preview_frame = None
		self.removed_count = 0
		self.load_sources()
		# the batch has its own processors and options, the system settings from Setup are shared
		self.store = OptionStore(session, 'batch_options', 'batch_processor_order')
		stored = session.config.get('batch') or {}
		# runs the user took out of the list, they stay out until restored, also after a restart
		self.removed : set = { (tuple(sources), target) for sources, target in stored.get('removed') or [] }
		# targets dropped on the list or added with Add Files, planned with the files of the target folder
		self.target_files : List[str] = [ path for path in stored.get('target_files') or [] if isinstance(path, str) ]
		# the frame of each video target the preview shows, picked on the timeline under the preview, also after a restart
		self.preview_frames : Dict[str, int] = { target: index for target, index in (stored.get('preview_frames') or {}).items() if isinstance(index, int) and index > 0 }
		self.timeline_target = ''
		self.timeline_reader : Optional[VideoReader] = None
		self.preview_frame_key : Optional[Tuple[str, int]] = None
		# typing and setting changes replan the outputs once they settle
		self.refresh_timer = QTimer(self)
		self.refresh_timer.setSingleShot(True)
		self.refresh_timer.setInterval(200)
		self.refresh_timer.timeout.connect(self.refresh_matches)
		# names with {date} or {time} follow the clock, and files appearing in the output folder move {n} and _2 on
		self.clock = QTimer(self)
		self.clock.setInterval(1000)
		self.clock.timeout.connect(self.on_clock)
		self.watch_timer = QTimer(self)
		self.watch_timer.setSingleShot(True)
		self.watch_timer.setInterval(300)
		self.watch_timer.timeout.connect(lambda: self.refresh_matches(True))
		self.watcher = QFileSystemWatcher(self)
		self.watcher.directoryChanged.connect(self.watch_timer.start)
		self.preview_timer = QTimer(self)
		self.preview_timer.setSingleShot(True)
		self.preview_timer.setInterval(700)
		self.preview_timer.timeout.connect(self.run_preview)
		# facefusion reports no progress for a single frame, so the preview bar follows how long Batch previews took before
		self.preview_clock = QTimer(self)
		self.preview_clock.setInterval(100)
		self.preview_clock.timeout.connect(self.tick_preview_progress)
		self.preview_started = 0.0
		self.preview_floor = 0.0
		self.preview_estimate = 'batch_preview_seconds'
		# the faces and the mask facefusion sees in the previewed frame, drawn over the preview
		self.face_probe = FaceProbe(session, self)
		self.face_probe.scanned.connect(self.on_faces_scanned)
		self.face_probe.loading.connect(self.on_faces_loading)
		self.faces_timer = QTimer(self)
		self.faces_timer.setSingleShot(True)
		self.faces_timer.setInterval(250)
		self.faces_timer.timeout.connect(self.scan_faces)

		layout = QVBoxLayout(self)
		layout.setContentsMargins(24, 16, 24, 16)
		layout.setSpacing(12)
		title = QHBoxLayout()
		title.addWidget(SubtitleLabel('Batch', self))
		hint = CaptionLabel('Runs every selected face against every target, one output at a time, with the processors and options in the Settings panel. Selected audio goes into every run.', self)
		hint.setTextColor('#707070', '#9a9a9a')
		title.addWidget(elastic(hint), 1)
		self.settings_button = TogglePushButton(FluentIcon.SETTING, 'Settings', self)
		self.settings_button.setToolTip('Show or hide the processors and options of the batch on the right')
		self.settings_button.installEventFilter(ToolTipFilter(self.settings_button))
		title.addWidget(self.settings_button)
		layout.addLayout(title)

		# the batch on the left, its settings on the right, like the Studio page
		self.splitter = Splitter(Qt.Orientation.Horizontal, self)
		layout.addWidget(self.splitter, 1)
		batch_tab = QWidget(self.splitter)
		batch_layout = QVBoxLayout(batch_tab)
		batch_layout.setContentsMargins(0, 0, 0, 0)
		batch_layout.setSpacing(12)
		self.splitter.addWidget(batch_tab)
		self.splitter.addWidget(self.create_settings_column())
		self.splitter.setStretchFactor(0, 11)
		self.splitter.setStretchFactor(1, 9)
		self.splitter.setSizes(session.config.get('batch_split') or [ 1100, 900 ])
		self.splitter.splitterMoved.connect(lambda *_: self.session.config.set('batch_split', self.splitter.sizes()))
		# a double click on the grip splits the page in half again
		self.splitter.default_sizes = lambda total: [ total // 2, total - total // 2 ]
		self.splitter.reset.connect(lambda: self.session.config.set('batch_split', self.splitter.sizes()))
		self.settings_button.setChecked(session.config.get('batch_settings_open') is not False)
		self.settings_column.setVisible(self.settings_button.isChecked())
		self.settings_button.toggled.connect(self.toggle_settings)

		self.source_card = SourceCard(self, 'Source faces', 'Drop face images (or audio for lip sync) here or click to browse\nLeave empty for processors that need no source')
		copy_sources = TransparentPushButton(FluentIcon.COPY, 'Copy Faces from Studio', self.source_card)
		copy_sources.setToolTip('Add the sources from the Studio page, with their names and which ones are used')
		copy_sources.installEventFilter(ToolTipFilter(copy_sources))
		copy_sources.clicked.connect(self.copy_studio_sources)
		self.source_card.add_tool(copy_sources)
		batch_layout.addWidget(self.source_card)

		card, form = section_card('Targets and output', '', batch_tab)
		self.target_dir = LineEdit(card)
		self.target_dir.setText(stored.get('target_dir', ''))
		self.target_dir.setPlaceholderText('Folder with the target images or videos')
		self.target_dir.setClearButtonEnabled(True)
		self.target_filter = ComboBox(card)
		patterns = [ pattern for pattern, _ in TARGET_FILTERS ]
		for pattern, name in TARGET_FILTERS:
			self.target_filter.addItem('%s  (%s)' % (name, pattern), userData = pattern)
		# stored under a new key, so everyone starts on All files once
		self.target_filter.setCurrentIndex(patterns.index(stored.get('target_pattern')) if stored.get('target_pattern') in patterns else 0)
		self.target_filter.setFixedWidth(210)
		self.target_filter.currentIndexChanged.connect(lambda _: self.refresh_matches())
		target_browse = PushButton(FluentIcon.FOLDER, 'Browse', card)
		target_browse.clicked.connect(lambda: self.browse(self.target_dir))
		form.addLayout(form_row('Target folder', [ self.target_dir, self.target_filter, target_browse ], card))
		self.output_dir_edit = LineEdit(card)
		self.output_dir_edit.setText(stored.get('output_dir', os.path.join(session.config.get('output_dir') or '', 'batch')))
		output_browse = PushButton(FluentIcon.FOLDER, 'Browse', card)
		output_browse.clicked.connect(lambda: self.browse(self.output_dir_edit))
		output_open = PushButton(FluentIcon.SHARE, 'Open Folder', card)
		output_open.setToolTip('Open the output folder in File Explorer')
		output_open.installEventFilter(ToolTipFilter(output_open))
		output_open.clicked.connect(self.open_output_folder)
		form.addLayout(form_row('Output folder', [ self.output_dir_edit, output_browse, output_open ], card))
		self.output_name_edit = LineEdit(card)
		self.output_name_edit.setPlaceholderText('File name (automatic)')
		self.output_name_edit.setClearButtonEnabled(True)
		self.output_name_edit.setText(upgrade_template(stored.get('template', DEFAULT_TEMPLATE)))
		self.tags_button = add_edit_button(self.output_name_edit, FluentIcon.TAG, 'File name tags and examples')
		self.tags_button.clicked.connect(self.show_name_tags)
		self.subfolders_button = TogglePushButton('Sub-Folders', card)
		self.subfolders_button.setToolTip('Save into <output folder>\\<source name>\\Images, Videos or GIF, created as needed')
		self.subfolders_button.installEventFilter(ToolTipFilter(self.subfolders_button))
		self.subfolders_button.setChecked(bool(stored.get('subfolders')))
		self.subfolders_button.toggled.connect(lambda _: self.refresh_matches())
		form.addLayout(form_row('File name', [ self.output_name_edit, self.subfolders_button ], card))
		# what the first file will be called, so tags can be checked before a run
		self.name_preview_label = elastic(CaptionLabel('', card))
		self.name_preview_label.setTextColor('#707070', '#9a9a9a')
		self.name_preview_label.installEventFilter(ToolTipFilter(self.name_preview_label))
		form.addLayout(form_row('', [ self.name_preview_label ], card))
		batch_layout.addWidget(card)

		for edit in [ self.target_dir, self.output_dir_edit, self.output_name_edit ]:
			edit.textChanged.connect(self.refresh_timer.start)

		matches_card = SimpleCardWidget(batch_tab)
		matches_layout = QVBoxLayout(matches_card)
		matches_layout.setContentsMargins(20, 14, 20, 16)
		header = QHBoxLayout()
		header.addWidget(StrongBodyLabel('Planned outputs', matches_card))
		# shortens rather than keep the list from narrowing for a wider preview
		self.summary = elastic(CaptionLabel('', matches_card))
		self.summary.setTextColor('#707070', '#9a9a9a')
		header.addWidget(self.summary)
		header.addStretch(1)
		add_files = TransparentPushButton(FluentIcon.ADD, 'Add Files', matches_card)
		add_files.setToolTip('Add single image or video files as targets, next to the target folder. You can also drop files on the list.')
		add_files.installEventFilter(ToolTipFilter(add_files))
		add_files.clicked.connect(self.browse_target_files)
		header.addWidget(add_files)
		self.restore_button = TransparentPushButton(FluentIcon.RETURN, 'Restore removed', matches_card)
		self.restore_button.setToolTip('Put the outputs you removed back into the list')
		self.restore_button.installEventFilter(ToolTipFilter(self.restore_button))
		self.restore_button.clicked.connect(self.restore_removed)
		header.addWidget(self.restore_button)
		self.remove_button = TransparentPushButton(FluentIcon.DELETE, 'Remove', matches_card)
		self.remove_button.setToolTip('Take the selected outputs out of this batch (Delete key). Files already made are kept.')
		self.remove_button.installEventFilter(ToolTipFilter(self.remove_button))
		self.remove_button.clicked.connect(self.remove_selected)
		header.addWidget(self.remove_button)
		matches_layout.addLayout(header)
		self.match_list = OutputList(matches_card)
		self.match_list.files_dropped.connect(self.add_target_files)
		self.match_list.setUniformItemSizes(True)
		# the rows shorten their text to fit, a sideways scroll bar would only hide the end of the names
		self.match_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
		self.match_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
		self.match_list.setItemDelegate(OutputDelegate(self))
		self.match_list.itemDoubleClicked.connect(self.show_item_result)
		self.match_list.retry_clicked.connect(self.retry_item)
		self.match_list.remove_clicked.connect(self.remove_item)
		self.match_list.itemSelectionChanged.connect(self.on_selection_changed)
		delete_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.match_list)
		delete_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
		delete_shortcut.activated.connect(self.remove_selected)
		matches_layout.addWidget(self.match_list, 1)
		# dragging the grip between the list and the preview widens the preview
		self.outputs_splitter = Splitter(Qt.Orientation.Horizontal, batch_tab)
		self.outputs_splitter.setHandleWidth(12)
		self.outputs_splitter.addWidget(matches_card)
		self.outputs_splitter.addWidget(self.create_preview_card(self.outputs_splitter))
		self.outputs_splitter.setStretchFactor(0, 1)
		self.outputs_splitter.setStretchFactor(1, 0)
		self.outputs_splitter.setSizes(session.config.get('batch_preview_split') or [ 700, PREVIEW_WIDTH ])
		self.outputs_splitter.splitterMoved.connect(lambda *_: self.session.config.set('batch_preview_split', self.outputs_splitter.sizes()))
		# and here gives the preview its first width back
		self.outputs_splitter.default_sizes = lambda total: [ total - PREVIEW_WIDTH, PREVIEW_WIDTH ]
		self.outputs_splitter.reset.connect(lambda: self.session.config.set('batch_preview_split', self.outputs_splitter.sizes()))
		batch_layout.addWidget(self.outputs_splitter, 1)

		run_card = SimpleCardWidget(self)
		run_layout = QHBoxLayout(run_card)
		run_layout.setContentsMargins(16, 12, 16, 12)
		self.start_button = PrimaryPushButton(FluentIcon.PLAY, 'Run Batch', run_card)
		self.start_button.clicked.connect(self.start)
		self.stop_button = PushButton(FluentIcon.CLOSE, 'Stop', run_card)
		self.stop_button.setEnabled(False)
		self.stop_button.clicked.connect(self.stop)
		self.skip_button = PushButton(FluentIcon.PAGE_RIGHT, 'Skip', run_card)
		self.skip_button.setToolTip('Stop the output that is being made and go on with the next one')
		self.skip_button.installEventFilter(ToolTipFilter(self.skip_button))
		self.skip_button.setEnabled(False)
		self.skip_button.clicked.connect(self.skip)
		self.check_button = PushButton(FluentIcon.CERTIFICATE, 'NSFW Check', run_card)
		self.check_button.setToolTip("Run FaceFusion's NSFW filter on every target without processing them. Run Batch skips the targets it flags.")
		self.check_button.installEventFilter(ToolTipFilter(self.check_button))
		self.check_button.clicked.connect(self.check_targets)
		run_layout.addWidget(self.start_button)
		run_layout.addWidget(self.stop_button)
		run_layout.addWidget(self.skip_button)
		run_layout.addWidget(self.check_button)
		run_layout.addSpacing(10)
		status = QVBoxLayout()
		self.status_label = BodyLabel('Ready', run_card)
		self.progress = ProgressBar(run_card)
		self.progress.setRange(0, 1000)
		status.addWidget(self.status_label)
		status.addWidget(self.progress)
		run_layout.addLayout(status, 1)
		run_layout.addSpacing(10)
		self.time_label = BodyLabel('', run_card)
		self.time_label.setToolTip('How long the batch has been running, and how long the last one took')
		self.time_label.installEventFilter(ToolTipFilter(self.time_label))
		run_layout.addWidget(self.time_label)
		run_layout.addSpacing(10)
		self.open_output_button = PushButton(FluentIcon.SHARE, 'Open Output Folder', run_card)
		self.open_output_button.setToolTip('Open the batch output folder in File Explorer')
		self.open_output_button.installEventFilter(ToolTipFilter(self.open_output_button))
		self.open_output_button.clicked.connect(self.open_output_folder)
		run_layout.addWidget(self.open_output_button)
		layout.addWidget(run_card)

		session.busy_changed.connect(self.on_busy_changed)
		session.runner.progress.connect(self.on_progress)
		session.runner.log.connect(self.on_log)
		session.runner.finished.connect(self.on_finished)
		session.content_checked.connect(self.on_content_checked)
		self.store.value_changed.connect(self.on_value_changed)
		self.store.schema_changed.connect(self.refresh_timer.start)
		self.store.schema_changed.connect(self.schedule_preview)
		self.source_card.refresh(self.source_paths)
		self.refresh_matches()
		# only once the page is built, the splash lets timers fire while it builds
		self.clock.start()

	def create_settings_column(self) -> QWidget:
		copy_button = PushButton(FluentIcon.COPY, 'Copy Settings from Studio', self)
		copy_button.setToolTip('Make the processors and options here the same as on the Studio page. Later changes on either page stay on that page.')
		copy_button.installEventFilter(ToolTipFilter(copy_button))
		copy_button.clicked.connect(self.copy_studio_settings)
		self.settings_column = SettingsColumn(self.store, self.splitter, (copy_button,), expansion_key = 'batch_groups_expanded')
		return self.settings_column

	def toggle_settings(self, shown : bool) -> None:
		self.settings_column.setVisible(shown)
		self.session.config.set('batch_settings_open', shown)

	def create_preview_card(self, parent : QWidget) -> QWidget:
		card = SimpleCardWidget(parent)
		card.setMinimumWidth(PREVIEW_MIN_WIDTH)
		column = QVBoxLayout(card)
		column.setContentsMargins(16, 14, 16, 16)
		header = QHBoxLayout()
		header.addWidget(StrongBodyLabel('Preview', card))
		header.addStretch(1)
		self.faces_button = ToggleToolButton(FluentIcon.PEOPLE, card)
		self.faces_button.setToolTip('Faces: show the faces FaceFusion detects in this frame with the current face settings.\nOutlined faces will be processed, dashed ones are skipped by the face selector.')
		self.mask_button = ToggleToolButton(FluentIcon.PALETTE, card)
		self.mask_button.setToolTip('Mask: shade the face mask FaceFusion would blend the new face through, with the current Face masker settings.')
		for button, key in [ (self.faces_button, 'batch_show_faces'), (self.mask_button, 'batch_show_mask') ]:
			button.installEventFilter(ToolTipFilter(button))
			button.setChecked(bool(self.session.config.get(key)))
			button.toggled.connect(lambda checked, key = key: self.on_overlay_toggled(key, checked))
			header.addWidget(button)
		header.addSpacing(4)
		self.live_button = TogglePushButton(FluentIcon.SYNC, 'Auto', card)
		self.live_button.setToolTip('Make the preview again by itself when a setting, a source or the selected output changes')
		self.live_button.installEventFilter(ToolTipFilter(self.live_button))
		self.live_button.setChecked(self.session.config.get('batch_live_preview') is not False)
		self.live_button.toggled.connect(self.on_live_toggled)
		header.addWidget(self.live_button)
		refresh = tool_button(FluentIcon.UPDATE, 'Make the preview now', card)
		refresh.clicked.connect(lambda: self.schedule_preview(True))
		header.addWidget(refresh)
		column.addLayout(header)
		self.preview_view = PreviewView('Select an output to preview it', card)
		self.preview_view.setAcceptDrops(False)
		self.preview_view.setMinimumHeight(180)
		column.addWidget(self.preview_view, 1)
		# a video target is previewed on the frame picked here, the batch itself still makes the whole video
		self.timeline = QWidget(card)
		timeline_row = QHBoxLayout(self.timeline)
		timeline_row.setContentsMargins(0, 0, 0, 0)
		timeline_row.setSpacing(8)
		self.preview_slider = Slider(Qt.Orientation.Horizontal, self.timeline)
		self.preview_slider.setMinimumWidth(80)
		self.preview_slider.setToolTip('The frame of this video the preview shows')
		self.preview_slider.installEventFilter(ToolTipFilter(self.preview_slider))
		self.preview_slider.valueChanged.connect(self.on_preview_frame_moved)
		guard_wheel(self.preview_slider)
		self.preview_time_label = CaptionLabel('', self.timeline)
		self.preview_time_label.setTextColor('#707070', '#9a9a9a')
		timeline_row.addWidget(self.preview_slider, 1)
		timeline_row.addWidget(self.preview_time_label)
		self.timeline.hide()
		column.addWidget(self.timeline)
		self.scrub_timer = QTimer(self)
		self.scrub_timer.setSingleShot(True)
		self.scrub_timer.setInterval(40)
		self.scrub_timer.timeout.connect(self.show_scrubbed_frame)
		# the bar and its Stop button only show while a preview runs
		self.preview_row = QWidget(card)
		bar_row = QHBoxLayout(self.preview_row)
		bar_row.setContentsMargins(0, 0, 0, 0)
		bar_row.setSpacing(8)
		self.preview_bar = ProgressBar(self.preview_row)
		self.preview_bar.setRange(0, 1000)
		bar_row.addWidget(self.preview_bar, 1, Qt.AlignmentFlag.AlignVCenter)
		self.preview_stop_button = tool_button(FluentIcon.CLOSE, 'Stop making this preview', self.preview_row)
		self.preview_stop_button.clicked.connect(self.stop_preview)
		bar_row.addWidget(self.preview_stop_button)
		self.preview_row.hide()
		column.addWidget(self.preview_row)
		self.preview_label = elastic(CaptionLabel('', card))
		self.preview_label.setTextColor('#707070', '#9a9a9a')
		self.preview_label.setWordWrap(True)
		column.addWidget(self.preview_label)
		# what face detection found, and which setting to change when it found nothing usable
		self.faces_label = CaptionLabel('', card)
		self.faces_label.setWordWrap(True)
		self.faces_label.setVisible(self.overlays_on())
		column.addWidget(self.faces_label)
		return card

	def copy_studio_settings(self) -> None:
		order = self.session.config.get(Session.ORDER_KEY) or self.session.processors()
		schema = self.session.schema
		# the system settings are shared already, only the batch's own options would change
		changed = [ dest for dest, value in self.session.values.items() if (not schema or schema.options.get(dest, {}).get('group') not in SYSTEM_GROUPS) and self.store.values.get(dest) != value ]
		if not changed and list(order) == list(self.session.config.get(self.store.ORDER_KEY) or order):
			self.session.notify.emit('info', 'Already the same', 'Batch already uses the same processors and options as Studio.')
			return
		difference = ('Batch has %d option%s set differently from Studio.' % (len(changed), '' if len(changed) == 1 else 's')) if changed else 'Batch runs the processors in another order than Studio.'
		box = MessageBox('Replace the Batch settings?', difference + ' Copying makes Batch use the processors and options of the Studio page, and the Batch settings you have now are lost.', self.window())
		box.yesButton.setText('Copy Settings')
		box.cancelButton.setText('Cancel')
		if not box.exec():
			return
		self.store.copy_from(self.session.values, order)
		self.session.notify.emit('success', 'Settings copied from Studio', 'Batch now uses the same processors and options as Studio. Later changes on either page stay on that page.')

	def copy_studio_sources(self) -> None:
		config = self.session.config
		paths = [ path for path in config.get('source_paths') or [] if os.path.exists(path) ]
		added = [ path for path in paths if path not in self.source_paths ]
		if not added:
			self.session.notify.emit('info', 'Nothing to copy', 'The Studio page has no sources that are not here already.' if paths else 'The Studio page has no sources yet.')
			return
		unused = config.get('unused_source_paths') or []
		self.source_names.update({ path: name for path, name in (config.get('source_names') or {}).items() if path in added })
		self.unused_sources = self.unused_sources + [ path for path in added if path in unused ]
		self.set_sources(self.source_paths + added)
		self.session.notify.emit('success', 'Copied from Studio', '%d source%s added.' % (len(added), '' if len(added) == 1 else 's'))

	def run_values(self) -> Dict[str, Any]:
		"""the options of a batch run: its own, with the system settings from Setup"""
		schema = self.session.schema
		values = dict(self.session.values)
		values.update({ dest: value for dest, value in self.store.values.items() if not schema or schema.options.get(dest, {}).get('group') not in SYSTEM_GROUPS })
		return values

	def on_sources_changed(self, change : str) -> None:
		self.refresh_matches()
		self.schedule_preview()

	def on_value_changed(self, dest : str, _ : object) -> None:
		self.refresh_timer.start()
		schema = self.store.schema
		group = schema.options.get(dest, {}).get('group') if schema else None
		if not schema or group not in LIVE_IGNORED_GROUPS:
			self.schedule_preview()
		if not schema or group in FACE_GROUPS + MASK_GROUPS or dest == 'processors':
			self.schedule_face_scan()

	def open_output_folder(self) -> None:
		StudioPage.open_folder(self.output_dir())

	# the outputs in the list

	def on_selection_changed(self) -> None:
		self.sync_timeline()
		item = self.preview_item()
		if item and (not self.preview_run or self.run_key(item) != self.run_key(self.preview_run)):
			self.schedule_preview()

	def remove_selected(self) -> None:
		if self.running:
			return
		rows = sorted({ self.match_list.row(list_item) for list_item in self.match_list.selectedItems() })
		if not rows:
			self.session.notify.emit('info', 'Nothing selected', 'Click an output in the list first, Ctrl+click or Shift+click for several.')
			return
		self.removed.update(self.run_key(self.items[row]) for row in rows if row < len(self.items))
		self.forget_removed_files()
		self.match_list.clearSelection()
		self.refresh_matches(True)
		self.match_list.setCurrentRow(min(rows[0], self.match_list.count() - 1), QItemSelectionModel.SelectionFlag.ClearAndSelect)

	def remove_item(self, row : int) -> None:
		"""the remove button of a row, while the batch runs it works for every output but the running one"""
		if row >= len(self.items) or self.items[row].get('status') == 'running':
			return
		key = self.run_key(self.items[row])
		self.removed.add(key)
		if not self.running:
			self.forget_removed_files()
			self.refresh_matches(True)
			return
		# the batch goes on with the list it has, the plan after it leaves the output out
		self.items.pop(row)
		self.retry_only.discard(key)
		if row < self.current:
			self.current -= 1
		self.refresh_list()

	def retry_item(self, row : int) -> None:
		"""the retry button of a skipped or failed output, it joins a running batch or runs on its own"""
		if row >= len(self.items) or self.items[row].get('status') not in RETRY_STATUSES:
			return
		item = self.items[row]
		folder = os.path.dirname(item.get('output'))
		busy = not self.running and not self.retry_waiting and self.session.runner.is_running()
		if busy and not self.preview_running:
			self.session.notify.emit('warning', 'Busy', 'FaceFusion is already running: %s. Retry once it has finished.' % self.session.task)
			return
		if not self.running and not folder_is_writable(folder):
			self.session.notify.emit('error', 'Cannot save to this folder', 'Windows does not allow saving to %s. Choose another output folder, such as Videos or Documents.' % folder)
			return
		item.pop('reason', None)
		item.pop('confirm', None)
		item['status'] = ''
		if self.running or self.retry_waiting:
			if self.retry_only:
				self.retry_only.add(self.run_key(item))
			self.refresh_item(row)
			return
		# only this output runs, the other rows keep what they show
		self.retry_only = { self.run_key(item) }
		if busy:
			# the preview of the selected output gives way, the retry starts once it has ended
			self.retry_waiting = True
			self.refresh_item(row)
			self.stop_preview()
			return
		self.begin_run()

	def forget_removed_files(self) -> None:
		"""an added file with all its outputs removed leaves the batch, rather than wait in the removed list"""
		folder_targets = { os.path.normcase(os.path.normpath(path)) for path in self.folder_targets() }
		runs = self.runs()
		for target in list(self.target_files):
			if os.path.normcase(target) in folder_targets:
				continue
			if all(self.run_key(run) in self.removed for run in runs if run.get('target') == target):
				self.target_files.remove(target)
				self.removed = { key for key in self.removed if key[1] != target }

	def add_target_files(self, paths : List[str]) -> None:
		if self.running:
			self.session.notify.emit('info', 'Batch is running', 'Add files once the batch has finished or is stopped.')
			return
		folders = [ path for path in paths if os.path.isdir(path) ]
		files = [ path for path in paths if os.path.isfile(path) and media_kind(path) in [ 'image', 'video' ] ]
		rejected = [ path for path in paths if path not in folders and path not in files ]
		if rejected:
			self.session.notify.emit('warning', 'Skipped files', 'Targets must be images or videos: ' + ', '.join(os.path.basename(path) for path in rejected[:3]))
		# a dropped folder becomes the target folder
		if folders:
			self.target_dir.setText(folders[0])
		known = { os.path.normcase(path) for path in self.target_files }
		for path in files:
			if os.path.normcase(path) not in known:
				self.target_files.append(path)
				known.add(os.path.normcase(path))
		# a file added again comes back with all its outputs
		added = { os.path.normcase(path) for path in files }
		self.removed = { key for key in self.removed if os.path.normcase(key[1] or '') not in added }
		if files:
			self.refresh_matches(True)

	def browse_target_files(self) -> None:
		paths, _ = QFileDialog.getOpenFileNames(self, 'Choose target images or videos', self.last_dir(), TARGET_FILES_FILTER)
		self.add_target_files([ os.path.normpath(path) for path in paths ])

	def restore_removed(self) -> None:
		self.removed.clear()
		self.refresh_matches(True)

	# preview of the selected output, made on a smaller copy of its target with the settings of the batch

	def on_live_toggled(self, checked : bool) -> None:
		self.session.config.set('batch_live_preview', checked)
		if checked:
			self.schedule_preview()

	def schedule_preview(self, now : bool = False) -> None:
		if now:
			self.preview_timer.stop()
			self.run_preview()
		elif self.live_button.isChecked():
			self.preview_timer.start()

	def preview_item(self) -> Optional[Dict[str, Any]]:
		row = self.match_list.currentRow() if self.match_list.selectedItems() else 0
		return self.items[row] if 0 <= row < len(self.items) else None

	def showEvent(self, event) -> None:
		super().showEvent(event)
		# a preview asked for while the page was hidden is made once it shows
		if self.preview_waiting or (self.preview_run is None and self.live_button.isChecked()):
			self.schedule_preview()
		self.schedule_face_scan()

	def hideEvent(self, event) -> None:
		super().hideEvent(event)
		# the probe holds its models in memory, let them go while nobody looks
		self.faces_timer.stop()
		self.face_probe.stop()

	def sync_timeline(self) -> None:
		"""shows the timeline for a video target, on the frame last picked for it"""
		item = self.preview_item()
		target = item.get('target') if item else ''
		if target == self.timeline_target:
			return
		self.timeline_target = target
		if self.timeline_reader:
			self.timeline_reader.release()
			self.timeline_reader = None
		if target and media_kind(target) == 'video':
			self.timeline_reader = VideoReader(target)
		count = self.timeline_reader.frame_count if self.timeline_reader else 0
		self.timeline.setVisible(count > 1)
		self.preview_slider.blockSignals(True)
		self.preview_slider.setRange(0, max(0, count - 1))
		self.preview_slider.setValue(min(self.preview_frames.get(target, 0), max(0, count - 1)))
		self.preview_slider.blockSignals(False)
		self.refresh_time_label()

	def refresh_time_label(self) -> None:
		index = self.preview_slider.value()
		fps = self.timeline_reader.fps if self.timeline_reader else 0.0
		self.preview_time_label.setText('%s · #%d' % (format_timecode(index, fps), index))

	def preview_frame_index(self, target : str) -> int:
		return self.preview_frames.get(target, 0) if media_kind(target) == 'video' else 0

	def on_preview_frame_moved(self, index : int) -> None:
		if self.timeline_target:
			# set again at the end, so the newest picks are the ones kept
			self.preview_frames.pop(self.timeline_target, None)
			self.preview_frames[self.timeline_target] = index
		self.refresh_time_label()
		self.scrub_timer.start()

	def show_scrubbed_frame(self) -> None:
		"""the picked frame shows at once, its preview follows when Auto is on"""
		item = self.preview_item()
		if not item or item.get('target') != self.timeline_target:
			return
		frame = self.scaled_frame(item)
		if frame is None:
			return
		self.preview_view.set_pixmap(to_pixmap(frame))
		self.preview_view.set_faces([])
		self.preview_frame = frame
		self.preview_frame_key = (item.get('target'), self.preview_frame_index(item.get('target')))
		self.save()
		self.schedule_preview()
		self.schedule_face_scan()

	def scaled_frame(self, item : Dict[str, Any]) -> Optional[Any]:
		"""the picked frame of the item's target (the first one until a frame is picked), scaled down to the preview size"""
		target = item.get('target')
		index = self.preview_frame_index(target)
		frame = self.timeline_reader.read(index) if self.timeline_reader and target == self.timeline_target else read_frame(target, index)
		if frame is None:
			return None
		height, width = frame.shape[:2]
		scale = PREVIEW_SIZE / max(height, width)
		if scale < 1:
			frame = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))), interpolation = cv2.INTER_AREA)
		return frame

	def run_frame(self, item : Dict[str, Any]) -> Optional[Any]:
		"""the frame the preview runs on, as facefusion reads it in a whole run and at studio's preview size"""
		target = item.get('target')
		index = self.preview_frame_index(target)
		shown = self.timeline_reader.read(index) if self.timeline_reader and target == self.timeline_target else read_frame(target, index)
		frame = self.session.run_frame(target, index, shown)
		if frame is None:
			return None
		height, width = frame.shape[:2]
		scale = PREVIEW_MAX_SIDE / min(height, width)
		if scale < 1:
			frame = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation = cv2.INTER_AREA)
		return frame

	def run_preview(self) -> None:
		item = self.preview_item()
		if not item:
			self.preview_run = None
			self.preview_view.set_pixmap(QPixmap())
			self.preview_label.setText('')
			return
		# a batch, another page or the last preview is still running, this one follows once facefusion is free
		if self.running or not self.isVisible() or self.session.runner.is_running():
			self.preview_waiting = True
			if self.preview_running or self.session.runner.is_running():
				self.preview_label.setText('Waiting for FaceFusion to finish…')
			return
		self.preview_waiting = False
		frame = self.scaled_frame(item)
		run_frame = self.run_frame(item)
		if frame is None or run_frame is None:
			self.preview_label.setText('Could not read %s' % os.path.basename(item.get('target')))
			return
		os.makedirs(PREVIEW_DIR, exist_ok = True)
		frame_path = os.path.join(PREVIEW_DIR, 'batch_frame.png')
		output_path = os.path.join(PREVIEW_DIR, 'batch_result.png')
		for path in [ frame_path, output_path ]:
			if os.path.exists(path):
				os.remove(path)
		write_image(frame_path, run_frame)
		values = self.run_values()
		values.update({ 'reference_frame_index': 0, 'trim_frame_start': None, 'trim_frame_end': None, 'workflow_mode': 'auto' })
		# a single frame has no sound to sync lips to
		processors = list(values.get('processors') or [])
		values['processors'] = [ processor for processor in processors if processor != 'lip_syncer' ]
		if processors and not values.get('processors'):
			self.preview_label.setText('Lip sync needs the whole video, a single frame has nothing to preview.')
			return
		args = [ 'run' ]
		image_sources = [ path for path in item.get('sources') if media_kind(path) == 'image' ]
		if image_sources:
			args += [ '-s' ] + image_sources
		args += [ '-t', frame_path, '-o', output_path ] + self.session.option_args(values)
		# another target or frame shows right away, so the faces drawn over it line up while the preview runs
		frame_key = (item.get('target'), self.preview_frame_index(item.get('target')))
		if frame_key != self.preview_frame_key:
			self.preview_view.set_pixmap(to_pixmap(frame))
			self.preview_view.set_faces([])
			self.preview_frame_key = frame_key
		self.preview_run = item
		self.preview_frame = frame
		self.preview_running = True
		self.schedule_face_scan()
		if not self.session.run_facefusion('Batch preview of ' + os.path.basename(item.get('target')), args, preview = True):
			self.preview_running = False
			self.preview_label.setText('')
			return
		self.preview_started = time.monotonic()
		self.preview_floor = 0.0
		self.preview_estimate = self.session.preview_key('batch_preview_seconds')
		self.preview_bar.setValue(0)
		self.preview_row.show()
		self.preview_clock.start()
		self.tick_preview_progress()

	def tick_preview_progress(self) -> None:
		"""moves the preview bar along the time Batch previews usually take, like Studio's"""
		elapsed = time.monotonic() - self.preview_started
		expected = self.session.expected_preview_seconds(self.preview_estimate)
		ratio = estimated_progress(elapsed, expected, self.preview_floor)
		left = expected - elapsed
		name = self.preview_name()
		self.preview_bar.setValue(int(ratio * 1000))
		self.preview_label.setText('Making the preview of %s…  %d%%' % (name, ratio * 100) + ('  ·  about %d s left' % round(left) if left >= 1 and self.preview_floor < 0.9 else ''))

	def preview_name(self) -> str:
		"""the previewed target, with the frame when it is a video"""
		if not self.preview_run:
			return ''
		target = self.preview_run.get('target')
		return os.path.basename(target) + (' #%d' % self.preview_frame_index(target) if media_kind(target) == 'video' else '')

	def stop_preview(self) -> None:
		if not self.preview_running:
			return
		# no new preview right after, the next change or the refresh button makes one
		self.preview_timer.stop()
		self.preview_waiting = False
		self.session.runner.stop()

	def on_preview_finished(self, exit_code : int, elapsed : float) -> None:
		self.preview_running = False
		self.preview_clock.stop()
		self.preview_row.hide()
		output_path = os.path.join(PREVIEW_DIR, 'batch_result.png')
		name = self.preview_name()
		if exit_code == 0 and os.path.exists(output_path):
			self.session.remember_preview_seconds(self.preview_estimate, elapsed)
			self.preview_view.set_compare(to_pixmap(self.preview_frame), to_pixmap(read_frame(output_path)))
			result = self.check_results.get(self.preview_run.get('target'))
			if result and result.get('blocked'):
				# one clean frame says nothing about the rest, Run Batch checks the whole video
				flagged = self.flagged_words(self.preview_run.get('target'))
				self.preview_label.setText('%s  ·  %.1f s  ·  This frame is OK, but Run Batch skips this %s: %s' % (name, elapsed, 'video' if result.get('kind') == 'video' else 'target', flagged or BLOCKED_TITLE))
			else:
				self.preview_label.setText('%s  ·  %.1f s  ·  drag the divider to compare' % (name, elapsed))
		elif exit_code == -1:
			self.preview_label.setText('Preview stopped')
		elif self.session.nsfw_block_message(True):
			self.preview_view.set_pixmap(to_pixmap(self.preview_frame))
			self.preview_label.setText('⛔ %s in %s, FaceFusion refused to process it' % (BLOCKED_TITLE, name))
		else:
			status, _ = self.session.describe_run_error()
			self.preview_label.setText(status if self.session.run_error() else 'Preview failed (exit code %d), see Console' % exit_code)
		if self.retry_waiting:
			self.retry_waiting = False
			QTimer.singleShot(0, self.begin_run)
		elif self.preview_waiting:
			QTimer.singleShot(0, self.run_preview)

	# faces and mask over the preview, scanned on the same smaller frame the preview runs on

	def overlays_on(self) -> bool:
		return self.faces_button.isChecked() or self.mask_button.isChecked()

	def on_overlay_toggled(self, key : str, checked : bool) -> None:
		self.session.config.set(key, checked)
		self.faces_label.setVisible(self.overlays_on())
		self.preview_view.set_faces([])
		if self.overlays_on():
			self.schedule_face_scan()
		else:
			self.faces_timer.stop()
			self.face_probe.stop()

	def schedule_face_scan(self) -> None:
		if self.overlays_on() and self.isVisible():
			self.faces_timer.start()

	def scan_faces(self) -> None:
		if not self.overlays_on() or not self.isVisible():
			return
		item = self.preview_run
		if not item:
			self.faces_label.setText('Make a preview first, the faces and the mask are drawn over it.')
			return
		if not self.session.is_configured() or not self.session.schema:
			self.faces_label.setText('Set up FaceFusion first, face detection runs in its environment.')
			return
		frame = self.preview_frame if self.preview_frame is not None else self.scaled_frame(item)
		if frame is None:
			return
		mask = self.mask_button.isChecked()
		sources = [ path for path in item.get('sources') if media_kind(path) == 'image' ]
		args = self.session.option_args(self.run_values(), FACE_GROUPS + (MASK_GROUPS if mask else []))
		self.face_probe.scan(frame, None, sources, args, tag = self.faces_tag(item), mask = mask)

	def faces_tag(self, item : Dict[str, Any]) -> Tuple:
		return 'batch', self.run_key(item), self.preview_frame_index(item.get('target'))

	def on_faces_loading(self, loading : bool) -> None:
		if loading and self.overlays_on():
			self.faces_label.setText('Loading the face models…')

	def on_faces_scanned(self, result : dict) -> None:
		if not self.overlays_on() or not self.preview_run or result.get('tag') != self.faces_tag(self.preview_run):
			return
		faces = (result.get('faces') or []) if self.faces_button.isChecked() else []
		mask = read_mask(result.get('mask')) if self.mask_button.isChecked() else None
		boxes = [ (face.get('box'), face_tag(index, face), bool(face.get('selected'))) for index, face in enumerate(faces) ]
		self.preview_view.set_faces(boxes, (result.get('width') or 0, result.get('height') or 0), mask)
		lines = []
		problem = bool(result.get('error'))
		if self.faces_button.isChecked() or problem:
			lines.append(describe_faces(result, 0))
			problem = problem or not any(face.get('selected') for face in result.get('faces') or [])
		if self.mask_button.isChecked() and not result.get('error'):
			values = self.run_values()
			schema = self.session.schema
			lines.append(describe_mask(result, { dest: values.get(dest, schema.options.get(dest, {}).get('default')) for dest in [ 'face_mask_padding', 'face_mask_blur', 'face_mask_areas', 'face_mask_regions' ] } if schema else {}))
			problem = problem or not result.get('masked')
		self.faces_label.setText('\n'.join(line for line in lines if line))
		self.faces_label.setTextColor('#b45309' if problem else '#707070', '#f0b35a' if problem else '#9a9a9a')

	def on_clock(self) -> None:
		template = self.output_name_edit.text() or AUTOMATIC_TEMPLATE
		if not self.running and self.isVisible() and CLOCK_TAGS.search(template) and len(self.plan_items) <= LIST_LIMIT:
			self.refresh_matches(True)

	def watch_output_folders(self) -> None:
		"""the output folder and the folders below it the outputs go to, so new files there replan the names"""
		output_dir = os.path.normpath(self.output_dir()) if self.output_dir() else ''
		folders = { output_dir } if output_dir else set()
		for item in self.plan_items:
			folder = os.path.dirname(item.get('output'))
			while output_dir and folder not in folders and len(folder) > len(output_dir) and os.path.normcase(folder).startswith(os.path.normcase(output_dir)):
				folders.add(folder)
				folder = os.path.dirname(folder)
		wanted = sorted(folder for folder in folders if os.path.isdir(folder))
		watched = sorted(self.watcher.directories())
		if wanted != watched:
			if watched:
				self.watcher.removePaths(watched)
			if wanted:
				self.watcher.addPaths(wanted)

	def last_dir(self) -> str:
		folder = self.target_dir.text().strip()
		if os.path.isdir(folder):
			return folder
		if self.target_files:
			return os.path.dirname(self.target_files[-1])
		return os.path.dirname(self.source_paths[0]) if self.source_paths else os.path.expanduser('~')

	def browse(self, edit : LineEdit) -> None:
		path = QFileDialog.getExistingDirectory(self, 'Choose folder', edit.text() or os.path.expanduser('~'))
		if path:
			edit.setText(os.path.normpath(path))

	def output_dir(self) -> str:
		return self.output_dir_edit.text().strip()

	def target_pattern(self) -> str:
		folder = self.target_dir.text().strip()
		return os.path.join(folder, self.target_filter.currentData()) if folder else ''

	def folder_targets(self) -> List[str]:
		pattern = self.target_pattern()
		# All files also matches text files and the like, only images and videos can be targets
		return [ path for path in sorted(glob.glob(pattern)) if media_kind(path) in [ 'image', 'video' ] ] if pattern else []

	def targets(self) -> List[str]:
		"""the files of the target folder, then the added files that still exist and are not in it"""
		targets = self.folder_targets()
		known = { os.path.normcase(os.path.normpath(path)) for path in targets }
		return targets + [ path for path in self.target_files if os.path.normcase(path) not in known and os.path.isfile(path) ]

	def runs(self) -> List[Dict[str, Any]]:
		"""one run per selected face and target, the selected audio goes into each of them"""
		targets = self.targets()
		sources = self.active_sources()
		images = [ path for path in sources if media_kind(path) == 'image' ]
		audio = [ path for path in sources if media_kind(path) == 'audio' ]
		return [ { 'source': image, 'sources': ([ image ] if image else []) + audio, 'target': target } for image in images or [ None ] for target in targets ]

	def name_tag_values(self, run : Optional[Dict[str, Any]] = None) -> Dict[str, str]:
		"""what each file name tag stands for in a run, the first planned one when none is given"""
		run = run or (self.plan_items[0] if self.plan_items else { 'sources': self.active_sources()[:1], 'target': '' })
		values = run_tag_values(self.store)
		values.update(
		{
			'target': os.path.splitext(os.path.basename(run.get('target') or ''))[0],
			'source': '+'.join(self.source_name(path) for path in run.get('sources')),
			'frame': '0'
		})
		return values

	def output_extension(self, run : Optional[Dict[str, Any]] = None) -> str:
		run = run or (self.plan_items[0] if self.plan_items else { 'sources': [], 'target': '' })
		target = run.get('target') or ''
		if media_kind(target) == 'image' and any(media_kind(path) == 'audio' for path in run.get('sources')):
			return '.mp4'
		return os.path.splitext(target)[1].lower() or '.mp4'

	@staticmethod
	def run_key(item : Dict[str, Any]) -> Tuple:
		return tuple(item.get('sources')), item.get('target')

	def plan(self) -> List[Dict[str, Any]]:
		template = self.output_name_edit.text()
		output_dir = self.output_dir()
		subfolders = self.subfolders_button.isChecked()
		# outputs planned earlier in the list count as taken, so {n} keeps counting across the batch
		taken : set = set()
		items = []
		runs = self.runs()
		self.removed_count = sum(1 for run in runs if self.run_key(run) in self.removed)
		for run in [ run for run in runs if self.run_key(run) not in self.removed ]:
			path, replaces = build_output(template, self.name_tag_values(run), output_dir, self.output_extension(run), subfolders, taken)
			key = os.path.normcase(path)
			name = os.path.relpath(path, output_dir) if subfolders and output_dir else os.path.basename(path)
			items.append(dict(run, output = path, name = name, replaces = replaces, repeated = key in taken, status = ''))
			taken.add(key)
		return items

	def refresh_matches(self, keep_results : bool = False) -> None:
		"""plans the outputs again, keep_results leaves the runs that ended in the list with what they made"""
		if self.running:
			return
		# a change the user made and that is still waiting starts the list over
		keep_results = keep_results and not self.refresh_timer.isActive()
		self.refresh_timer.stop()
		self.save()
		self.plan_items = self.plan()
		finished = { self.run_key(item): item for item in self.items if item.get('status') in FINISHED } if keep_results else {}
		self.items = [ finished.get(self.run_key(item), item) for item in self.plan_items ]
		self.pairs_count = len(self.items)
		self.refresh_list()
		faces = len({ item.get('source') for item in self.items if item.get('source') })
		targets = len({ item.get('target') for item in self.items })
		summary = '%d faces × %d targets = %d outputs' % (faces, targets, len(self.items)) if faces else '%d targets' % targets
		self.summary.setText(summary + ('  ·  %d removed' % self.removed_count if self.removed_count else ''))
		self.restore_button.setVisible(bool(self.removed_count))
		self.remove_button.setEnabled(bool(self.items))
		self.refresh_name_preview()
		self.watch_output_folders()
		self.sync_timeline()

	def refresh_name_preview(self) -> None:
		"""what the next run would call its files, which moves on once a run made the files before"""
		template = self.output_name_edit.text()
		plan = self.plan_items
		if not plan:
			self.name_preview_label.setText('Choose a target folder or drop files on the list to see the file names')
			self.name_preview_label.setToolTip('')
			return
		first = plan[0]
		notes = [ 'and %d more' % (len(plan) - 1) ] if len(plan) > 1 else []
		if not template.strip():
			notes.append('automatic name, click the tag button to build your own')
		repeated = sum(1 for item in plan if item.get('repeated'))
		if repeated:
			notes.append('%d %s the same name as another, add {source} or {n}' % (repeated, 'output has' if repeated == 1 else 'outputs have'))
		replaces = sum(1 for item in plan if item.get('replaces') and not item.get('repeated'))
		if replaces:
			notes.append('%s, add {n} to keep both' % ('replaces an existing file' if replaces == 1 else '%d replace existing files' % replaces))
		unknown = unknown_tags(template)
		if unknown:
			notes.append('unknown tag %s is kept as typed' % ', '.join(unknown))
		self.name_preview_label.setText('Saves As:  %s%s' % (first.get('name'), ('   ·   ' + '   ·   '.join(notes)) if notes else ''))
		self.name_preview_label.setToolTip(first.get('output'))

	def show_name_tags(self) -> None:
		Flyout.make(NameTagsView(self), self.tags_button, self.window(), FlyoutAnimationType.DROP_DOWN)

	def item_status(self, item : Dict[str, Any]) -> str:
		if item.get('status'):
			return item.get('status')
		result = self.check_results.get(item.get('target'))
		return 'skip' if result and result.get('blocked') else ''

	def item_lines(self, item : Dict[str, Any]) -> Tuple[str, str]:
		"""the output name, and what it is made from with why it failed"""
		reason = item.get('reason') if self.item_status(item) == 'failed' else ''
		sources = ''.join('  +  ' + self.source_name(path) for path in item.get('sources'))
		return item.get('name'), '←  %s%s' % (os.path.basename(item.get('target')), sources) + ('  ·  ' + reason if reason else '')

	def item_text(self, item : Dict[str, Any]) -> str:
		name, detail = self.item_lines(item)
		return '%s  %s' % (name, detail)

	@staticmethod
	def progress_words(item : Dict[str, Any]) -> str:
		"""how far the running output is, empty until facefusion reports its frames"""
		if 'progress' not in item:
			return ''
		speed = item.get('speed', '').replace('frame/s', ' frames/s')
		return 'Processing %d%%%s  ·  ' % (int(item.get('progress') * 100), '  ·  ' + speed if speed else '')

	def flagged_words(self, target : str) -> str:
		"""how much of a video the nsfw check flagged, empty when it gave no count"""
		result = self.check_results.get(target)
		if not result or result.get('kind') != 'video' or not result.get('checked'):
			return ''
		return 'NSFW in %d of %d checked frames' % (len(result.get('flagged') or []), result.get('checked'))

	def status_words(self, item : Dict[str, Any]) -> str:
		"""the words before the names, a flagged video says how much of it was flagged"""
		status = self.item_status(item)
		flagged = self.flagged_words(item.get('target')) if status in [ 'blocked', 'skip' ] else ''
		if flagged:
			return flagged + (', will be skipped  ·  ' if status == 'skip' else '  ·  ')
		return STATUS_LOOK.get(status, ('', '', None, None))[1]

	def item_tooltip(self, item : Dict[str, Any]) -> str:
		return self.status_words(item) + self.item_text(item) + '\n' + item.get('output')

	def refresh_list(self) -> None:
		# the rows are drawn from items, so only their number has to match
		shown = self.items[:LIST_LIMIT]
		while self.match_list.count() > len(shown):
			self.match_list.takeItem(self.match_list.count() - 1)
		while self.match_list.count() < len(shown):
			self.match_list.addItem(QListWidgetItem())
		for index, item in enumerate(shown):
			self.match_list.item(index).setToolTip(self.item_tooltip(item))
		self.match_list.viewport().update()

	def refresh_item(self, index : int) -> None:
		if index < min(len(self.items), LIST_LIMIT):
			self.match_list.item(index).setToolTip(self.item_tooltip(self.items[index]))
			self.match_list.viewport().update()

	def show_item_result(self, list_item) -> None:
		index = self.match_list.row(list_item)
		item = self.items[index] if 0 <= index < len(self.items) else None
		result = self.check_results.get(item.get('target')) if item else None
		if not result:
			return
		target = item.get('target')
		jump = (lambda frame_index: self.show_preview_frame(target, frame_index)) if media_kind(target) == 'video' else None
		# the preview moves to the first flagged frame behind the dialog, so closing it shows what was flagged
		if jump and result.get('flagged'):
			jump(result.get('flagged')[0])
		NsfwDialog(result, item.get('status') == 'blocked', self.window(), jump).exec()

	def show_preview_frame(self, target : str, index : int) -> None:
		"""moves the timeline of the shown target to a frame, its preview follows like a picked one"""
		if target != self.timeline_target:
			return
		self.preview_slider.setValue(min(index, self.preview_slider.maximum()))

	def save(self) -> None:
		self.session.config.set('batch',
		{
			'target_dir': self.target_dir.text().strip(),
			'target_pattern': self.target_filter.currentData(),
			'target_files': self.target_files,
			'output_dir': self.output_dir(),
			'template': self.output_name_edit.text().strip(),
			'subfolders': self.subfolders_button.isChecked(),
			'removed': [ [ list(sources), target ] for sources, target in list(self.removed)[:REMOVED_LIMIT] ],
			# the first frame needs no entry, the newest picks are kept
			'preview_frames': dict([ (target, index) for target, index in self.preview_frames.items() if index > 0 ][-FRAMES_LIMIT:])
		})

	# running, one facefusion run per output so a refused target only fails its own output

	def start(self) -> None:
		self.save()
		self.refresh_matches()
		if not self.pairs_count:
			self.session.notify.emit('warning', 'Nothing to do', 'No target files match the folder and filter, and no files were added to the list.')
			return
		if not self.output_dir():
			self.session.notify.emit('warning', 'No output folder', 'Choose where the results go.')
			return
		if any(item.get('repeated') for item in self.items):
			self.session.notify.emit('warning', 'Same file name', 'Some outputs would get the same name and replace each other. Add {source} or {n} to the file name.')
			return
		# with Sub-Folders on every source has its own folder, each is checked and created here
		for folder in sorted({ os.path.dirname(item.get('output')) for item in self.items }):
			if not folder_is_writable(folder):
				self.session.notify.emit('error', 'Cannot save to this folder', 'Windows does not allow saving to %s. Choose another output folder, such as Videos or Documents.' % folder)
				return
		replacing = [ item for item in self.items if item.get('replaces') and self.item_status(item) != 'skip' ]
		if replacing and not self.confirm_replace(replacing):
			return
		self.start_clock()
		# the nsfw check runs first, so the flagged rows show while the rest are made
		unchecked = sorted({ item.get('target') for item in self.items } - set(self.check_results))
		if unchecked:
			self.running = True
			self.prechecking = True
			self.stopped = False
			self.confirming = False
			self.progress.setValue(0)
			self.progress.resume()
			self.status_label.setText('Checking %d target%s with the NSFW filter before the batch…' % (len(unchecked), '' if len(unchecked) == 1 else 's'))
			if not self.session.run_content_check('batch', unchecked):
				self.prechecking = False
				self.running = False
				self.stop_clock()
				self.on_busy_changed(self.session.runner.is_running(), self.session.task)
				self.status_label.setText('Ready')
			return
		self.run_batch()

	def confirm_replace(self, replacing : List[Dict[str, Any]]) -> bool:
		"""asks before outputs replace files that are already in the output folder"""
		names = [ item.get('name') for item in replacing ]
		shown = '\n'.join('•  ' + name for name in names[:8]) + ('\n…and %d more' % (len(names) - 8) if len(names) > 8 else '')
		count = 'This file is' if len(names) == 1 else 'These %d files are' % len(names)
		box = MessageBox('Replace existing files?', '%s already in the output folder and the batch would replace %s:\n\n%s\n\nAdd {n} to the file name to keep both.' % (count, 'it' if len(names) == 1 else 'them', shown), self.window())
		box.yesButton.setText('Replace')
		box.cancelButton.setText('Cancel')
		return bool(box.exec())

	def run_batch(self) -> None:
		for item in self.items:
			# targets the nsfw check flagged would only be refused, so they are not run
			item['status'] = 'blocked' if self.item_status(item) == 'skip' else ''
		self.retry_only = set()
		self.begin_run()

	def start_clock(self) -> None:
		self.run_started = time.monotonic()
		self.run_clock.start()
		self.show_run_time()

	def stop_clock(self) -> None:
		self.run_clock.stop()
		self.time_label.setText('')

	def run_time(self) -> str:
		return format_length(time.monotonic() - self.run_started)

	def show_run_time(self) -> None:
		self.time_label.setText('⏱ ' + self.run_time())

	def begin_run(self) -> None:
		if not self.run_clock.isActive():
			self.start_clock()
		self.refresh_list()
		self.running = True
		self.stopped = False
		self.confirming = False
		self.started_count = 0
		self.current = -1
		self.run_task = ''
		self.progress.setValue(0)
		self.progress.resume()
		self.run_next()

	def stop(self) -> None:
		self.stopped = True
		self.session.runner.stop()

	def skip(self) -> None:
		if self.running and self.current >= 0:
			self.skipping = True
			self.session.runner.stop()

	def queued(self) -> List[int]:
		return [ index for index, item in enumerate(self.items) if not item.get('status') and (not self.retry_only or self.run_key(item) in self.retry_only) ]

	def run_next(self) -> None:
		queued = self.queued()
		if self.stopped or not queued:
			self.finish()
			return
		self.current = queued[0]
		self.skipping = False
		item = self.items[self.current]
		item['status'] = 'running'
		self.refresh_item(self.current)
		self.started_count += 1
		args = [ 'run' ]
		if item.get('sources'):
			args += [ '-s' ] + item.get('sources')
		values = self.run_values()
		values.update({ 'reference_frame_index': 0, 'trim_frame_start': None, 'trim_frame_end': None })
		script = 'facefusion.py'
		if extension(item.get('target')) in GIF_EXTENSIONS:
			# facefusion has no gif support, the helper converts to video and back around the run
			args = [ item.get('target'), item.get('output'), os.path.join(PREVIEW_DIR, 'gif'), '--' ] + args + self.session.option_args(values)
			script = os.path.join(APP_DIR, 'gif_run.py')
		else:
			args += [ '-t', item.get('target'), '-o', item.get('output') ] + self.session.option_args(values)
		total = self.started_count + len(queued) - 1
		task = 'Batch %d of %d: %s' % (self.started_count, total, item.get('name'))
		self.run_task = task
		self.status_label.setText(task)
		# the models stay loaded from one output to the next
		if not self.session.run_facefusion(task, args, script = script, keep_loaded = True):
			# the message says why, the batch ends here as if stopped so the buttons and status come back
			item['status'] = ''
			self.refresh_item(self.current)
			self.current = -1
			self.stopped = True
			self.finish()

	def on_busy_changed(self, busy : bool, task : str) -> None:
		self.start_button.setEnabled(not busy and not self.running)
		self.check_button.setEnabled(not busy and not self.running)
		self.stop_button.setEnabled(busy and self.running)
		self.skip_button.setEnabled(busy and self.running and not self.confirming and not self.prechecking)

	def on_progress(self, title : str, ratio : float, status : str) -> None:
		# the nsfw analysis before the frames fills its own bar quickly, only the frames count toward the output
		if self.running and self.current >= 0 and title.lower() == PROCESSING_TITLE:
			item = self.items[self.current]
			item['progress'] = ratio
			item['speed'] = status.split('  ·  ')[0]
			self.refresh_item(self.current)
			self.status_label.setText('%s  ·  processing %d%%' % (self.run_task, int(ratio * 100)))
			total = self.started_count + len(self.queued())
			self.progress.setValue(int((self.started_count - 1 + ratio) / max(total, 1) * 1000))

	def on_log(self, line : str) -> None:
		if self.preview_running and self.preview_clock.isActive():
			self.preview_floor = preview_step_floor(line, self.preview_floor)
			self.tick_preview_progress()

	def on_finished(self, exit_code : int, elapsed : float) -> None:
		if self.preview_running:
			self.on_preview_finished(exit_code, elapsed)
			return
		if self.preview_waiting and not self.running:
			# something else used facefusion, the preview that waited for it goes now
			QTimer.singleShot(0, self.run_preview)
		if not self.running or self.current < 0:
			return
		index, self.current = self.current, -1
		item = self.items[index]
		item.pop('progress', None)
		item.pop('speed', None)
		if exit_code == 0:
			item['status'] = 'done'
		elif exit_code == -1:
			item['status'] = 'skipped' if self.skipping and not self.stopped else ''
		elif self.session.nsfw_block_is_certain():
			item['status'] = 'blocked'
			self.check_results[item.get('target')] = { 'path': item.get('target'), 'kind': 'video', 'blocked': True, 'rate': self.session.content_rate, 'checked': 0, 'flagged': [], 'previews': [] }
		else:
			item['status'] = 'failed'
			error = self.session.run_error() or ''
			item['reason'] = (explain_error(error) or error).split('. ')[0][:120]
			# an image gives no reason when it is refused, the nsfw check confirms it once the batch is done
			item['confirm'] = bool(self.session.nsfw_block_message(media_kind(item.get('target')) == 'image'))
		self.refresh_item(index)
		# facefusion prints no progress for an image, so the bar moves on as each output ends
		total = self.started_count + len(self.queued())
		self.progress.setValue(int(self.started_count / max(total, 1) * 1000))
		# let every listener see this run end before the next one starts
		QTimer.singleShot(0, self.run_next)

	def finish(self) -> None:
		unconfirmed = sorted({ item.get('target') for item in self.items if item.pop('confirm', False) })
		if unconfirmed and not self.stopped:
			self.confirming = True
			self.status_label.setText('Checking %d refused image%s with the NSFW filter…' % (len(unconfirmed), '' if len(unconfirmed) == 1 else 's'))
			if self.session.run_content_check('batch', unconfirmed):
				return
			self.confirming = False
		self.running = False
		self.run_clock.stop()
		took = self.run_time()
		self.time_label.setText('Total ' + took)
		self.on_busy_changed(self.session.runner.is_running(), self.session.task)
		if self.preview_waiting:
			QTimer.singleShot(0, self.run_preview)
		counts = { status: sum(1 for item in self.items if item.get('status') == status) for status in [ 'done', 'skipped', 'blocked', 'failed' ] }
		# the made files take their names, Saves As and the rows not run yet move on to the next free ones
		self.refresh_matches(True)
		parts = [ '%d done' % counts.get('done') ]
		if counts.get('skipped'):
			parts.append('%d skipped' % counts.get('skipped'))
		if counts.get('blocked'):
			parts.append('%d skipped: NSFW content detected' % counts.get('blocked'))
		if counts.get('failed'):
			parts.append('%d failed, see Console' % counts.get('failed'))
		summary = ', '.join(parts)
		if self.stopped:
			self.progress.pause()
			self.status_label.setText('Stopped after %s  ·  %s' % (took, summary))
			return
		self.progress.setValue(1000)
		if counts.get('failed'):
			self.progress.error()
		self.status_label.setText('Batch finished in %s  ·  ' % took + summary + ('. Double-click a ⛔ output to see what was flagged.' if counts.get('blocked') else ''))
		if counts.get('blocked'):
			show_blocked_bar(self.window(), "FaceFusion's NSFW filter flagged %d of the outputs in this batch, so they were skipped. The other outputs were made as normal." % counts.get('blocked'))
		else:
			self.session.notify.emit('warning' if counts.get('failed') else 'success', 'Batch finished in ' + took, '%s in %s' % (summary, self.output_dir()))

	# nsfw check

	def check_targets(self) -> None:
		self.refresh_matches()
		targets = sorted({ item.get('target') for item in self.items })
		if not targets:
			self.session.notify.emit('warning', 'Nothing to check', 'No target files match the folder and filter, and no files were added to the list.')
			return
		self.confirming = False
		self.progress.setValue(0)
		self.progress.resume()
		self.status_label.setText('Checking %d target%s with the NSFW filter…' % (len(targets), '' if len(targets) == 1 else 's'))
		self.session.run_content_check('batch', targets)

	def on_content_checked(self, owner : str, results : list, exit_code : int) -> None:
		if owner != 'batch':
			return
		for result in results:
			self.check_results[result.get('path')] = result
		if self.prechecking:
			self.prechecking = False
			if exit_code == 0:
				self.progress.setValue(0)
				# let every listener see the check end before the first output starts
				QTimer.singleShot(0, self.run_batch)
				return
			self.running = False
			self.stop_clock()
			self.on_busy_changed(self.session.runner.is_running(), self.session.task)
			self.refresh_list()
		if self.confirming:
			# the check after a batch: refused images it flags were nsfw, the rest failed for another reason
			self.confirming = False
			for index, item in enumerate(self.items):
				result = self.check_results.get(item.get('target'))
				if item.get('status') == 'failed' and result and result.get('blocked'):
					item['status'] = 'blocked'
					self.refresh_item(index)
			self.finish()
			return
		if exit_code == -1:
			self.progress.pause()
			self.status_label.setText('NSFW check stopped')
			return
		if exit_code != 0:
			self.progress.error()
			self.status_label.setText('NSFW check failed (exit code %d). See Console.' % exit_code)
			return
		self.progress.setValue(1000)
		self.refresh_list()
		flagged = [ result for result in results if result.get('blocked') ]
		if flagged:
			skipped = sum(1 for item in self.items if self.item_status(item) == 'skip')
			self.status_label.setText('⛔ %s in %d of %d targets. Run Batch skips those %d outputs. Double-click one to see what was flagged.' % (BLOCKED_TITLE, len(flagged), len(results), skipped))
			show_blocked_bar(self.window(), "FaceFusion's NSFW filter flagged %d of the %d targets. Run Batch will skip them and make the rest." % (len(flagged), len(results)))
		else:
			self.status_label.setText('✓ No NSFW content detected in the %d targets' % len(results))
			self.session.notify.emit('success', 'No NSFW content detected', "FaceFusion's NSFW filter found nothing in the %d targets." % len(results))
