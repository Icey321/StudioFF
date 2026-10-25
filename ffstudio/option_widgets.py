from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QBoxLayout, QButtonGroup, QFrame, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, CheckBox, ComboBox, FlowLayout, FluentIcon, LineEdit, Slider, SpinBox, SwitchButton, ToolTipFilter

from ffstudio.schema import Schema, format_scalar, same_value
from ffstudio.widgets import ChoiceButton, DecimalBox, InfoButton, NumberBox, RangeSlider, ResetButton, StarButton, guard_wheel

AUTO_TEXT = 'Auto'
LABEL_WIDTH = 210
# options with a few short choices, shown as a row of buttons instead of a drop down
BUTTON_CHOICES = [ 'face_selector_gender', 'face_selector_mode', 'workflow_strategy' ]
# options facefusion only reads while a choice is ticked in another option, grayed out otherwise
REQUIRED_CHOICES =\
{
	'face_occluder_model': ('face_mask_types', 'occlusion'),
	'face_mask_areas': ('face_mask_types', 'area'),
	'face_mask_regions': ('face_mask_types', 'region'),
	'face_parser_model': ('face_mask_types', 'region')
}
# a lower and an upper option shown together as one slider with two grips, the row belongs to the lower one
PAIRED_OPTIONS =\
{
	'face_selector_age_start': ('face_selector_age_end', 'filter the detected faces based on their age, between the two grips')
}
PAIRED_PARTNERS = { partner: dest for dest, (partner, _) in PAIRED_OPTIONS.items() }


def pretty_choice(value : Any) -> str:
	return str(value)


class OptionControl(QWidget):
	changed = Signal(object)

	def __init__(self, option : Dict[str, Any], parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.option = option
		self.updating = False

	def value(self) -> Any:
		raise NotImplementedError

	def set_value(self, value : Any) -> None:
		raise NotImplementedError

	def set_option(self, option : Dict[str, Any]) -> None:
		self.option = option

	def emit_changed(self, *_ : Any) -> None:
		if not self.updating:
			self.changed.emit(self.value())


def choices_without_auto(option : Dict[str, Any]) -> List[Any]:
	# facefusion lists 'auto' as a choice of some options that default to nothing, both mean the same, so it shows once
	choices = option.get('choices') or []
	if option.get('default') is None:
		return [ choice for choice in choices if choice != 'auto' ]
	return choices


class ChoiceControl(OptionControl):
	def __init__(self, option : Dict[str, Any], parent : Optional[QWidget] = None) -> None:
		super().__init__(option, parent)
		layout = QHBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		self.combo = ComboBox(self)
		self.combo.setMinimumWidth(140)
		layout.addWidget(self.combo)
		self.combo.currentIndexChanged.connect(self.emit_changed)
		self.fill()

	def fill(self) -> None:
		self.updating = True
		self.combo.clear()
		self.items : List[Any] = []
		if self.option.get('default') is None:
			self.items.append(None)
			self.combo.addItem(AUTO_TEXT)
		for choice in choices_without_auto(self.option):
			self.items.append(choice)
			self.combo.addItem(pretty_choice(choice))
		self.updating = False

	def value(self) -> Any:
		index = self.combo.currentIndex()
		return self.items[index] if 0 <= index < len(self.items) else None

	def set_value(self, value : Any) -> None:
		self.updating = True
		matches = [ index for index, item in enumerate(self.items) if format_scalar(item) == format_scalar(value) ] if value is not None else [ 0 ]
		if not matches and self.option.get('default') in self.items:
			matches = [ self.items.index(self.option.get('default')) ]
		self.combo.setCurrentIndex(matches[0] if matches else 0)
		self.updating = False

	def set_option(self, option : Dict[str, Any]) -> None:
		current = self.value()
		super().set_option(option)
		self.fill()
		self.set_value(current if current in (option.get('choices') or []) else option.get('default'))
		self.emit_changed()


class ButtonChoiceControl(OptionControl):
	"""a row of buttons for short choices, the face selector gender ones with an icon each"""
	ICONS = { 'auto': ('Auto', None, FluentIcon.PEOPLE), 'male': ('Male', 'male', None), 'female': ('Female', 'female', None) }

	def __init__(self, option : Dict[str, Any], parent : Optional[QWidget] = None) -> None:
		super().__init__(option, parent)
		layout = QHBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(6)
		self.group = QButtonGroup(self)
		self.group.setExclusive(True)
		order = list(self.ICONS)
		choices = sorted(choices_without_auto(option), key = lambda choice: order.index(choice) if choice in order else len(order))
		self.items : List[Any] = ([ None ] if option.get('default') is None else []) + choices
		for index, item in enumerate(self.items):
			text, symbol, icon = self.ICONS.get(item or 'auto', (str(item).capitalize(), None, None))
			button = ChoiceButton(text, symbol, icon, self)
			button.setCheckable(True)
			self.group.addButton(button, index)
			layout.addWidget(button)
		layout.addStretch(1)
		self.group.idToggled.connect(lambda _, checked: checked and self.emit_changed())

	def value(self) -> Any:
		index = self.group.checkedId()
		return self.items[index] if 0 <= index < len(self.items) else None

	def set_value(self, value : Any) -> None:
		self.updating = True
		matches = [ index for index, item in enumerate(self.items) if format_scalar(item) == format_scalar(value) ] if value not in (None, 'auto') else [ 0 ]
		if not matches and self.option.get('default') in self.items:
			matches = [ self.items.index(self.option.get('default')) ]
		self.group.button(matches[0] if matches else 0).setChecked(True)
		self.updating = False


class MultiChoiceControl(OptionControl):
	def __init__(self, option : Dict[str, Any], parent : Optional[QWidget] = None) -> None:
		super().__init__(option, parent)
		self.flow = FlowLayout(self, needAni = False)
		self.flow.setContentsMargins(0, 0, 0, 0)
		self.flow.setHorizontalSpacing(16)
		self.flow.setVerticalSpacing(6)
		self.boxes : List[CheckBox] = []
		for choice in option.get('choices') or []:
			box = CheckBox(pretty_choice(choice), self)
			box.setProperty('choice', choice)
			box.stateChanged.connect(self.emit_changed)
			self.flow.addWidget(box)
			self.boxes.append(box)

	def value(self) -> Any:
		return [ box.property('choice') for box in self.boxes if box.isChecked() ]

	def set_value(self, value : Any) -> None:
		self.updating = True
		selected = [ format_scalar(item) for item in (value or []) ]
		for box in self.boxes:
			box.setChecked(format_scalar(box.property('choice')) in selected)
		self.updating = False


class RangeControl(OptionControl):
	def __init__(self, option : Dict[str, Any], parent : Optional[QWidget] = None) -> None:
		super().__init__(option, parent)
		layout = QHBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(10)
		value_range = option.get('range') or {}
		self.is_float = option.get('type') == 'float'
		self.minimum = value_range.get('min', 0)
		self.maximum = value_range.get('max', 100)
		self.step = value_range.get('step', 1) or 1
		self.optional = option.get('default') is None

		self.switch : Optional[SwitchButton] = None
		if self.optional:
			self.switch = SwitchButton(self)
			self.switch.setOnText('')
			self.switch.setOffText('')
			self.switch.checkedChanged.connect(self.on_switch)
			layout.addWidget(self.switch)

		self.slider = Slider(Qt.Orientation.Horizontal, self)
		self.slider.setRange(0, round((self.maximum - self.minimum) / self.step))
		self.slider.setMinimumWidth(80)
		self.slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
		layout.addWidget(self.slider, 1)

		if self.is_float:
			self.spin = DecimalBox(self)
			self.spin.setDecimals(2)
			self.spin.setSingleStep(self.step)
		else:
			self.spin = NumberBox(self)
			self.spin.setSingleStep(int(self.step))
		self.spin.setRange(self.minimum, self.maximum)
		self.spin.setFixedWidth(96)
		layout.addWidget(self.spin)

		self.slider.valueChanged.connect(self.on_slider)
		self.spin.valueChanged.connect(self.on_spin)
		guard_wheel(self.slider, self.spin)

	def on_slider(self, position : int) -> None:
		if not self.updating:
			self.updating = True
			self.spin.setValue(self.minimum + position * self.step)
			self.updating = False
			self.emit_changed()

	def on_spin(self, value : float) -> None:
		if not self.updating:
			self.updating = True
			self.slider.setValue(round((value - self.minimum) / self.step))
			self.updating = False
			self.emit_changed()

	def on_switch(self, checked : bool) -> None:
		self.slider.setEnabled(checked)
		self.spin.setEnabled(checked)
		self.emit_changed()

	def value(self) -> Any:
		if self.switch and not self.switch.isChecked():
			return None
		value = self.spin.value()
		return round(value, 4) if self.is_float else int(value)

	def set_value(self, value : Any) -> None:
		self.updating = True
		if self.switch:
			self.switch.setChecked(value is not None)
			self.slider.setEnabled(value is not None)
			self.spin.setEnabled(value is not None)
		if value is None:
			value = self.minimum
		self.spin.setValue(value)
		self.slider.setValue(round((float(value) - self.minimum) / self.step))
		self.updating = False


class PairedRangeControl(OptionControl):
	"""two options of the same range as one slider with two grips, its value is the pair (lower, upper)

	with optional options the switch turns both off together, a value missing on one side starts at its end of the range
	"""

	def __init__(self, option : Dict[str, Any], partner : Dict[str, Any], parent : Optional[QWidget] = None) -> None:
		super().__init__(option, parent)
		self.partner = partner
		layout = QHBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(10)
		value_range = option.get('range') or {}
		self.minimum = int(value_range.get('min', 0))
		self.maximum = int(value_range.get('max', 100))
		self.optional = option.get('default') is None or partner.get('default') is None

		self.switch : Optional[SwitchButton] = None
		if self.optional:
			self.switch = SwitchButton(self)
			self.switch.setOnText('')
			self.switch.setOffText('')
			self.switch.checkedChanged.connect(self.on_switch)
			layout.addWidget(self.switch)

		self.slider = RangeSlider(self)
		self.slider.set_range(self.minimum, self.maximum)
		layout.addWidget(self.slider, 1)
		self.spins : List[NumberBox] = []
		for _ in range(2):
			spin = NumberBox(self)
			spin.setRange(self.minimum, self.maximum)
			spin.setFixedWidth(84)
			spin.valueChanged.connect(self.on_spin)
			layout.addWidget(spin)
			self.spins.append(spin)
		self.slider.values_changed.connect(self.on_slider)
		guard_wheel(self.slider, *self.spins)

	def on_slider(self, low : int, high : int) -> None:
		if not self.updating:
			self.updating = True
			self.spins[0].setValue(low)
			self.spins[1].setValue(high)
			self.updating = False
			self.emit_changed()

	def on_spin(self, _ : int) -> None:
		if not self.updating:
			low, high = self.spins[0].value(), self.spins[1].value()
			# a box pushed past the other one takes the other one along
			if low > high:
				if self.sender() is self.spins[0]:
					high = low
				else:
					low = high
			self.show_values(low, high)
			self.emit_changed()

	def on_switch(self, checked : bool) -> None:
		self.set_controls_enabled(checked)
		self.emit_changed()

	def set_controls_enabled(self, enabled : bool) -> None:
		self.slider.setEnabled(enabled)
		for spin in self.spins:
			spin.setEnabled(enabled)

	def show_values(self, low : int, high : int) -> None:
		self.updating = True
		self.slider.set_values(low, high)
		self.spins[0].setValue(low)
		self.spins[1].setValue(high)
		self.updating = False

	def value(self) -> Any:
		if self.switch and not self.switch.isChecked():
			return (None, None)
		return (int(self.spins[0].value()), int(self.spins[1].value()))

	def set_value(self, value : Any) -> None:
		low, high = value if isinstance(value, (list, tuple)) else (value, None)
		active = low is not None or high is not None
		self.updating = True
		if self.switch:
			self.switch.setChecked(active)
			self.set_controls_enabled(active)
		self.updating = False
		low = self.minimum if low is None else int(low)
		high = self.maximum if high is None else int(high)
		self.show_values(min(low, high), max(low, high))


class MultiNumberControl(OptionControl):
	LABELS = { 'margin': [ 'Top', 'Right', 'Bottom', 'Left' ], 'padding': [ 'Top', 'Right', 'Bottom', 'Left' ], 'color': [ 'R', 'G', 'B', 'A' ] }

	def __init__(self, option : Dict[str, Any], parent : Optional[QWidget] = None) -> None:
		super().__init__(option, parent)
		# a flow layout so the boxes wrap onto a second line in a narrow panel
		flow = FlowLayout(self, needAni = False)
		flow.setContentsMargins(0, 0, 0, 0)
		flow.setHorizontalSpacing(10)
		flow.setVerticalSpacing(6)
		value_range = option.get('range') or { 'min': 0, 'max': 100000 }
		count = len(option.get('default') or [ 0, 0, 0, 0 ])
		labels = next((labels for key, labels in self.LABELS.items() if key in option.get('dest')), [ '' ] * count)
		self.spins : List[SpinBox] = []
		for index in range(count):
			cell = QWidget(self)
			cell_layout = QHBoxLayout(cell)
			cell_layout.setContentsMargins(0, 0, 0, 0)
			cell_layout.setSpacing(6)
			label = labels[index % len(labels)]
			if label:
				caption = CaptionLabel(label, cell)
				caption.setFixedWidth(44 if len(label) > 1 else 10)
				cell_layout.addWidget(caption)
			spin = NumberBox(cell)
			spin.setRange(int(value_range.get('min')), int(value_range.get('max')))
			spin.setFixedWidth(84)
			spin.valueChanged.connect(self.emit_changed)
			guard_wheel(spin)
			cell_layout.addWidget(spin)
			flow.addWidget(cell)
			self.spins.append(spin)

	def value(self) -> Any:
		return [ spin.value() for spin in self.spins ]

	def set_value(self, value : Any) -> None:
		self.updating = True
		values = list(value or [])
		for index, spin in enumerate(self.spins):
			spin.setValue(int(values[index]) if index < len(values) else (int(values[-1]) if values else 0))
		self.updating = False


class IntegerControl(OptionControl):
	def __init__(self, option : Dict[str, Any], parent : Optional[QWidget] = None) -> None:
		super().__init__(option, parent)
		layout = QHBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		self.optional = option.get('default') is None
		self.spin = SpinBox(self)
		self.spin.setRange(-1 if self.optional else 0, 10000000)
		if self.optional:
			self.spin.setSpecialValueText(AUTO_TEXT)
		self.spin.setFixedWidth(160)
		self.spin.valueChanged.connect(self.emit_changed)
		guard_wheel(self.spin)
		layout.addWidget(self.spin)
		layout.addStretch(1)

	def value(self) -> Any:
		value = self.spin.value()
		return None if self.optional and value < 0 else value

	def set_value(self, value : Any) -> None:
		self.updating = True
		self.spin.setValue(-1 if value is None else int(value))
		self.updating = False


class TextControl(OptionControl):
	def __init__(self, option : Dict[str, Any], parent : Optional[QWidget] = None) -> None:
		super().__init__(option, parent)
		layout = QHBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		self.edit = LineEdit(self)
		self.edit.setPlaceholderText(AUTO_TEXT if option.get('default') is None else format_scalar(option.get('default')))
		self.edit.setClearButtonEnabled(True)
		self.edit.setMinimumWidth(140)
		self.edit.editingFinished.connect(self.emit_changed)
		layout.addWidget(self.edit)

	def value(self) -> Any:
		text = self.edit.text().strip()
		if not text:
			return None
		if self.option.get('multiple'):
			return text.split()
		return text

	def set_value(self, value : Any) -> None:
		self.updating = True
		if value is None:
			self.edit.clear()
		elif isinstance(value, list):
			self.edit.setText(' '.join(format_scalar(item) for item in value))
		else:
			self.edit.setText(format_scalar(value))
		self.updating = False


def create_control(option : Dict[str, Any], parent : Optional[QWidget] = None) -> OptionControl:
	multiple = option.get('multiple')
	if option.get('choices') and multiple:
		return MultiChoiceControl(option, parent)
	if option.get('dest') in BUTTON_CHOICES:
		return ButtonChoiceControl(option, parent)
	if option.get('choices'):
		return ChoiceControl(option, parent)
	if option.get('range') and multiple:
		return MultiNumberControl(option, parent)
	if option.get('range'):
		return RangeControl(option, parent)
	if option.get('type') == 'int' and not multiple:
		return IntegerControl(option, parent)
	return TextControl(option, parent)


class OptionRow(QFrame):
	"""label + help + control + reset, bound to a shared values dict

	puts the label above the control when the panel is too narrow for both side by side.
	with info_icon set the help hides behind an (i) next to the label instead of showing under it.
	with on_favorite set it gets a star in front of the label that shows on hover, or always once the option is a favorite.
	with short_label set the label loses its group words (Face selector gender becomes Gender, Reference face distance
	becomes Distance) so most labels fit on one line, for rows inside their group's box.
	with group_caption set those words also show in small capitals underneath, for the favorites box where groups mix
	"""

	def __init__(self, schema : Schema, option : Dict[str, Any], values : Dict[str, Any], on_change : Callable[[str, Any], None], parent : Optional[QWidget] = None, on_favorite : Optional[Callable[[str], None]] = None, context : str = '', info_icon : bool = False, short_label : bool = False, group_caption : bool = False) -> None:
		super().__init__(parent)
		self.schema = schema
		self.dest = option.get('dest')
		# the upper option of a pair shown on this row, Age start carries Age end
		self.partner, paired_help = PAIRED_OPTIONS.get(self.dest, (None, ''))
		self.values = values
		self.on_change = on_change
		self.applying = False
		# the group name in front of the label, for rows shown outside their own group
		label = schema.option_label(option)
		self.title = context + ' · ' + label if context and not label.lower().startswith(context.lower()) else label
		caption = ''
		if group_caption:
			self.title, caption = schema.split_label(option)
			caption = caption or context
		elif short_label:
			self.title = schema.split_label(option)[0]
		if self.partner and self.title.endswith(' start'):
			self.title = self.title[:-len(' start')]
		self.favorite = False
		self.hovered = False

		self.layout_ = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
		self.layout_.setContentsMargins(0, 6, 0, 6)
		self.layout_.setSpacing(12)

		self.text_widget = QWidget(self)
		text_layout = QVBoxLayout(self.text_widget)
		text_layout.setContentsMargins(0, 0, 0, 0)
		text_layout.setSpacing(0)
		# title line: favorite star, label, then the (i) that holds the description when info_icon is set
		title_layout = QHBoxLayout()
		title_layout.setContentsMargins(0, 0, 0, 0)
		title_layout.setSpacing(2)
		self.star_button : Optional[StarButton] = None
		if on_favorite:
			self.star_button = StarButton(self.text_widget)
			self.star_button.setFixedSize(22, 22)
			self.star_button.setIconSize(QSize(13, 13))
			self.star_button.clicked.connect(lambda: on_favorite(self.dest))
			self.star_button.installEventFilter(ToolTipFilter(self.star_button))
			policy = self.star_button.sizePolicy()
			policy.setRetainSizeWhenHidden(True)
			self.star_button.setSizePolicy(policy)
			title_layout.addWidget(self.star_button, 0, Qt.AlignmentFlag.AlignTop)
		self.label = BodyLabel(self.title, self.text_widget)
		self.label.setWordWrap(True)
		title_layout.addWidget(self.label, 0, Qt.AlignmentFlag.AlignVCenter)
		self.help_text = paired_help or option.get('help', '').split(' (choices')[0]
		self.help : Optional[CaptionLabel] = None
		if info_icon:
			if self.help_text:
				title_layout.addWidget(InfoButton(self.help_text, self.text_widget), 0, Qt.AlignmentFlag.AlignTop)
		title_layout.addStretch(1)
		text_layout.addLayout(title_layout)
		if caption:
			self.caption = CaptionLabel(caption.upper(), self.text_widget)
			font = self.caption.font()
			font.setPixelSize(10)
			font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
			self.caption.setFont(font)
			self.caption.setTextColor('#8a8a8a', '#8a8a8a')
			# lined up under the label, past the star
			self.caption.setContentsMargins(24 if self.star_button else 0, 0, 0, 0)
			text_layout.addWidget(self.caption)
		if not info_icon:
			self.help = CaptionLabel(self.help_text, self.text_widget)
			self.help.setWordWrap(True)
			self.help.setTextColor('#606060', '#a0a0a0')
			text_layout.addWidget(self.help)
		self.text_widget.setFixedWidth(LABEL_WIDTH)
		self.layout_.addWidget(self.text_widget, 0, Qt.AlignmentFlag.AlignTop)

		self.control_area = QWidget(self)
		control_layout = QHBoxLayout(self.control_area)
		control_layout.setContentsMargins(0, 0, 0, 0)
		control_layout.setSpacing(8)
		resolved = schema.resolve(self.dest, values)
		if self.partner:
			self.control = PairedRangeControl(resolved, schema.resolve(self.partner, values), self.control_area)
		else:
			self.control = create_control(resolved, self.control_area)
		self.control.set_value(self.stored_value())
		self.control.changed.connect(self.on_control_changed)
		control_layout.addWidget(self.control, 1)
		self.set_favorite(False)

		self.reset_button = ResetButton(self.control_area)
		self.reset_button.setToolTip('Reset to default')
		self.reset_button.installEventFilter(ToolTipFilter(self.reset_button))
		self.reset_button.clicked.connect(self.reset)
		policy = self.reset_button.sizePolicy()
		policy.setRetainSizeWhenHidden(True)
		self.reset_button.setSizePolicy(policy)
		control_layout.addWidget(self.reset_button, 0, Qt.AlignmentFlag.AlignVCenter)
		self.layout_.addWidget(self.control_area, 1)
		self.label_colors = (self.label.lightColor, self.label.darkColor)
		self.refresh_reset()
		self.refresh_available()

	def set_favorite(self, favorite : bool) -> None:
		self.favorite = favorite
		if self.star_button:
			self.star_button.set_on(favorite)
			self.star_button.setToolTip('Remove from Favorite Settings' if favorite else 'Add to Favorite Settings')
			self.star_button.setVisible(favorite or self.hovered)

	def enterEvent(self, event) -> None:
		self.hovered = True
		self.set_favorite(self.favorite)
		super().enterEvent(event)

	def leaveEvent(self, event) -> None:
		self.hovered = False
		self.set_favorite(self.favorite)
		super().leaveEvent(event)

	def minimumSizeHint(self) -> QSize:
		# report the stacked width, otherwise the side by side minimum forces the panel wider than the window
		hint = super().minimumSizeHint()
		return QSize(self.control_area.minimumSizeHint().width(), hint.height())

	def resizeEvent(self, event) -> None:
		super().resizeEvent(event)
		self.update_direction()

	def update_direction(self) -> None:
		wide = self.width() >= LABEL_WIDTH + 12 + self.control_area.minimumSizeHint().width()
		direction = QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom
		if self.layout_.direction() == direction:
			return
		self.layout_.setDirection(direction)
		if wide:
			self.text_widget.setFixedWidth(LABEL_WIDTH)
			self.layout_.setAlignment(self.text_widget, Qt.AlignmentFlag.AlignTop)
		else:
			self.text_widget.setMinimumWidth(0)
			self.text_widget.setMaximumWidth(16777215)
			self.layout_.setAlignment(self.text_widget, Qt.AlignmentFlag(0))
		self.layout_.setSpacing(12 if wide else 6)

	def dests(self) -> List[str]:
		return [ self.dest, self.partner ] if self.partner else [ self.dest ]

	def stored_value(self) -> Any:
		"""the value in the shared values the control shows, a pair for a paired row"""
		stored = [ self.values.get(dest, self.schema.resolve(dest, self.values).get('default')) for dest in self.dests() ]
		return tuple(stored) if self.partner else stored[0]

	def default_value(self) -> Any:
		defaults = [ self.schema.resolve(dest, self.values).get('default') for dest in self.dests() ]
		return tuple(defaults) if self.partner else defaults[0]

	def shows(self, value : Any) -> bool:
		shown = self.control.value()
		if self.partner:
			return all(same_value(left, right) for left, right in zip(shown, value))
		return same_value(shown, value)

	def sync(self) -> None:
		"""shows the shared value again after it was changed somewhere else"""
		if not self.applying and not self.shows(self.stored_value()):
			self.control.set_value(self.stored_value())
			self.refresh_reset()

	def matches(self, text : str) -> bool:
		words = []
		for dest in self.dests():
			option = self.schema.options.get(dest) or {}
			words += [ self.schema.option_label(option), dest.replace('_', ' ') ] + [ format_scalar(choice) for choice in option.get('choices') or [] ]
		haystack = ' '.join(words + [ self.title, self.help_text ]).lower()
		return all(word in haystack for word in text.split())

	def on_control_changed(self, value : Any) -> None:
		# the first of a pair goes in while the other one is still old, the row must not show that in between
		self.applying = True
		for dest, item in zip(self.dests(), value if self.partner else [ value ]):
			self.on_change(dest, item)
		self.applying = False
		self.refresh_reset()

	def reset(self) -> None:
		self.control.set_value(self.default_value())
		self.on_control_changed(self.control.value())

	def refresh_option(self) -> None:
		if self.partner:
			return
		self.control.set_option(self.schema.resolve(self.dest, self.values))

	def refresh_available(self) -> None:
		"""grays out the control while the choice it belongs to is not ticked, the label keeps its star and (i)"""
		required = REQUIRED_CHOICES.get(self.dest)
		if not required:
			return
		driver, choice = required
		ticked = self.values.get(driver, self.schema.resolve(driver, self.values).get('default')) or []
		available = choice in ticked
		self.control_area.setEnabled(available)
		self.label.setTextColor(*(self.label_colors if available else ('#a0a0a0', '#6a6a6a')))
		driver_label = self.schema.split_label(self.schema.options.get(driver) or {})[0]
		self.control_area.setToolTip('' if available else 'Only used while %s is ticked in %s' % (pretty_choice(choice), driver_label))

	def refresh_reset(self) -> None:
		self.reset_button.setVisible(not self.shows(self.default_value()))
		self.label.setText(self.title + ('  •' if self.reset_button.isVisible() else ''))
