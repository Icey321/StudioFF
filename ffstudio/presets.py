from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import ComboBox, FluentIcon, LineEdit, MessageBox, MessageBoxBase, SubtitleLabel, ToolTipFilter, TransparentToolButton

from ffstudio.pages.setup import SYSTEM_GROUPS
from ffstudio.schema import same_value
from ffstudio.session import load_values

# config key of the presets, shared by every page: { name: { 'values': {...}, 'order': [...] } }
PRESETS_KEY = 'presets'
EDITED_SUFFIX = '  (edited)'


def own_values(store) -> Dict[str, Any]:
	"""the processors and options a preset keeps, the system settings of Setup stay as they are"""
	schema = store.schema
	return { dest: value for dest, value in store.values.items() if not schema or schema.options.get(dest, {}).get('group') not in SYSTEM_GROUPS }


def read_presets(config) -> Dict[str, Dict[str, Any]]:
	return dict(config.get(PRESETS_KEY) or {})


def preset_changes(store, preset : Dict[str, Any], order : List[str]) -> Tuple[Dict[str, Any], List[str]]:
	"""the options loading a preset would change and the processor order it brings, options the installed facefusion
	no longer has are left out"""
	schema = store.schema
	current = own_values(store)
	merged = dict(store.values)
	merged.update({ dest: value for dest, value in (preset.get('values') or {}).items() if dest in current })
	loaded = load_values(schema, merged)
	changes = { dest: loaded.get(dest) for dest in current if not same_value(loaded.get(dest), current.get(dest)) }
	installed = list(schema.processors) if schema else []
	preset_order = [ name for name in preset.get('order') or [] if name in installed ]
	return changes, preset_order + [ name for name in order if name not in preset_order ]


class PresetNameDialog(MessageBoxBase):
	"""asks for the name of a new preset"""

	def __init__(self, parent : QWidget) -> None:
		super().__init__(parent)
		self.viewLayout.addWidget(SubtitleLabel('Save Preset', self.widget))
		self.name_edit = LineEdit(self.widget)
		self.name_edit.setPlaceholderText('Preset name')
		self.name_edit.setClearButtonEnabled(True)
		self.name_edit.textChanged.connect(lambda text: self.yesButton.setEnabled(bool(text.strip())))
		self.name_edit.returnPressed.connect(lambda: self.yesButton.isEnabled() and self.yesButton.click())
		self.viewLayout.addWidget(self.name_edit)
		self.yesButton.setText('Save')
		self.yesButton.setEnabled(False)
		self.cancelButton.setText('Cancel')
		self.widget.setMinimumWidth(360)
		self.name_edit.setFocus()

	def name(self) -> str:
		return self.name_edit.text().strip()


class PresetBar(QWidget):
	"""a drop down of the saved presets with buttons to save, overwrite and delete them, the same list on every page"""

	def __init__(self, column, active_key : str, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.column = column
		self.store = column.store
		self.session = getattr(self.store, 'session', self.store)
		self.config = self.session.config
		# config key of the preset this page loaded or saved last
		self.active_key = active_key
		row = QHBoxLayout(self)
		row.setContentsMargins(0, 0, 0, 0)
		row.setSpacing(4)
		self.combo = ComboBox(self)
		self.combo.setPlaceholderText('Presets')
		self.combo.setToolTip('Load a saved set of processors and options. Setup\'s system settings stay as they are.')
		self.combo.installEventFilter(ToolTipFilter(self.combo))
		self.combo.activated.connect(self.on_activated)
		row.addWidget(self.combo, 1)
		self.save_button = self.tool_button(FluentIcon.SAVE_AS, 'Save the current processors and options as a new preset', self.save_new)
		self.overwrite_button = self.tool_button(FluentIcon.SAVE, 'Overwrite the selected preset with the current processors and options', self.overwrite)
		self.delete_button = self.tool_button(FluentIcon.DELETE, 'Delete the selected preset', self.delete)
		for button in [ self.save_button, self.overwrite_button, self.delete_button ]:
			row.addWidget(button)
		# a slider sends a change on every step, the comparison with the preset waits until it settles
		self.edited_timer = QTimer(self)
		self.edited_timer.setSingleShot(True)
		self.edited_timer.setInterval(200)
		self.edited_timer.timeout.connect(self.refresh_edited)
		self.session.presets_changed.connect(self.refresh)
		self.store.value_changed.connect(self.edited_timer.start)
		self.store.schema_changed.connect(self.edited_timer.start)
		self.refresh()

	def tool_button(self, icon : FluentIcon, tip : str, handler) -> TransparentToolButton:
		button = TransparentToolButton(icon, self)
		button.setToolTip(tip)
		button.installEventFilter(ToolTipFilter(button))
		button.clicked.connect(handler)
		return button

	def active(self) -> str:
		name = self.config.get(self.active_key) or ''
		return name if name in read_presets(self.config) else ''

	def set_active(self, name : str) -> None:
		self.config.set(self.active_key, name)
		self.refresh()

	def is_edited(self, preset : Dict[str, Any]) -> bool:
		changes, order = preset_changes(self.store, preset, self.column.processor_order())
		return bool(changes) or order != self.column.processor_order()

	def refresh(self) -> None:
		presets = read_presets(self.config)
		active = self.active()
		self.combo.blockSignals(True)
		self.combo.clear()
		for name in sorted(presets, key = str.lower):
			self.combo.addItem(name, userData = name)
		self.combo.setCurrentIndex(self.combo.findData(active) if active else -1)
		self.combo.blockSignals(False)
		self.combo.setEnabled(bool(presets))
		self.combo.setPlaceholderText('Presets' if presets else 'No presets yet')
		self.overwrite_button.setEnabled(bool(active))
		self.delete_button.setEnabled(bool(active))
		self.refresh_edited()

	def refresh_edited(self) -> None:
		active = self.active()
		index = self.combo.findData(active) if active else -1
		if index < 0:
			return
		edited = self.is_edited(read_presets(self.config).get(active) or {})
		self.combo.setItemText(index, active + (EDITED_SUFFIX if edited else ''))
		self.overwrite_button.setEnabled(edited)

	def saved_anywhere(self) -> bool:
		return any(not self.is_edited(preset) for preset in read_presets(self.config).values())

	def on_activated(self, index : int) -> None:
		name = self.combo.itemData(index)
		preset = read_presets(self.config).get(name)
		if preset is None:
			return
		if not self.saved_anywhere():
			box = MessageBox('Load "%s"?' % name, 'The processors and options you have now are not saved in a preset and are replaced by the ones of "%s". Setup\'s system settings stay as they are.' % name, self.window())
			box.yesButton.setText('Load Preset')
			box.cancelButton.setText('Cancel')
			if not box.exec():
				self.refresh()
				return
		self.load(name, preset)

	def load(self, name : str, preset : Dict[str, Any]) -> None:
		changes, order = preset_changes(self.store, preset, self.column.processor_order())
		self.config.set(self.active_key, name)
		self.config.set(self.store.ORDER_KEY, order)
		for dest, value in changes.items():
			self.store.set_value(dest, value)
		self.column.rebuild_processors()
		self.edited_timer.stop()
		self.refresh()
		self.session.notify.emit('success', 'Preset loaded', '"%s" is now used on this page.' % name)

	def store_preset(self, name : str) -> None:
		presets = read_presets(self.config)
		presets[name] = { 'values': own_values(self.store), 'order': self.column.processor_order() }
		self.config.set(PRESETS_KEY, presets)
		self.config.set(self.active_key, name)
		self.config.save()
		self.session.presets_changed.emit()

	def save_new(self) -> None:
		dialog = PresetNameDialog(self.window())
		if not dialog.exec() or not dialog.name():
			return
		name = dialog.name()
		if name in read_presets(self.config):
			box = MessageBox('Replace "%s"?' % name, 'A preset with this name already exists. Saving replaces it with the processors and options you have now, on both Studio and Batch.', self.window())
			box.yesButton.setText('Replace')
			box.cancelButton.setText('Cancel')
			if not box.exec():
				return
		self.store_preset(name)
		self.session.notify.emit('success', 'Preset saved', '"%s" can now be loaded on Studio and Batch.' % name)

	def overwrite(self) -> None:
		name = self.active()
		if not name:
			return
		box = MessageBox('Overwrite "%s"?' % name, 'The preset is replaced with the processors and options you have now, on both Studio and Batch.', self.window())
		box.yesButton.setText('Overwrite')
		box.cancelButton.setText('Cancel')
		if not box.exec():
			return
		self.store_preset(name)
		self.session.notify.emit('success', 'Preset saved', '"%s" now has the current processors and options.' % name)

	def delete(self) -> None:
		name = self.active()
		if not name:
			return
		box = MessageBox('Delete "%s"?' % name, 'The preset is removed from both Studio and Batch. The settings you have now stay as they are.', self.window())
		box.yesButton.setText('Delete')
		box.cancelButton.setText('Cancel')
		if not box.exec():
			return
		presets = read_presets(self.config)
		presets.pop(name, None)
		self.config.set(PRESETS_KEY, presets)
		self.config.set(self.active_key, '')
		self.config.save()
		self.session.presets_changed.emit()
