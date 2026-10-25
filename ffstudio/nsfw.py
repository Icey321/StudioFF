import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, IconWidget, ImageLabel, InfoBar, InfoBarIcon, InfoBarPosition, MessageBoxBase, PushButton, SubtitleLabel

from ffstudio.media import format_timecode

BLOCKED_TITLE = 'NSFW content detected'
# how long the red NSFW bar shows before it closes by itself
BLOCKED_BAR_MS = 10000
BLOCKED_STATUS = "⛔ NSFW content detected: FaceFusion refused this target"
TRIM_TIP = 'FaceFusion only checks the trimmed part of a video, so trimming away the flagged part lets the rest be processed.'
THUMBNAIL_SIZE = 180


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
	if result.get('blocked'):
		return BLOCKED_TITLE, "FaceFusion's NSFW filter flagged %d of the %d checked frames in %s (%.0f%%, the limit is 10%%), so FaceFusion %s." % (flagged, checked, name, min(result.get('rate') or 0, 100), refuses)
	if flagged:
		return 'Under the NSFW limit', "FaceFusion's NSFW filter flagged %d of the %d checked frames in %s (%.0f%%), which is under its 10%% limit. FaceFusion will process it." % (flagged, checked, name, result.get('rate') or 0)
	return 'No NSFW content detected', "FaceFusion's NSFW filter found nothing in the %d checked frames of %s. FaceFusion will process it." % (checked, name)


class FrameTile(QWidget):
	"""one flagged image or frame, clicking it reports the frame index"""

	def __init__(self, path : str, caption : str, frame_index : int, on_click : Optional[Callable[[int], None]], parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
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
		label.setTextColor('#707070', '#9a9a9a')
		layout.addWidget(label, 0, Qt.AlignmentFlag.AlignHCenter)
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
		is_video = result.get('kind') == 'video'
		if previews:
			jump = (lambda frame_index: (on_frame(frame_index), self.accept())) if on_frame and is_video else None
			grid = QGridLayout()
			grid.setSpacing(12)
			for index, path in enumerate(previews):
				frame_index = flagged[index] if index < len(flagged) else 0
				if is_video:
					caption = 'Frame %d  ·  %s' % (frame_index, format_timecode(frame_index, result.get('fps') or 0))
				else:
					caption = 'Flagged frame #%d' % result.get('frame_label') if result.get('frame_label') is not None else 'Flagged image'
				grid.addWidget(FrameTile(path, caption, frame_index, jump, self.widget), index // 3, index % 3)
			self.viewLayout.addLayout(grid)
			notes = []
			if len(flagged) > len(previews):
				notes.append('Showing %d of %d flagged frames.' % (len(previews), len(flagged)))
			if jump:
				notes.append('Click a frame to show it on the timeline.')
			if notes:
				note = CaptionLabel('  '.join(notes), self.widget)
				note.setTextColor('#707070', '#9a9a9a')
				self.viewLayout.addWidget(note)
		if is_video and result.get('blocked'):
			tip = CaptionLabel(TRIM_TIP, self.widget)
			tip.setWordWrap(True)
			tip.setTextColor('#707070', '#9a9a9a')
			self.viewLayout.addWidget(tip)

		self.yesButton.setText('Close')
		self.cancelButton.hide()
		self.buttonLayout.insertStretch(0, 1)
		self.widget.setMinimumWidth(min(620, 3 * THUMBNAIL_SIZE + 80) if previews else 460)


def show_blocked_bar(parent : QWidget, message : str, button_text : str = '', on_button : Optional[Callable[[], None]] = None) -> InfoBar:
	"""a red bar that stays longer than the other notifications, so a refused run is hard to miss, and then goes by itself"""
	bar = InfoBar.error(BLOCKED_TITLE, message, orient = Qt.Orientation.Vertical, isClosable = True, position = InfoBarPosition.TOP_RIGHT, duration = BLOCKED_BAR_MS, parent = parent)
	if on_button:
		button = PushButton(button_text, bar)
		button.clicked.connect(on_button)
		button.clicked.connect(bar.close)
		bar.addWidget(button)
	return bar
