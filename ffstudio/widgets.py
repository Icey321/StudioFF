import math
import os
import textwrap
from typing import Callable, List, Optional, Tuple

import cv2
import numpy

from PySide6.QtCore import QEvent, QMimeData, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDrag, QDragEnterEvent, QDropEvent, QFont, QFontMetrics, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QFrame, QHBoxLayout, QLineEdit, QPushButton, QSizePolicy, QSplitter, QSplitterHandle, QStyle, QStyleOptionButton, QStylePainter, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ComboBox, CompactDoubleSpinBox, CompactSpinBox, FlowLayout, FluentIcon, FluentIconBase, Flyout, FlyoutAnimationType, IconWidget, SimpleCardWidget, StrongBodyLabel, Theme, TogglePushButton, ToolTipFilter, TransparentToolButton, isDarkTheme, themeColor
from qfluentwidgets.components.widgets.line_edit import LineEditButton
from qfluentwidgets.components.widgets.spin_box import CompactSpinBoxBase, SpinIcon

from ffstudio.drive import describe_drive, drive_text


class WheelGuard(QObject):
	"""lets the mouse wheel change a control only after it was clicked, otherwise the wheel scrolls the page"""

	def eventFilter(self, watched : QObject, event : QEvent) -> bool:
		if event.type() == QEvent.Type.Wheel and not watched.hasFocus():
			# an ignored event travels on to the parent, so the scroll area still scrolls
			event.ignore()
			return True
		return False


WHEEL_GUARD = WheelGuard()


def guard_wheel(*widgets : QWidget) -> None:
	for widget in widgets:
		# the default wheel focus would hand focus to whatever control the wheel passes over
		widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
		widget.installEventFilter(WHEEL_GUARD)


class StepArrows(QWidget):
	"""the small up and down arrows inside a number box, the top one steps up and the bottom one steps down

	holding the mouse button repeats the step like holding an arrow key
	"""

	def __init__(self, box : QAbstractSpinBox) -> None:
		super().__init__(box)
		self.box = box
		self.hovered = 0
		self.pressed = 0
		self.setFixedSize(26, 33)
		self.setMouseTracking(True)
		self.setCursor(Qt.CursorShape.ArrowCursor)
		self.repeat_timer = QTimer(self)
		self.repeat_timer.timeout.connect(self.repeat)

	def direction_at(self, y : float) -> int:
		return 1 if y < self.height() / 2 else -1

	def can_step(self, direction : int) -> bool:
		if not self.box.isEnabled() or self.box.isReadOnly():
			return False
		flag = QAbstractSpinBox.StepEnabledFlag.StepUpEnabled if direction > 0 else QAbstractSpinBox.StepEnabledFlag.StepDownEnabled
		return bool(self.box.stepEnabled() & flag)

	def step(self, direction : int) -> None:
		if self.can_step(direction):
			self.box.stepBy(direction)

	def repeat(self) -> None:
		self.repeat_timer.setInterval(60)
		self.step(self.pressed)

	def mousePressEvent(self, event) -> None:
		if event.button() == Qt.MouseButton.LeftButton:
			self.pressed = self.direction_at(event.position().y())
			self.step(self.pressed)
			self.repeat_timer.start(400)
			self.update()

	def mouseReleaseEvent(self, event) -> None:
		self.pressed = 0
		self.repeat_timer.stop()
		self.update()

	def mouseMoveEvent(self, event) -> None:
		direction = self.direction_at(event.position().y())
		if direction != self.hovered:
			self.hovered = direction
			self.update()

	def leaveEvent(self, event) -> None:
		self.hovered = 0
		self.update()

	def paintEvent(self, event) -> None:
		painter = QPainter(self)
		painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
		size = 9
		x = (self.width() - size) / 2 - 1
		middle = self.height() / 2
		# a gap between the arrows so each one is easy to hit on its own
		gap = 2.5
		for direction, icon, top in ((1, SpinIcon.UP, middle - gap - size), (-1, SpinIcon.DOWN, middle + gap)):
			if self.hovered == direction and self.can_step(direction):
				painter.setPen(Qt.PenStyle.NoPen)
				if isDarkTheme():
					painter.setBrush(QColor(255, 255, 255, 28 if self.pressed else 16))
				else:
					painter.setBrush(QColor(0, 0, 0, 20 if self.pressed else 10))
				painter.drawRoundedRect(QRectF(x - 5, top - 2.5, size + 10, size + 5), 3, 3)
			painter.setOpacity(1.0 if self.can_step(direction) else 0.3)
			icon.render(painter, QRectF(x, top, size, size))
		painter.setOpacity(1.0)


def use_step_arrows(box : QAbstractSpinBox) -> None:
	# the stock arrow button opens a flyout with large arrows, these small arrows step the value in place
	box.compactSpinButton.hide()
	box.step_arrows = StepArrows(box)
	box.hBoxLayout.addWidget(box.step_arrows, 0, Qt.AlignmentFlag.AlignRight)


class NumberBox(CompactSpinBox):
	"""compact spin box whose small arrows step the value, a click in the text gives a text cursor

	the stock one pops up a flyout with large arrows on every focus and on its arrow button
	"""

	def __init__(self, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		use_step_arrows(self)

	def focusInEvent(self, event) -> None:
		super(CompactSpinBoxBase, self).focusInEvent(event)


class DecimalBox(CompactDoubleSpinBox):
	def __init__(self, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		use_step_arrows(self)

	def focusInEvent(self, event) -> None:
		super(CompactSpinBoxBase, self).focusInEvent(event)


class RangeSlider(QWidget):
	"""a slider with two grips for a lower and an upper value, drawn like the fluent slider

	a click on the groove moves the nearer grip there, the arrow keys and the wheel move the grip used last
	"""
	values_changed = Signal(int, int)
	HANDLE = 22

	def __init__(self, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.minimum = 0
		self.maximum = 100
		self.low = 0
		self.high = 100
		self.active = 0
		self.dragging = -1
		self.hovered = -1
		self.setMouseTracking(True)
		self.setFixedHeight(self.HANDLE)
		self.setMinimumWidth(80)
		self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

	def set_range(self, minimum : int, maximum : int) -> None:
		self.minimum, self.maximum = minimum, max(minimum, maximum)
		self.set_values(self.low, self.high)

	def values(self) -> Tuple[int, int]:
		return self.low, self.high

	def set_values(self, low : int, high : int) -> None:
		"""sets both grips without emitting values_changed"""
		low = min(max(int(low), self.minimum), self.maximum)
		high = min(max(int(high), self.minimum), self.maximum)
		self.low, self.high = min(low, high), max(low, high)
		self.update()

	def move_grip(self, grip : int, value : int) -> None:
		value = min(max(int(value), self.minimum), self.maximum)
		low, high = (min(value, self.high), self.high) if grip == 0 else (self.low, max(value, self.low))
		if (low, high) != (self.low, self.high):
			self.low, self.high = low, high
			self.update()
			self.values_changed.emit(low, high)

	def groove(self) -> Tuple[float, float]:
		radius = self.HANDLE / 2
		return radius, max(1.0, self.width() - self.HANDLE)

	def grip_x(self, value : int) -> float:
		left, width = self.groove()
		span = self.maximum - self.minimum
		return left + (value - self.minimum) / span * width if span else left

	def value_at(self, x : float) -> int:
		left, width = self.groove()
		return round(self.minimum + (x - left) / width * (self.maximum - self.minimum))

	def grip_at(self, x : float) -> int:
		low_x, high_x = self.grip_x(self.low), self.grip_x(self.high)
		if abs(low_x - high_x) < 1:
			# grips on top of each other: the side of the click decides, so they can always be pulled apart
			return 0 if x < low_x else 1
		return 0 if abs(x - low_x) <= abs(x - high_x) else 1

	def paintEvent(self, _) -> None:
		painter = QPainter(self)
		painter.setRenderHints(QPainter.RenderHint.Antialiasing)
		painter.setPen(Qt.PenStyle.NoPen)
		dark = isDarkTheme()
		enabled = self.isEnabled()
		accent = themeColor() if enabled else QColor(255, 255, 255, 80) if dark else QColor(0, 0, 0, 60)
		left, width = self.groove()
		middle = self.HANDLE / 2
		painter.setBrush(QColor(255, 255, 255, 115) if dark else QColor(0, 0, 0, 100))
		painter.drawRoundedRect(QRectF(left, middle - 2, width, 4), 2, 2)
		low_x, high_x = self.grip_x(self.low), self.grip_x(self.high)
		painter.setBrush(accent)
		painter.drawRoundedRect(QRectF(low_x, middle - 2, high_x - low_x, 4), 2, 2)
		for grip, x in enumerate((low_x, high_x)):
			painter.setPen(QColor(0, 0, 0, 90 if dark else 25))
			painter.setBrush(QColor(69, 69, 69) if dark else Qt.GlobalColor.white)
			painter.drawEllipse(QPointF(x, middle), middle - 1, middle - 1)
			painter.setPen(Qt.PenStyle.NoPen)
			painter.setBrush(accent)
			radius = 4 if grip == self.dragging else 6 if grip == self.hovered and enabled else 5
			painter.drawEllipse(QPointF(x, middle), radius, radius)

	def mousePressEvent(self, event) -> None:
		if event.button() != Qt.MouseButton.LeftButton:
			return super().mousePressEvent(event)
		x = event.position().x()
		self.active = self.dragging = self.grip_at(x)
		if abs(x - self.grip_x(self.values()[self.dragging])) > self.HANDLE / 2:
			self.move_grip(self.dragging, self.value_at(x))
		self.update()

	def mouseMoveEvent(self, event) -> None:
		x = event.position().x()
		if self.dragging >= 0:
			self.move_grip(self.dragging, self.value_at(x))
			return
		grip = self.grip_at(x)
		hovered = grip if abs(x - self.grip_x(self.values()[grip])) <= self.HANDLE / 2 else -1
		if hovered != self.hovered:
			self.hovered = hovered
			self.update()

	def mouseReleaseEvent(self, _) -> None:
		self.dragging = -1
		self.update()

	def leaveEvent(self, event) -> None:
		self.hovered = -1
		self.update()
		super().leaveEvent(event)

	def keyPressEvent(self, event) -> None:
		steps = { Qt.Key.Key_Left: -1, Qt.Key.Key_Down: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: 1, Qt.Key.Key_PageDown: -10, Qt.Key.Key_PageUp: 10 }
		if event.key() in steps:
			self.move_grip(self.active, self.values()[self.active] + steps.get(event.key()))
		else:
			super().keyPressEvent(event)

	def wheelEvent(self, event) -> None:
		step = 1 if event.angleDelta().y() > 0 else -1 if event.angleDelta().y() < 0 else 0
		self.move_grip(self.active, self.values()[self.active] + step)
		event.accept()


class ResetButton(TransparentToolButton):
	"""tool button with the classic open circle arrow"""

	def __init__(self, parent : Optional[QWidget] = None) -> None:
		# the icon overload of the base constructor calls self.__init__ again, so set it separately
		super().__init__(parent)
		self.setIcon(FluentIcon.SYNC)
		self.setFixedSize(30, 30)
		self.setIconSize(QSize(14, 14))

	def _drawIcon(self, icon, painter : QPainter, rect : QRectF, state = None) -> None:
		color = QColor(255, 255, 255, 220) if isDarkTheme() else QColor(0, 0, 0, 200)
		painter.setPen(QPen(color, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
		painter.setBrush(Qt.BrushStyle.NoBrush)
		circle = rect.adjusted(1.5, 1.5, -1.5, -1.5)
		# counter clockwise arc that starts at the top and leaves a gap at the upper left
		painter.drawArc(circle, 90 * 16, -300 * 16)
		painter.setPen(Qt.PenStyle.NoPen)
		painter.setBrush(color)
		tip = QPointF(circle.center().x(), circle.top())
		painter.drawPolygon(QPolygonF([ tip + QPointF(-4.2, 0), tip + QPointF(0.8, -3.6), tip + QPointF(0.8, 3.6) ]))


def star_path(rect : QRectF) -> QPainterPath:
	radius = min(rect.width(), rect.height()) / 2
	# a star's points reach higher than its body goes low, nudge it down so it looks centred
	center = rect.center() + QPointF(0, radius * 0.08)
	points = []
	for index in range(10):
		angle = math.radians(-90 + index * 36)
		length = radius if index % 2 == 0 else radius * 0.45
		points.append(center + QPointF(math.cos(angle) * length, math.sin(angle) * length))
	path = QPainterPath()
	path.addPolygon(QPolygonF(points))
	path.closeSubpath()
	return path


class StarIcon(FluentIconBase):
	"""star outline in the text color, drawn at paint time so it follows theme changes like the other fluent icons"""

	def path(self, theme = Theme.AUTO) -> str:
		return ''

	def render(self, painter : QPainter, rect, theme = Theme.AUTO, indexes = None, **attributes) -> None:
		painter.save()
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		color = QColor(255, 255, 255) if isDarkTheme() else QColor(0, 0, 0)
		painter.setPen(QPen(color, 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
		painter.setBrush(Qt.BrushStyle.NoBrush)
		painter.drawPath(star_path(QRectF(rect).adjusted(1, 1, -1, -1)))
		painter.restore()

	def icon(self, theme = Theme.AUTO, color : Optional[QColor] = None) -> QIcon:
		pixmap = QPixmap(32, 32)
		pixmap.fill(Qt.GlobalColor.transparent)
		painter = QPainter(pixmap)
		self.render(painter, QRectF(0, 0, 32, 32))
		painter.end()
		return QIcon(pixmap)


def star_icon() -> StarIcon:
	return StarIcon()


class StarButton(TransparentToolButton):
	"""tool button with a star outline, filled in the theme color when on"""

	def __init__(self, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.on = False
		self.setFixedSize(30, 30)
		self.setIconSize(QSize(15, 15))

	def set_on(self, on : bool) -> None:
		self.on = on
		self.update()

	def _drawIcon(self, icon, painter : QPainter, rect : QRectF, state = None) -> None:
		path = star_path(rect.adjusted(0.5, 0.5, -0.5, -0.5))
		if self.on:
			painter.fillPath(path, themeColor())
		else:
			color = QColor(255, 255, 255, 200) if isDarkTheme() else QColor(0, 0, 0, 170)
			painter.setPen(QPen(color, 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
			painter.setBrush(Qt.BrushStyle.NoBrush)
			painter.drawPath(path)


class InfoButton(TransparentToolButton):
	"""faint (i) that shows a description as a tooltip on hover and as a flyout on click"""

	def __init__(self, text : str, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.text = text
		self.setIcon(FluentIcon.INFO)
		self.setFixedSize(20, 20)
		self.setIconSize(QSize(12, 12))
		self.setCursor(Qt.CursorShape.WhatsThisCursor)
		# fluent tooltips do not wrap, so break long descriptions into lines
		self.setToolTip(textwrap.fill(text, 64))
		self.installEventFilter(ToolTipFilter(self, 300))
		self.clicked.connect(self.show_flyout)

	def show_flyout(self) -> None:
		Flyout.create('', textwrap.fill(self.text, 64), target = self, parent = self.window(), aniType = FlyoutAnimationType.PULL_UP)

	def _drawIcon(self, icon, painter : QPainter, rect : QRectF, state = None) -> None:
		painter.setOpacity(1.0 if self.isHover else 0.55)
		super()._drawIcon(icon, painter, rect)


def gender_path(rect : QRectF, gender : str) -> QPainterPath:
	"""outline of the male (circle with an arrow) or female (circle with a cross) sign"""
	width, height = rect.width(), rect.height()
	path = QPainterPath()
	if gender == 'male':
		radius = width * 0.27
		center = QPointF(rect.left() + width * 0.4, rect.top() + height * 0.6)
		path.addEllipse(center, radius, radius)
		tip = QPointF(rect.left() + width * 0.92, rect.top() + height * 0.08)
		path.moveTo(center + QPointF(radius * 0.71, -radius * 0.71))
		path.lineTo(tip)
		path.moveTo(tip + QPointF(-width * 0.3, 0))
		path.lineTo(tip)
		path.lineTo(tip + QPointF(0, height * 0.3))
	else:
		radius = width * 0.28
		center = QPointF(rect.center().x(), rect.top() + height * 0.34)
		path.addEllipse(center, radius, radius)
		path.moveTo(center.x(), center.y() + radius)
		path.lineTo(center.x(), rect.bottom())
		path.moveTo(center.x() - width * 0.2, rect.bottom() - height * 0.2)
		path.lineTo(center.x() + width * 0.2, rect.bottom() - height * 0.2)
	return path


class ChoiceButton(TogglePushButton):
	"""one button of a row of choices, with a fluent icon or a drawn male / female sign"""

	def __init__(self, text : str, symbol : Optional[str] = None, icon : Optional[FluentIcon] = None, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.symbol = symbol
		self.setText(text)
		# any icon makes the button reserve room for one, a sign is painted over it below, a button without either shows only its text
		if icon or symbol:
			self.setIcon(icon or FluentIcon.PIN)

	def _drawIcon(self, icon, painter : QPainter, rect : QRectF, state = None) -> None:
		if not self.symbol:
			super()._drawIcon(icon, painter, rect)
			return
		if self.isChecked():
			color = QColor(0, 0, 0) if isDarkTheme() else QColor(255, 255, 255)
		else:
			color = QColor(255, 255, 255) if isDarkTheme() else QColor(0, 0, 0)
		painter.setPen(QPen(color, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
		painter.setBrush(Qt.BrushStyle.NoBrush)
		painter.drawPath(gender_path(rect.adjusted(1, 1, -1, -1), self.symbol))


# each click on the Landmarks button moves to the next mode
LANDMARK_MODES = [ 'off', '5', '68' ]


def landmarks_mode(value) -> str:
	"""the Landmarks mode from the config, it was on or off before it had a choice of points"""
	if value is True:
		return '68'
	return value if value in LANDMARK_MODES else 'off'


class IconCountButton(TogglePushButton):
	"""a toggle button with an icon and an optional short text after it, as narrow as icon_width while it has no text"""

	# no __init__ of its own, the fluent button constructor calls itself again with other arguments
	icon_width = 0

	def setText(self, text : str) -> None:
		super().setText(text)
		# the icon padding of a push button leaves a gap where the text would be
		if not text and self.icon_width:
			self.setFixedWidth(self.icon_width)
		else:
			self.setMinimumWidth(0)
			self.setMaximumWidth(16777215)

	def _drawIcon(self, icon, painter : QPainter, rect : QRectF, state = None) -> None:
		if not self.text():
			rect = QRectF((self.width() - rect.width()) / 2, rect.y(), rect.width(), rect.height())
		super()._drawIcon(icon, painter, rect)


def attach_landmarks_cycle(button, mode : str, on_change : Callable[[str], None], text : str = '') -> None:
	"""makes a toggle button step through off, the 5 key points and all 68 points, lit while points are drawn and labelled with their number"""
	def show(mode : str) -> None:
		button.landmarks_mode = mode
		button.setChecked(mode != 'off')
		button.setText(text if mode == 'off' else ('%s %s' % (text, mode)).strip())

	def step() -> None:
		mode = LANDMARK_MODES[(LANDMARK_MODES.index(button.landmarks_mode) + 1) % len(LANDMARK_MODES)]
		show(mode)
		on_change(mode)

	show(mode)
	button.clicked.connect(step)


def add_edit_button(edit : QLineEdit, icon : FluentIcon, tip : str) -> LineEditButton:
	"""puts an icon button inside a fluent line edit at the far right, after its clear button"""
	button = LineEditButton(icon, edit)
	button.setFixedWidth(29)
	button.setIconSize(QSize(12, 12))
	button.setToolTip(tip)
	button.installEventFilter(ToolTipFilter(button))
	edit.rightButtons.append(button)
	edit.hBoxLayout.addWidget(button, 0, Qt.AlignmentFlag.AlignRight)
	edit._adjustTextMargins()
	return button


class SplitterHandle(QSplitterHandle):
	def __init__(self, orientation : Qt.Orientation, parent : QSplitter) -> None:
		super().__init__(orientation, parent)
		self.hovered = False
		self.setCursor(Qt.CursorShape.SplitHCursor if orientation == Qt.Orientation.Horizontal else Qt.CursorShape.SplitVCursor)

	def enterEvent(self, event) -> None:
		self.hovered = True
		self.update()

	def leaveEvent(self, event) -> None:
		self.hovered = False
		self.update()

	def paintEvent(self, event) -> None:
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		painter.setPen(Qt.PenStyle.NoPen)
		if self.hovered or self.isSliderDown():
			painter.setBrush(themeColor())
			length = 64
		else:
			painter.setBrush(QColor(255, 255, 255, 50) if isDarkTheme() else QColor(0, 0, 0, 45))
			length = 40
		if self.orientation() == Qt.Orientation.Horizontal:
			rect = QRectF((self.width() - 4) / 2, (self.height() - length) / 2, 4, length)
		else:
			rect = QRectF((self.width() - length) / 2, (self.height() - 4) / 2, length, 4)
		painter.drawRoundedRect(rect, 2, 2)

	def isSliderDown(self) -> bool:
		return bool(self.property('dragging'))

	def mousePressEvent(self, event) -> None:
		self.setProperty('dragging', True)
		super().mousePressEvent(event)

	def mouseReleaseEvent(self, event) -> None:
		self.setProperty('dragging', False)
		self.update()
		super().mouseReleaseEvent(event)

	def mouseDoubleClickEvent(self, event) -> None:
		if event.button() == Qt.MouseButton.LeftButton:
			self.splitter().reset_sizes()


class Splitter(QSplitter):
	"""splitter with a slim grip that lights up on hover, a double click on the grip puts back default_sizes"""
	reset = Signal()

	def __init__(self, orientation : Qt.Orientation, parent : Optional[QWidget] = None) -> None:
		super().__init__(orientation, parent)
		self.setHandleWidth(16)
		self.setChildrenCollapsible(False)
		# the sizes for the room there is, none leaves a double click doing nothing
		self.default_sizes : Optional[Callable[[int], List[int]]] = None

	def reset_sizes(self) -> None:
		if self.default_sizes:
			self.setSizes(self.default_sizes(sum(self.sizes())))
			self.reset.emit()

	def createHandle(self) -> QSplitterHandle:
		return SplitterHandle(self.orientation(), self)


def collect_drop_paths(event : QDropEvent) -> List[str]:
	paths = []
	for url in event.mimeData().urls():
		if url.isLocalFile():
			paths.append(os.path.normpath(url.toLocalFile()))
	return paths


class CollapsibleCard(SimpleCardWidget):
	toggled = Signal(bool)

	def __init__(self, title : str, subtitle : str = '', icon : Optional[FluentIcon] = None, expanded : bool = True, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.setBorderRadius(8)
		outer = QVBoxLayout(self)
		outer.setContentsMargins(16, 10, 12, 10)
		outer.setSpacing(4)

		self.header = QWidget(self)
		self.header.setCursor(Qt.CursorShape.PointingHandCursor)
		header_layout = QHBoxLayout(self.header)
		header_layout.setContentsMargins(0, 0, 0, 0)
		header_layout.setSpacing(10)
		if icon:
			icon_widget = IconWidget(icon, self.header)
			icon_widget.setFixedSize(16, 16)
			header_layout.addWidget(icon_widget)
		self.title_label = StrongBodyLabel(title, self.header)
		header_layout.addWidget(self.title_label)
		self.subtitle_label = CaptionLabel(subtitle, self.header)
		self.subtitle_label.setTextColor('#707070', '#9a9a9a')
		header_layout.addWidget(self.subtitle_label)
		header_layout.addStretch(1)
		self.header_tools = QHBoxLayout()
		self.header_tools.setSpacing(4)
		header_layout.addLayout(self.header_tools)
		self.chevron = TransparentToolButton(FluentIcon.CHEVRON_DOWN_MED, self.header)
		self.chevron.setFixedSize(28, 28)
		self.chevron.setIconSize(QSize(10, 10))
		self.chevron.clicked.connect(self.toggle)
		header_layout.addWidget(self.chevron)
		outer.addWidget(self.header)

		self.body = QWidget(self)
		self.body_layout = QVBoxLayout(self.body)
		self.body_layout.setContentsMargins(0, 4, 4, 2)
		self.body_layout.setSpacing(0)
		outer.addWidget(self.body)
		self.set_expanded(expanded)

	def mouseReleaseEvent(self, event) -> None:
		if self.header.geometry().contains(event.position().toPoint()):
			self.toggle()
		super().mouseReleaseEvent(event)

	def toggle(self) -> None:
		self.set_expanded(not self.body.isVisible())

	def set_expanded(self, expanded : bool) -> None:
		self.body.setVisible(expanded)
		self.chevron.setIcon(FluentIcon.CHEVRON_DOWN_MED if expanded else FluentIcon.CHEVRON_RIGHT_MED)
		self.toggled.emit(expanded)

	def add_widget(self, widget : QWidget) -> None:
		self.body_layout.addWidget(widget)

	def add_tool(self, widget : QWidget) -> None:
		self.header_tools.addWidget(widget)

	def set_subtitle(self, text : str) -> None:
		self.subtitle_label.setText(text)


GRIP_RADIUS = 15


class PreviewView(QWidget):
	"""scales a pixmap into the available space, shows a hint and accepts dropped files

	with a compare pixmap set it becomes a before / after view split by a draggable divider
	"""
	files_dropped = Signal(list)
	clicked = Signal()
	# a right click on a face box: its index in faces and the global position for a menu
	face_menu_requested = Signal(int, QPoint)

	def __init__(self, hint : str = '', parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.pixmap = QPixmap()
		self.compare_pixmap = QPixmap()
		self.compare_labels = ('', '')
		# the divider starts at the far left so the whole result shows, drag it right to reveal the target
		self.split = 0.0
		self.dragging = False
		self.selected : Optional[bool] = None
		self.scaled_cache = {}
		self.hint = hint
		self.badge = ''
		# a red border around the picture, when face detection found no face in it
		self.warning = False
		# detected faces drawn over the image: (box in frame pixels, tag, selected) and optionally a color for the box,
		# plus the frame size the boxes belong to
		self.faces : List[tuple] = []
		self.faces_size = (0, 0)
		# the face mask as a tinted image at frame size, plus its outline in frame pixels
		self.mask_image = QImage()
		self.mask_outline : List[QPolygonF] = []
		# the facial landmarks per face in frame pixels: (68 points, 5 key points, estimated)
		self.landmarks : List[tuple] = []
		self.drag_active = False
		# modifier keys held during the last click, so a click handler can tell ctrl / shift clicks apart
		self.click_modifiers = Qt.KeyboardModifier.NoModifier
		self.setAcceptDrops(True)
		self.setMinimumHeight(180)
		self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
		self.setCursor(Qt.CursorShape.PointingHandCursor)

	def set_pixmap(self, pixmap : QPixmap) -> None:
		self.pixmap = pixmap
		self.compare_pixmap = QPixmap()
		self.setCursor(Qt.CursorShape.PointingHandCursor)
		self.update()

	def set_compare(self, before : QPixmap, after : QPixmap, labels : tuple = ('Target', 'Result')) -> None:
		self.pixmap = after
		self.compare_pixmap = before
		self.compare_labels = labels
		self.setCursor(Qt.CursorShape.SplitHCursor)
		self.update()

	def is_comparing(self) -> bool:
		return not self.pixmap.isNull() and not self.compare_pixmap.isNull()

	def set_badge(self, text : str) -> None:
		self.badge = text
		self.update()

	def set_warning(self, warning : bool) -> None:
		if warning != self.warning:
			self.warning = warning
			self.update()

	def set_faces(self, faces : List[tuple], frame_size : Tuple[int, int] = (0, 0), mask = None, landmarks : Optional[List[tuple]] = None) -> None:
		"""boxes for the detected faces, the face mask as a 0-255 array at frame size and the facial landmarks, all cleared by an empty call"""
		self.faces = faces
		self.faces_size = frame_size
		self.landmarks = landmarks or []
		self.mask_image = QImage()
		self.mask_outline = []
		if mask is not None and mask.any():
			color = themeColor()
			overlay = numpy.zeros(mask.shape[:2] + (4,), numpy.uint8)
			overlay[..., 0], overlay[..., 1], overlay[..., 2] = color.blue(), color.green(), color.red()
			overlay[..., 3] = (mask.astype(numpy.float32) * 0.5).astype(numpy.uint8)
			self.mask_image = QImage(overlay.data, overlay.shape[1], overlay.shape[0], overlay.strides[0], QImage.Format.Format_ARGB32).copy()
			contours, _ = cv2.findContours((mask > 127).astype(numpy.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
			self.mask_outline = [ QPolygonF([ QPointF(float(x), float(y)) for x, y in contour.reshape(-1, 2) ]) for contour in contours ]
			self.faces_size = (mask.shape[1], mask.shape[0])
		self.update()

	def set_selected(self, selected : Optional[bool]) -> None:
		self.selected = selected
		self.update()

	def image_rect(self) -> QRectF:
		if self.pixmap.isNull():
			return QRectF(self.rect())
		size = self.pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
		return QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2, size.width(), size.height())

	def scaled(self, key : str, pixmap : QPixmap, size : QSize) -> QPixmap:
		# rescaling full size frames on every mouse move makes the divider lag
		cached = self.scaled_cache.get(key)
		if cached and cached[0] == pixmap.cacheKey() and cached[1] == size:
			return cached[2]
		result = pixmap.scaled(size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
		self.scaled_cache[key] = (pixmap.cacheKey(), size, result)
		return result

	def split_x(self, image_rect : QRectF) -> float:
		# the grip is centred on the line, so the line stops where the whole grip still fits in the view
		margin = GRIP_RADIUS + 2
		x = min(max(image_rect.x() + image_rect.width() * self.split, image_rect.left()), image_rect.right())
		return min(max(x, margin), self.width() - margin)

	def move_split(self, x : float) -> None:
		rect = self.image_rect()
		if rect.width() > 0:
			self.split = min(1.0, max(0.0, (x - rect.x()) / rect.width()))
			self.update()

	def face_rect(self, box : List[float], image_rect : QRectF) -> QRectF:
		scale = image_rect.width() / self.faces_size[0]
		return QRectF(image_rect.x() + box[0] * scale, image_rect.y() + box[1] * scale, (box[2] - box[0]) * scale, (box[3] - box[1]) * scale)

	def face_at(self, position : QPointF) -> int:
		"""the face box under a point, the smallest one where boxes overlap, -1 for none"""
		if not self.faces or not self.faces_size[0] or self.pixmap.isNull() or self.is_comparing():
			return -1
		image_rect = self.image_rect()
		hits = [ (rect.width() * rect.height(), index) for index, rect in ((index, self.face_rect(face[0], image_rect)) for index, face in enumerate(self.faces)) if rect.contains(position) ]
		return min(hits)[1] if hits else -1

	def contextMenuEvent(self, event) -> None:
		index = self.face_at(QPointF(event.pos()))
		if index >= 0:
			self.face_menu_requested.emit(index, event.globalPos())

	def mousePressEvent(self, event) -> None:
		if self.is_comparing() and event.button() == Qt.MouseButton.LeftButton:
			self.dragging = True
			self.move_split(event.position().x())

	def mouseMoveEvent(self, event) -> None:
		if self.dragging:
			self.move_split(event.position().x())

	def mouseDoubleClickEvent(self, event) -> None:
		if self.is_comparing():
			self.split = 0.0 if self.split > 0.0 else 0.5
			self.update()

	def mouseReleaseEvent(self, event) -> None:
		if self.dragging:
			self.dragging = False
			return
		if event.button() == Qt.MouseButton.LeftButton:
			self.click_modifiers = event.modifiers()
			self.clicked.emit()

	def dragEnterEvent(self, event : QDragEnterEvent) -> None:
		if event.mimeData().hasUrls():
			self.drag_active = True
			self.update()
			event.acceptProposedAction()

	def dragLeaveEvent(self, event) -> None:
		self.drag_active = False
		self.update()

	def dropEvent(self, event : QDropEvent) -> None:
		self.drag_active = False
		self.update()
		paths = collect_drop_paths(event)
		if paths:
			self.files_dropped.emit(paths)

	def paintEvent(self, event) -> None:
		painter = QPainter(self)
		painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
		dark = isDarkTheme()
		rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
		path = QPainterPath()
		path.addRoundedRect(rect, 8, 8)
		painter.fillPath(path, QColor(20, 20, 20) if dark else QColor(243, 243, 243))

		if not self.pixmap.isNull():
			image_rect = self.image_rect()
			size = image_rect.size().toSize()
			painter.save()
			painter.setClipPath(path)
			if self.is_comparing():
				split_x = self.split_x(image_rect)
				painter.drawPixmap(image_rect.topLeft(), self.scaled('before', self.compare_pixmap, size))
				painter.setClipRect(QRectF(split_x, image_rect.y(), image_rect.right() - split_x, image_rect.height()), Qt.ClipOperation.IntersectClip)
				painter.drawPixmap(image_rect.topLeft(), self.scaled('after', self.pixmap, size))
				painter.restore()
				self.paint_overlays(painter, image_rect)
				self.paint_divider(painter, image_rect, split_x)
			else:
				painter.drawPixmap(image_rect.topLeft(), self.scaled('after', self.pixmap, size))
				painter.restore()
				self.paint_overlays(painter, image_rect)
		else:
			pen = QPen(QColor(255, 255, 255, 60) if dark else QColor(0, 0, 0, 50), 1.5, Qt.PenStyle.DashLine)
			painter.setPen(pen)
			painter.drawRoundedRect(rect.adjusted(6, 6, -6, -6), 6, 6)
			painter.setPen(QColor(255, 255, 255, 150) if dark else QColor(0, 0, 0, 130))
			font = QFont(self.font())
			font.setPointSizeF(10)
			painter.setFont(font)
			painter.drawText(rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.hint)

		if self.badge and not self.is_comparing():
			self.paint_badge(painter, QPointF(10, 10), self.badge)

		if self.selected is False:
			painter.fillPath(path, QColor(0, 0, 0, 150) if dark else QColor(255, 255, 255, 170))
		elif self.selected:
			painter.setPen(QPen(themeColor(), 3))
			painter.setBrush(Qt.BrushStyle.NoBrush)
			painter.drawRoundedRect(rect.adjusted(1.5, 1.5, -1.5, -1.5), 7, 7)
			self.paint_check(painter, QPointF(14, self.height() - 14))

		if self.warning and not self.pixmap.isNull():
			painter.setPen(QPen(QColor(248, 113, 113) if dark else QColor(220, 38, 38), 3))
			painter.setBrush(Qt.BrushStyle.NoBrush)
			painter.drawRoundedRect(rect.adjusted(1.5, 1.5, -1.5, -1.5), 7, 7)

		if self.drag_active:
			painter.setPen(QPen(themeColor(), 2))
			painter.setBrush(Qt.BrushStyle.NoBrush)
			painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 8, 8)

	def paint_badge(self, painter : QPainter, position : QPointF, text : str, align_right : bool = False) -> None:
		font = QFont(self.font())
		font.setPointSizeF(8.5)
		painter.setFont(font)
		metrics = painter.fontMetrics()
		width = metrics.horizontalAdvance(text) + 16
		badge_rect = QRectF(position.x() - width if align_right else position.x(), position.y(), width, metrics.height() + 6)
		badge_path = QPainterPath()
		badge_path.addRoundedRect(badge_rect, 4, 4)
		painter.fillPath(badge_path, QColor(0, 0, 0, 150))
		painter.setPen(QColor(255, 255, 255))
		painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, text)

	def paint_overlays(self, painter : QPainter, image_rect : QRectF) -> None:
		if not self.mask_image.isNull():
			self.paint_mask(painter, image_rect)
		if self.landmarks and self.faces_size[0]:
			self.paint_landmarks(painter, image_rect)
		if self.faces and self.faces_size[0]:
			self.paint_faces(painter, image_rect)

	def paint_landmarks(self, painter : QPainter, image_rect : QRectF) -> None:
		# green like the Face Debugger, orange when the aligner was unsure and the 68 points are only estimated
		scale = image_rect.width() / self.faces_size[0]
		painter.save()
		painter.setPen(QPen(QColor(0, 0, 0, 140), 0.8))
		for points, key_points, estimated in self.landmarks:
			color = QColor(255, 165, 0) if estimated else QColor(0, 230, 90)
			for point_list, radius, fill in [ (points, 1.8, color), (key_points, 3.0, QColor(0, 220, 255)) ]:
				painter.setBrush(fill)
				for x, y in point_list:
					painter.drawEllipse(QPointF(image_rect.x() + x * scale, image_rect.y() + y * scale), radius, radius)
		painter.restore()

	def paint_mask(self, painter : QPainter, image_rect : QRectF) -> None:
		# the tint fades with the mask blur, the line marks where the mask is half strength
		painter.drawImage(image_rect, self.mask_image)
		scale = image_rect.width() / self.mask_image.width()
		painter.save()
		painter.translate(image_rect.topLeft())
		painter.scale(scale, scale)
		pen = QPen(themeColor().lighter(130), 1.5)
		pen.setCosmetic(True)
		painter.setPen(pen)
		painter.setBrush(Qt.BrushStyle.NoBrush)
		for polygon in self.mask_outline:
			painter.drawPolygon(polygon)
		painter.restore()

	def paint_faces(self, painter : QPainter, image_rect : QRectF) -> None:
		# faces that will be processed get the accent color (or their person's color), skipped ones a dashed outline
		font = QFont(self.font())
		font.setPointSizeF(8)
		painter.setFont(font)
		metrics = painter.fontMetrics()
		for face in self.faces:
			box, tag, selected = face[:3]
			person_color = face[3] if len(face) > 3 else None
			# a face mapping person's name, shown under the box
			name = face[4] if len(face) > 4 else ''
			rect = self.face_rect(box, image_rect)
			color = (person_color or themeColor()) if selected else (person_color or QColor(220, 220, 220))
			painter.setPen(QPen(color, 2.5 if selected else 1.5, Qt.PenStyle.SolidLine if selected else Qt.PenStyle.DashLine))
			painter.setBrush(Qt.BrushStyle.NoBrush)
			painter.drawRoundedRect(rect, 4, 4)
			for text, below in [ (tag, False), (name, True) ]:
				if not text:
					continue
				# the tag sits above the box and the name under it, each inside the box when the box touches that edge
				tag_rect = QRectF(rect.x(), 0, metrics.horizontalAdvance(text) + 12, metrics.height() + 4)
				if below:
					tag_rect.moveTop(rect.bottom() + 2)
					if tag_rect.bottom() > image_rect.bottom():
						tag_rect.moveBottom(rect.bottom() - 3)
				else:
					tag_rect.moveBottom(rect.top() - 2)
					if tag_rect.top() < image_rect.top():
						tag_rect.moveTop(rect.top() + 3)
				tag_rect.moveLeft(min(max(tag_rect.left(), image_rect.left()), image_rect.right() - tag_rect.width()))
				painter.setPen(Qt.PenStyle.NoPen)
				painter.setBrush(color if selected else QColor(0, 0, 0, 170))
				painter.drawRoundedRect(tag_rect, 3, 3)
				painter.setPen(QColor(255, 255, 255))
				painter.drawText(tag_rect, Qt.AlignmentFlag.AlignCenter, text)

	def paint_divider(self, painter : QPainter, image_rect : QRectF, split_x : float) -> None:
		painter.setPen(QPen(QColor(255, 255, 255, 230), 2))
		painter.drawLine(QPointF(split_x, image_rect.top()), QPointF(split_x, image_rect.bottom()))
		center = QPointF(split_x, image_rect.center().y())
		painter.setPen(QPen(QColor(0, 0, 0, 60), 1))
		painter.setBrush(QColor(255, 255, 255))
		painter.drawEllipse(center, GRIP_RADIUS, GRIP_RADIUS)
		painter.setPen(Qt.PenStyle.NoPen)
		painter.setBrush(QColor(40, 40, 40))
		for direction in (-1, 1):
			tip = center + QPointF(direction * 10, 0)
			base = center + QPointF(direction * 4, 0)
			painter.drawPolygon(QPolygonF([ tip, base + QPointF(0, -5), base + QPointF(0, 5) ]))
		before_label, after_label = self.compare_labels
		top = image_rect.top() + 10
		if before_label and split_x - image_rect.left() > 80:
			self.paint_badge(painter, QPointF(image_rect.left() + 10, top), before_label)
		if after_label and image_rect.right() - split_x > 80:
			self.paint_badge(painter, QPointF(image_rect.right() - 10, top), after_label, align_right = True)

	def paint_check(self, painter : QPainter, center : QPointF) -> None:
		painter.setPen(Qt.PenStyle.NoPen)
		painter.setBrush(themeColor())
		painter.drawEllipse(center, 9, 9)
		painter.setPen(QPen(QColor(255, 255, 255), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
		painter.setBrush(Qt.BrushStyle.NoBrush)
		painter.drawPolyline(QPolygonF([ center + QPointF(-4, 0), center + QPointF(-1, 3), center + QPointF(4, -3) ]))


class DriveLabel(CaptionLabel):
	"""free and total space of the drive an output folder is on, orange once it is getting full"""

	def __init__(self, folder : Callable[[], str], parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.folder = folder
		self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
		self.installEventFilter(ToolTipFilter(self, 300))
		# files saved by a run or by other programs change it, so it is read again now and then
		self.timer = QTimer(self)
		self.timer.setInterval(5000)
		self.timer.timeout.connect(self.refresh)

	def refresh(self) -> None:
		folder = self.folder()
		drive = describe_drive(folder) if folder else None
		self.setText(drive_text(folder) if drive else '')
		self.setToolTip('Space left on the drive the results are saved to' + ('\nGetting full, a long video may not fit. Free some space or choose an output folder on another drive.' if drive and drive.get('warn') else '') if drive else '')
		if drive and drive.get('warn'):
			self.setTextColor('#b45309', '#f0b35a')
		else:
			self.setTextColor('#707070', '#9a9a9a')

	def showEvent(self, event) -> None:
		super().showEvent(event)
		self.refresh()
		self.timer.start()

	def hideEvent(self, event) -> None:
		super().hideEvent(event)
		self.timer.stop()


class NameEdit(QLineEdit):
	"""small line edit for renaming in place, escape cancels"""
	cancelled = Signal()

	def __init__(self, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		accent = themeColor().name()
		background, text = ('#2b2b2b', '#ffffff') if isDarkTheme() else ('#ffffff', '#000000')
		self.setStyleSheet('QLineEdit { background: %s; color: %s; border: 1px solid %s; border-radius: 3px; padding: 0 3px; font-size: 12px; selection-background-color: %s; }' % (background, text, accent, accent))

	def keyPressEvent(self, event) -> None:
		if event.key() == Qt.Key.Key_Escape:
			self.cancelled.emit()
			return
		super().keyPressEvent(event)


class Thumbnail(QFrame):
	"""source thumbnail with its name underneath, clicking the name renames it"""
	removed = Signal(str)
	clicked = Signal(str, object)
	renamed = Signal(str, str)

	IMAGE_SIZE = 104
	NAME_HEIGHT = 20

	def __init__(self, path : str, pixmap : QPixmap, selected : Optional[bool] = None, parent : Optional[QWidget] = None, name : str = '') -> None:
		super().__init__(parent)
		self.path = path
		self.file_name = os.path.splitext(os.path.basename(path))[0]
		self.name = name or self.file_name
		self.editor : Optional[NameEdit] = None
		size = self.IMAGE_SIZE
		self.setFixedSize(size, size + self.NAME_HEIGHT)
		self.preview = PreviewView(os.path.basename(path), self)
		self.preview.setMinimumHeight(0)
		self.preview.setGeometry(0, 0, size, size)
		self.preview.set_pixmap(pixmap)
		self.preview.setAcceptDrops(False)
		self.preview.clicked.connect(lambda: self.clicked.emit(self.path, self.preview.click_modifiers))
		self.preview.installEventFilter(ToolTipFilter(self.preview))
		self.name_label = CaptionLabel('', self)
		self.name_label.setGeometry(2, size + 3, size - 4, self.NAME_HEIGHT - 3)
		self.name_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
		self.name_label.setCursor(Qt.CursorShape.IBeamCursor)
		self.name_label.installEventFilter(self)
		self.name_label.installEventFilter(ToolTipFilter(self.name_label))
		self.set_name(self.name)
		self.remove_button = TransparentToolButton(FluentIcon.CLOSE, self)
		self.remove_button.setFixedSize(22, 22)
		self.remove_button.setIconSize(QSize(9, 9))
		self.remove_button.move(size - 24, 2)
		self.remove_button.setStyleSheet('TransparentToolButton { background: rgba(0,0,0,120); border-radius: 11px; } TransparentToolButton:hover { background: rgba(196,43,28,220); }')
		self.remove_button.clicked.connect(lambda: self.removed.emit(self.path))
		self.set_selected(selected)

	def set_name(self, name : str) -> None:
		self.name = name or self.file_name
		self.name_label.setText(QFontMetrics(self.name_label.font()).elidedText(self.name, Qt.TextElideMode.ElideRight, self.IMAGE_SIZE - 4))
		renamed = '\nRenamed from %s' % os.path.basename(self.path) if self.name != self.file_name else ''
		self.name_label.setToolTip('%s%s\nClick to rename. {source} in the output file name uses this name.' % (self.name, renamed))

	def eventFilter(self, watched : QObject, event : QEvent) -> bool:
		if watched is self.name_label and event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
			self.start_rename()
			return True
		return super().eventFilter(watched, event)

	def start_rename(self) -> None:
		if self.editor:
			return
		self.editor = NameEdit(self)
		self.editor.setText(self.name)
		self.editor.selectAll()
		self.editor.setGeometry(0, self.IMAGE_SIZE + 1, self.IMAGE_SIZE, self.NAME_HEIGHT - 1)
		self.editor.editingFinished.connect(self.finish_rename)
		self.editor.cancelled.connect(self.close_editor)
		self.editor.show()
		self.editor.setFocus()
		self.name_label.hide()

	def finish_rename(self) -> None:
		if not self.editor:
			return
		# an empty name goes back to the file name
		name = self.editor.text().strip() or self.file_name
		self.close_editor()
		if name != self.name:
			self.set_name(name)
			self.renamed.emit(self.path, '' if name == self.file_name else name)

	def close_editor(self) -> None:
		editor, self.editor = self.editor, None
		if editor:
			# hiding takes the focus away, which finishes the edit a second time, so self.editor is cleared first
			editor.hide()
			editor.deleteLater()
		self.name_label.show()

	def set_selected(self, selected : Optional[bool]) -> None:
		self.preview.set_selected(selected)
		alpha = 110 if selected is False else 228
		self.name_label.setTextColor(QColor(0, 0, 0, alpha), QColor(255, 255, 255, alpha))
		if selected is None:
			self.preview.setToolTip(self.path)
		else:
			state = 'Used as a source.' if selected else 'Not used.'
			self.preview.setToolTip('%s\n%s\nClick to use only this one, Ctrl+click to add or remove it, Shift+click to pick a range.' % (self.path, state))


class DisabledToolTipFilter(ToolTipFilter):
	"""a tooltip that also shows while its button is off, so it can say what turns the button on"""

	def _canShowToolTip(self) -> bool:
		parent = self.parent()
		return parent.isWidgetType() and bool(parent.toolTip())


def round_pixmap(pixmap : QPixmap, size : int, ratio : int = 2) -> QPixmap:
	"""the picture cut to a circle of size, drawn at twice the pixels so it stays sharp"""
	result = QPixmap(size * ratio, size * ratio)
	result.fill(Qt.GlobalColor.transparent)
	if pixmap.isNull():
		return result
	scaled = pixmap.scaled(size * ratio, size * ratio, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
	painter = QPainter(result)
	painter.setRenderHint(QPainter.RenderHint.Antialiasing)
	path = QPainterPath()
	path.addEllipse(QRectF(0, 0, size * ratio, size * ratio))
	painter.setClipPath(path)
	painter.drawPixmap((size * ratio - scaled.width()) // 2, (size * ratio - scaled.height()) // 2, scaled)
	painter.end()
	result.setDevicePixelRatio(ratio)
	return result


FACE_ICON = 22
FACE_ICON_OVERLAP = 7
FACE_ICON_SLOTS = 5
# every face icon is as wide as five overlapping faces, like the group pills, so the names after them line up
FACE_ICON_SIZE = QSize(FACE_ICON + (FACE_ICON_SLOTS - 1) * (FACE_ICON - FACE_ICON_OVERLAP), FACE_ICON)


def faces_icon(pixmaps : List[QPixmap]) -> QIcon:
	"""up to five round faces overlapping, like the group pills"""
	ratio = 2
	result = QPixmap(FACE_ICON_SIZE * ratio)
	result.fill(Qt.GlobalColor.transparent)
	painter = QPainter(result)
	painter.setRenderHint(QPainter.RenderHint.Antialiasing)
	# a ring in the menu color keeps the overlapping faces apart
	ring = QColor(43, 43, 43) if isDarkTheme() else QColor(249, 249, 249)
	pixmaps = pixmaps[:FACE_ICON_SLOTS]
	# centered in the room of five, so one face sits in the middle of the column
	offset = (FACE_ICON_SLOTS - len(pixmaps)) * (FACE_ICON - FACE_ICON_OVERLAP) * ratio // 2
	for index, pixmap in enumerate(pixmaps):
		left = offset + index * (FACE_ICON - FACE_ICON_OVERLAP) * ratio
		painter.drawPixmap(left, 0, round_pixmap(pixmap, FACE_ICON * ratio, 1))
		painter.setBrush(Qt.BrushStyle.NoBrush)
		painter.setPen(QPen(ring, 2 * ratio))
		painter.drawEllipse(QRectF(left, 0, FACE_ICON * ratio, FACE_ICON * ratio).adjusted(-1, -1, 1, 1))
	painter.end()
	result.setDevicePixelRatio(ratio)
	return QIcon(result)


def symbol_icon(icon : FluentIconBase) -> QIcon:
	"""a small Fluent icon in the room of a face icon"""
	ratio = 2
	result = QPixmap(FACE_ICON_SIZE * ratio)
	result.fill(Qt.GlobalColor.transparent)
	painter = QPainter(result)
	painter.setRenderHint(QPainter.RenderHint.Antialiasing)
	icon.render(painter, QRectF((FACE_ICON_SIZE.width() - 16) / 2 * ratio, 3 * ratio, 16 * ratio, 16 * ratio))
	painter.end()
	result.setDevicePixelRatio(ratio)
	return QIcon(result)


class FaceComboBox(ComboBox):
	"""a dropdown with a face icon before each name, in the box and in its list"""

	def _createComboMenu(self):
		menu = super()._createComboMenu()
		menu.view.setIconSize(FACE_ICON_SIZE)
		return menu

	def paintEvent(self, event) -> None:
		# the button without its text, then the icon and name from the left, then the arrow
		painter = QStylePainter(self)
		option = QStyleOptionButton()
		self.initStyleOption(option)
		option.text = ''
		option.icon = QIcon()
		painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
		painter.end()
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		if not self.isEnabled():
			painter.setOpacity(0.4)
		left = 10
		index = self.currentIndex()
		if index >= 0:
			self.itemIcon(index).paint(painter, QRect(left, (self.height() - FACE_ICON) // 2, FACE_ICON_SIZE.width(), FACE_ICON), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
			text_left = left + FACE_ICON_SIZE.width() + 8
			text_width = self.width() - text_left - 32
			painter.setPen(QColor(255, 255, 255) if isDarkTheme() else QColor(0, 0, 0))
			painter.setFont(self.font())
			painter.drawText(QRectF(text_left, 0, text_width, self.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.fontMetrics().elidedText(self.itemText(index), Qt.TextElideMode.ElideRight, text_width))
		if self.isHover:
			painter.setOpacity(0.8)
		elif self.isPressed:
			painter.setOpacity(0.7)
		rect = QRectF(self.width() - 22, self.height() / 2 - 5 + self.arrowAni.y, 10, 10)
		if isDarkTheme():
			FluentIcon.ARROW_DOWN.render(painter, rect)
		else:
			FluentIcon.ARROW_DOWN.render(painter, rect, fill = '#646464')


class GroupChip(QWidget):
	"""a group of source faces as a pill: small faces, the name and how many, clicking the name renames it
	and clicking elsewhere uses the faces of the group"""
	clicked = Signal(int)
	renamed = Signal(int, str)
	removed = Signal(int)

	AVATAR = 24
	OVERLAP = 6
	MAX_AVATARS = 5
	HEIGHT = 32

	def __init__(self, group_id : int, name : str, pixmaps : List[QPixmap], parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.group_id = group_id
		self.name = name
		self.count = len(pixmaps)
		self.current = False
		self.hovered = False
		self.editor : Optional[NameEdit] = None
		self.avatars = [ self.round_pixmap(pixmap) for pixmap in pixmaps[:self.MAX_AVATARS] ]
		self.setFixedHeight(self.HEIGHT)
		self.setCursor(Qt.CursorShape.PointingHandCursor)
		layout = QHBoxLayout(self)
		avatars_width = len(self.avatars) * (self.AVATAR - self.OVERLAP) + self.OVERLAP
		layout.setContentsMargins(5 + avatars_width + 8, 0, 4, 0)
		layout.setSpacing(6)
		self.name_label = StrongBodyLabel(name, self)
		self.name_label.setCursor(Qt.CursorShape.IBeamCursor)
		self.name_label.setToolTip('%s\nClick to rename' % name)
		self.name_label.installEventFilter(self)
		self.name_label.installEventFilter(ToolTipFilter(self.name_label))
		layout.addWidget(self.name_label)
		count_label = CaptionLabel('%d faces' % self.count, self)
		count_label.setTextColor('#707070', '#9a9a9a')
		layout.addWidget(count_label)
		self.remove_button = TransparentToolButton(FluentIcon.CLOSE, self)
		self.remove_button.setFixedSize(22, 22)
		self.remove_button.setIconSize(QSize(9, 9))
		self.remove_button.setToolTip('Ungroup, the faces stay')
		self.remove_button.installEventFilter(ToolTipFilter(self.remove_button))
		self.remove_button.clicked.connect(lambda: self.removed.emit(self.group_id))
		layout.addWidget(self.remove_button)
		self.setToolTip('Click to use the faces of %s' % name)
		self.installEventFilter(ToolTipFilter(self))

	def round_pixmap(self, pixmap : QPixmap) -> QPixmap:
		return round_pixmap(pixmap, self.AVATAR)

	def set_current(self, current : bool) -> None:
		"""outlined in the accent color while exactly its faces are in use"""
		self.current = current
		self.update()

	def paintEvent(self, event) -> None:
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		dark = isDarkTheme()
		shade = 255 if dark else 0
		painter.setBrush(QColor(shade, shade, shade, 22 if self.hovered else 10))
		painter.setPen(QPen(themeColor(), 1.5) if self.current else QPen(QColor(shade, shade, shade, 45), 1))
		painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), self.HEIGHT / 2 - 1, self.HEIGHT / 2 - 1)
		ring = QColor(43, 43, 43) if dark else QColor(255, 255, 255)
		top = (self.height() - self.AVATAR) / 2
		for index, avatar in enumerate(self.avatars):
			left = 5 + index * (self.AVATAR - self.OVERLAP)
			painter.drawPixmap(int(left), int(top), avatar)
			# a ring in the card color keeps the overlapping faces apart
			painter.setBrush(Qt.BrushStyle.NoBrush)
			painter.setPen(QPen(ring, 2))
			painter.drawEllipse(QRectF(left, top, self.AVATAR, self.AVATAR).adjusted(-1, -1, 1, 1))

	def enterEvent(self, event) -> None:
		self.hovered = True
		self.update()

	def leaveEvent(self, event) -> None:
		self.hovered = False
		self.update()

	def mouseReleaseEvent(self, event) -> None:
		if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
			self.clicked.emit(self.group_id)

	def eventFilter(self, watched : QObject, event : QEvent) -> bool:
		if watched is self.name_label and event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
			self.start_rename()
			return True
		return super().eventFilter(watched, event)

	def start_rename(self) -> None:
		if self.editor:
			return
		self.editor = NameEdit(self)
		self.editor.setText(self.name)
		self.editor.selectAll()
		geometry = self.name_label.geometry()
		self.editor.setGeometry(geometry.x() - 4, (self.height() - 22) // 2, max(geometry.width() + 8, 90), 22)
		self.editor.editingFinished.connect(self.finish_rename)
		self.editor.cancelled.connect(self.close_editor)
		self.editor.show()
		self.editor.setFocus()

	def finish_rename(self) -> None:
		if not self.editor:
			return
		# an empty name keeps the old one
		name = self.editor.text().strip() or self.name
		self.close_editor()
		if name != self.name:
			self.renamed.emit(self.group_id, name)

	def close_editor(self) -> None:
		editor, self.editor = self.editor, None
		if editor:
			# hiding takes the focus away, which finishes the edit a second time, so self.editor is cleared first
			editor.hide()
			editor.deleteLater()


PROCESSOR_MIME = 'application/x-ffstudio-processor'


class ProcessorButton(QPushButton):
	"""square toggle for one processor, shows its run position when on and can be dragged to reorder"""

	BADGE_WIDTH = 24

	def __init__(self, name : str, text : str, parent : Optional[QWidget] = None) -> None:
		super().__init__(text, parent)
		self.name = name
		self.position = 0
		self.hovered = False
		self.press_pos = None
		self.setCheckable(True)
		self.setCursor(Qt.CursorShape.PointingHandCursor)

	def sizeHint(self) -> QSize:
		return QSize(self.fontMetrics().horizontalAdvance(self.text()) + self.BADGE_WIDTH + 22, 32)

	def set_position(self, position : int) -> None:
		self.position = position
		self.update()

	def enterEvent(self, event) -> None:
		self.hovered = True
		self.update()

	def leaveEvent(self, event) -> None:
		self.hovered = False
		self.update()

	def mousePressEvent(self, event) -> None:
		if event.button() == Qt.MouseButton.LeftButton:
			self.press_pos = event.position().toPoint()
		super().mousePressEvent(event)

	def mouseMoveEvent(self, event) -> None:
		if self.press_pos is not None and (event.position().toPoint() - self.press_pos).manhattanLength() >= QApplication.startDragDistance():
			hot_spot = self.press_pos
			self.press_pos = None
			# a drag is not a click, so the button must not toggle when it ends
			self.setDown(False)
			mime = QMimeData()
			mime.setData(PROCESSOR_MIME, self.name.encode())
			drag = QDrag(self)
			drag.setMimeData(mime)
			drag.setPixmap(self.grab())
			drag.setHotSpot(hot_spot)
			drag.exec(Qt.DropAction.MoveAction)
			self.hovered = False
			self.update()
			return
		super().mouseMoveEvent(event)

	def mouseReleaseEvent(self, event) -> None:
		self.press_pos = None
		super().mouseReleaseEvent(event)

	def paintEvent(self, event) -> None:
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		dark = isDarkTheme()
		rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
		checked = self.isChecked()
		if checked:
			fill = QColor(themeColor())
			if self.isDown():
				fill.setAlpha(200)
			elif self.hovered:
				fill = fill.lighter(110) if dark else fill.darker(108)
			painter.setPen(Qt.PenStyle.NoPen)
			text_color = QColor(0, 0, 0) if dark else QColor(255, 255, 255)
		else:
			alpha = 30 if self.hovered else 15
			fill = QColor(255, 255, 255, alpha) if dark else QColor(0, 0, 0, alpha - 8)
			painter.setPen(QPen(QColor(255, 255, 255, 40) if dark else QColor(0, 0, 0, 40), 1))
			text_color = QColor(255, 255, 255) if dark else QColor(0, 0, 0)
		painter.setBrush(fill)
		painter.drawRect(rect)

		badge = QRectF(rect.left(), rect.top(), self.BADGE_WIDTH, rect.height())
		if checked and self.position:
			font = QFont(self.font())
			font.setBold(True)
			painter.setFont(font)
			painter.setPen(text_color)
			painter.drawText(badge.adjusted(4, 0, 0, 0), Qt.AlignmentFlag.AlignCenter, str(self.position))
			separator = QColor(text_color)
			separator.setAlpha(90)
			painter.setPen(QPen(separator, 1))
			painter.drawLine(QPointF(badge.right() + 2, rect.top() + 7), QPointF(badge.right() + 2, rect.bottom() - 7))
		else:
			# grip dots hint that the button can be dragged
			dots = QColor(text_color)
			dots.setAlpha(110)
			painter.setPen(Qt.PenStyle.NoPen)
			painter.setBrush(dots)
			for column in (0, 1):
				for row in (-1, 0, 1):
					painter.drawEllipse(QPointF(badge.center().x() + column * 4, badge.center().y() + row * 4), 1.1, 1.1)
		painter.setPen(text_color)
		painter.setFont(self.font())
		painter.drawText(rect.adjusted(self.BADGE_WIDTH + 8, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())


class ProcessorArea(QWidget):
	"""wrapping row of processor buttons that can be reordered by drag and drop"""
	reordered = Signal(list)

	def __init__(self, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.flow = FlowLayout(self, needAni = False)
		self.flow.setContentsMargins(0, 0, 0, 0)
		self.flow.setHorizontalSpacing(8)
		self.flow.setVerticalSpacing(8)
		self.buttons : List[ProcessorButton] = []
		self.drop_index = -1
		self.drop_line = QRect()
		self.setAcceptDrops(True)

	def set_buttons(self, buttons : List[ProcessorButton]) -> None:
		# removes without deleting, the same buttons come back in a new order after a drop
		self.flow.removeAllWidgets()
		self.buttons = buttons
		for button in buttons:
			self.flow.addWidget(button)
			button.show()
		self.flow.invalidate()

	def clear(self) -> None:
		self.flow.takeAllWidgets()
		self.buttons = []

	def locate(self, position) -> None:
		"""finds the insert index for a drop at position and where to draw the insert marker"""
		row = [ (index, button) for index, button in enumerate(self.buttons) if button.geometry().top() - 4 <= position.y() <= button.geometry().bottom() + 4 ]
		if not row:
			# between or below rows: use the last row above the pointer
			above = [ (index, button) for index, button in enumerate(self.buttons) if button.geometry().top() <= position.y() ]
			if above:
				row = [ item for item in above if item[1].geometry().top() == above[-1][1].geometry().top() ]
			else:
				row = [ (0, self.buttons[0]) ] if self.buttons else []
		self.drop_index, self.drop_line = len(self.buttons), QRect()
		for index, button in row:
			geometry = button.geometry()
			if position.x() < geometry.center().x():
				self.drop_index = index
				self.drop_line = QRect(geometry.left() - 5, geometry.top(), 2, geometry.height())
				return
		if row:
			index, button = row[-1]
			geometry = button.geometry()
			self.drop_index = index + 1
			self.drop_line = QRect(geometry.right() + 4, geometry.top(), 2, geometry.height())

	def dragEnterEvent(self, event) -> None:
		if event.mimeData().hasFormat(PROCESSOR_MIME):
			event.acceptProposedAction()

	def dragMoveEvent(self, event) -> None:
		if event.mimeData().hasFormat(PROCESSOR_MIME):
			self.locate(event.position().toPoint())
			self.update()
			event.acceptProposedAction()

	def dragLeaveEvent(self, event) -> None:
		self.drop_index = -1
		self.update()

	def dropEvent(self, event) -> None:
		name = bytes(event.mimeData().data(PROCESSOR_MIME).data()).decode()
		self.locate(event.position().toPoint())
		order = [ button.name for button in self.buttons ]
		index = self.drop_index
		self.drop_index = -1
		self.update()
		if name not in order:
			return
		if order.index(name) < index:
			index -= 1
		order.remove(name)
		order.insert(index, name)
		event.acceptProposedAction()
		self.reordered.emit(order)

	def paintEvent(self, event) -> None:
		if self.drop_index >= 0 and not self.drop_line.isNull():
			painter = QPainter(self)
			painter.fillRect(self.drop_line, themeColor())
