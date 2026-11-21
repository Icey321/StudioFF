import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, IconWidget, ImageLabel, InfoBar, InfoBarIcon, InfoBarPosition, MessageBoxBase, PushButton, StrongBodyLabel, SubtitleLabel, isDarkTheme

from ffstudio.media import format_timecode

BLOCKED_TITLE = 'NSFW content detected'
# how long the red NSFW bar shows before it closes by itself
BLOCKED_BAR_MS = 10000
BLOCKED_STATUS = "⛔ NSFW content detected: FaceFusion refused this target"
TRIM_TIP = 'FaceFusion only checks the trimmed part of a video, so trimming away the flagged part lets the rest be processed.'
THUMBNAIL_SIZE = 180
FLAG_COLOR = '#d13438'
PASS_COLOR = '#2b9a8a'
FLAG_TEXT = ('#d13438', '#ff6b6b')
GRAY_TEXT = ('#707070', '#9a9a9a')
SCORES_NOTE = 'FaceFusion refuses a picture when (2) of its (3) detectors go past their limit (the tick). Closer to the tick means a closer call.'
FRAME_SCORES_NOTE = 'Each bar is one detector, the tick is its limit; a frame is flagged when (2) of the (3) go past it.'


def describe_result(result : Dict[str, Any], after_run : bool) -> Tuple[str, str]:
	"""title and message for one content_check.py result"""
	name = os.path.basename(result.get('path') or '')
	refuses = 'refused to process it' if after_run else 'will refuse to process it'
	if result.get('error'):
		return 'NSFW check could not read the target', '%s: %s.' % (name, result.get('error'))
	flagged = len(result.get('flagged') or [])
	if result.get('frame_label') is not None:
		# a preview of one video frame
		if result.get('blocked'):
			return BLOCKED_TITLE, "FaceFusion's NSFW filter flagged frame #%d of %s as explicit, so FaceFusion refused to preview it. A full run is only refused when more than 10%% of the checked frames are flagged, NSFW Check tests the whole video." % (result.get('frame_label'), name)
		return 'No NSFW content detected', "FaceFusion's NSFW filter found nothing in frame #%d of %s." % (result.get('frame_label'), name)
	if result.get('kind') == 'image':
		if result.get('blocked'):
			return BLOCKED_TITLE, "FaceFusion's NSFW filter flagged the image %s as explicit, so FaceFusion %s. Choose a different target image." % (name, refuses)
		return 'No NSFW content detected', "FaceFusion's NSFW filter found nothing in %s. FaceFusion will process it." % name
	checked = result.get('checked') or 0
	if not checked:
		return 'Too short for the NSFW check', "FaceFusion's NSFW filter only looks at every 30th frame, and %s has fewer than 30 (in its trimmed part), so FaceFusion will process it without checking." % name
	if result.get('blocked'):
		return BLOCKED_TITLE, "FaceFusion's NSFW filter flagged %d of the %d checked frames in %s (%.0f%%, the limit is 10%%), so FaceFusion %s." % (flagged, checked, name, min(result.get('rate') or 0, 100), refuses)
	if flagged:
		return 'Under the NSFW limit', "FaceFusion's NSFW filter flagged %d of the %d checked frames in %s (%.0f%%), which is under its 10%% limit. FaceFusion will process it." % (flagged, checked, name, result.get('rate') or 0)
	return 'No NSFW content detected', "FaceFusion's NSFW filter found nothing in the %d checked frames of %s. FaceFusion will process it." % (checked, name)


def share_percent(share : float) -> str:
	return '%d%%' % round(max(share, 0) * 100)


class ScoreBar(QWidget):
	"""one detector's score: the tick in the middle is its limit, a bar past it is red"""

	def __init__(self, share : float, parent : Optional[QWidget] = None, height : int = 10) -> None:
		super().__init__(parent)
		self.share = share
		self.setFixedHeight(height)
		self.setMinimumWidth(40)

	def paintEvent(self, event) -> None:
		dark = isDarkTheme()
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		rect = QRectF(self.rect()).adjusted(0, 1, 0, -1)
		radius = rect.height() / 2
		painter.setPen(Qt.PenStyle.NoPen)
		painter.setBrush(QColor('#3a3a3a' if dark else '#e6e6e6'))
		painter.drawRoundedRect(rect, radius, radius)
		# the bar shows up to twice the limit
		filled = max(0.0, min(self.share / 2, 1.0))
		if filled > 0:
			painter.setBrush(QColor(FLAG_COLOR if self.share > 1 else PASS_COLOR))
			painter.drawRoundedRect(QRectF(rect.left(), rect.top(), max(rect.height(), rect.width() * filled), rect.height()), radius, radius)
		painter.setPen(QColor('#c8c8c8' if dark else '#404040'))
		x = int(rect.left() + rect.width() / 2)
		painter.drawLine(x, 0, x, self.height())


def score_block(shares : List[float], block : QWidget) -> QVBoxLayout:
	"""the three detectors' scores of one image, a bar and words each"""
	layout = QVBoxLayout()
	layout.setContentsMargins(0, 4, 0, 0)
	layout.setSpacing(6)
	layout.addWidget(StrongBodyLabel('Detector Scores', block))
	grid = QGridLayout()
	grid.setHorizontalSpacing(12)
	grid.setVerticalSpacing(6)
	for row, share in enumerate(shares):
		grid.addWidget(BodyLabel('Detector %d' % (row + 1), block), row, 0)
		bar = ScoreBar(share, block)
		bar.setMinimumWidth(200)
		grid.addWidget(bar, row, 1)
		text = CaptionLabel(share_percent(share) + ' of its limit' + ('  ·  flags it' if share > 1 else ''), block)
		text.setTextColor(*(FLAG_TEXT if share > 1 else GRAY_TEXT))
		grid.addWidget(text, row, 2)
	grid.setColumnStretch(1, 1)
	layout.addLayout(grid)
	layout.addWidget(gray_note(SCORES_NOTE, block))
	return layout


def gray_note(text : str, parent : QWidget) -> CaptionLabel:
	note = CaptionLabel(text, parent)
	note.setWordWrap(True)
	note.setTextColor(*GRAY_TEXT)
	return note


class FrameTile(QWidget):
	"""one flagged image or frame, clicking it reports the frame index"""

	def __init__(self, path : str, caption : str, frame_index : int, on_click : Optional[Callable[[int], None]], parent : Optional[QWidget] = None, shares : Optional[List[float]] = None) -> None:
		super().__init__(parent)
		self.setFixedWidth(THUMBNAIL_SIZE)
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(4)
		image = ImageLabel(self)
		pixmap = QPixmap(path)
		if not pixmap.isNull():
			image.setPixmap(pixmap.scaled(THUMBNAIL_SIZE, THUMBNAIL_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
		image.setBorderRadius(6, 6, 6, 6)
		layout.addWidget(image, 0, Qt.AlignmentFlag.AlignHCenter)
		label = CaptionLabel(caption, self)
		label.setTextColor(*GRAY_TEXT)
		layout.addWidget(label, 0, Qt.AlignmentFlag.AlignHCenter)
		if shares:
			bars = QHBoxLayout()
			bars.setSpacing(4)
			for number, share in enumerate(shares):
				bar = ScoreBar(share, self, 6)
				bar.setToolTip('Detector %d: %s of its limit' % (number + 1, share_percent(share)))
				bars.addWidget(bar)
			layout.addLayout(bars)
			numbers = CaptionLabel('  ·  '.join(share_percent(share) for share in shares), self)
			numbers.setTextColor(*GRAY_TEXT)
			layout.addWidget(numbers, 0, Qt.AlignmentFlag.AlignHCenter)
		self.frame_index = frame_index
		self.on_click = on_click
		if on_click:
			self.setCursor(Qt.CursorShape.PointingHandCursor)
			self.setToolTip('Show this frame on the timeline')

	def mouseReleaseEvent(self, event) -> None:
		if self.on_click and event.button() == Qt.MouseButton.LeftButton:
			self.on_click(self.frame_index)
		super().mouseReleaseEvent(event)


class NsfwDialog(MessageBoxBase):
	"""says plainly that the nsfw filter flagged the target and shows what it flagged"""

	def __init__(self, result : Dict[str, Any], after_run : bool, parent : QWidget, on_frame : Optional[Callable[[int], None]] = None) -> None:
		super().__init__(parent)
		title, message = describe_result(result, after_run)
		header = QHBoxLayout()
		header.setSpacing(10)
		icon = IconWidget(InfoBarIcon.ERROR if result.get('blocked') else InfoBarIcon.SUCCESS, self.widget)
		icon.setFixedSize(22, 22)
		header.addWidget(icon)
		header.addWidget(SubtitleLabel(title, self.widget), 1)
		self.viewLayout.addLayout(header)
		body = BodyLabel(message, self.widget)
		body.setWordWrap(True)
		self.viewLayout.addWidget(body)

		previews : List[str] = result.get('previews') or []
		flagged : List[int] = result.get('flagged') or []
		flagged_scores : List[Optional[List[float]]] = result.get('flagged_scores') or []
		near : List[Dict[str, Any]] = result.get('near') or []
		is_video = result.get('kind') == 'video'
		jump = (lambda frame_index: (on_frame(frame_index), self.accept())) if on_frame and is_video else None
		if previews:
			grid = QGridLayout()
			grid.setSpacing(12)
			for index, path in enumerate(previews):
				frame_index = flagged[index] if index < len(flagged) else 0
				if is_video:
					caption = 'Frame %d  ·  %s' % (frame_index, format_timecode(frame_index, result.get('fps') or 0))
				else:
					caption = 'Flagged frame #%d' % result.get('frame_label') if result.get('frame_label') is not None else 'Flagged image'
				shares = flagged_scores[index] if is_video and index < len(flagged_scores) else None
				grid.addWidget(FrameTile(path, caption, frame_index, jump, self.widget, shares), index // 3, index % 3)
			if is_video:
				for column in range(3):
					grid.setColumnStretch(column, 1)
			self.viewLayout.addLayout(grid)
			notes = []
			if len(flagged) > len(previews):
				notes.append('Showing %d of %d flagged frames.' % (len(previews), len(flagged)))
			if jump:
				notes.append('Click a frame to show it on the timeline.')
			if notes:
				self.viewLayout.addWidget(gray_note('  '.join(notes), self.widget))
		if result.get('scores'):
			self.viewLayout.addLayout(score_block(result.get('scores'), self.widget))
		if near:
			self.viewLayout.addWidget(StrongBodyLabel('Closest Frames That Passed', self.widget))
			grid = QGridLayout()
			grid.setSpacing(12)
			for index, item in enumerate(near):
				frame_index = item.get('frame') or 0
				caption = 'Frame %d  ·  %s' % (frame_index, format_timecode(frame_index, result.get('fps') or 0))
				grid.addWidget(FrameTile(item.get('preview') or '', caption, frame_index, jump, self.widget, item.get('scores')), 0, index)
			# three equal columns, so the frames line up under the flagged ones
			for column in range(3):
				grid.setColumnStretch(column, 1)
			self.viewLayout.addLayout(grid)
		if is_video and (any(flagged_scores) or near):
			self.viewLayout.addWidget(gray_note(FRAME_SCORES_NOTE, self.widget))
		if is_video and result.get('blocked'):
			self.viewLayout.addWidget(gray_note(TRIM_TIP, self.widget))

		self.yesButton.setText('Close')
		self.cancelButton.hide()
		self.buttonLayout.insertStretch(0, 1)
		self.widget.setMinimumWidth(min(620, 3 * THUMBNAIL_SIZE + 80) if previews or near else 460)


def show_blocked_bar(parent : QWidget, message : str, button_text : str = '', on_button : Optional[Callable[[], None]] = None) -> InfoBar:
	"""a red bar that stays longer than the other notifications, so a refused run is hard to miss, and then goes by itself"""
	bar = InfoBar.error(BLOCKED_TITLE, message, orient = Qt.Orientation.Vertical, isClosable = True, position = InfoBarPosition.TOP_RIGHT, duration = BLOCKED_BAR_MS, parent = parent)
	if on_button:
		button = PushButton(button_text, bar)
		button.clicked.connect(on_button)
		button.clicked.connect(bar.close)
		bar.addWidget(button)
	return bar
