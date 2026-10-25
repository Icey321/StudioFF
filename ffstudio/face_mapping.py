"""face mapping: the people found in a target, a name and a source for each, and the runs that swap them

facefusion swaps one identity per run (all sources are averaged into one face) into the faces the face selector
picks, and in reference mode it picks the faces that look like one face of the reference frame, on every frame.
so each mapped person gets a run of their own on the target, in reference mode with their best frame and
position, and compose_passes.py puts the changed faces of all runs together
"""
import hashlib
import math
import os
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ComboBox, FluentIcon, LineEdit, TogglePushButton, ToolTipFilter, TransparentToolButton, qconfig, themeColor

from ffstudio.config import DATA_DIR
from ffstudio.media import media_kind
from ffstudio.widgets import CollapsibleCard

PEOPLE_DIR = os.path.join(DATA_DIR, 'people')
# written by face_probe.py's people scan next to the crops, one embedding per person
PEOPLE_FILE = 'people.npy'
# a person's source: nothing, the sources picked in the Source card, or one source file
LEAVE = ''
SELECTED = '*'
# processors that work on the whole frame instead of the selected faces, they only run in the last pass
FRAME_PROCESSORS = [ 'background_remover', 'frame_colorizer', 'frame_enhancer' ]
# the processors whose work follows the face selector, without one of them there is nothing to map
FACE_PROCESSORS = [ 'age_modifier', 'deep_swapper', 'expression_restorer', 'face_debugger', 'face_editor', 'face_enhancer', 'face_swapper', 'lip_syncer' ]
# below this distance facefusion takes two people for the same one
SAME_PERSON_DISTANCE = 0.15
PERSON_COLORS = [ '#7F77DD', '#1D9E75', '#D85A30', '#D4537E', '#378ADD', '#BA7517', '#639922', '#E24B4A' ]


def person_color(index : int) -> QColor:
	return QColor(PERSON_COLORS[index % len(PERSON_COLORS)])


def people_dir(target_path : str) -> str:
	return os.path.join(PEOPLE_DIR, hashlib.sha1(target_path.encode('utf-8')).hexdigest()[:16])


def person_name(people : List[Dict[str, Any]], index : int) -> str:
	return people[index].get('name') or 'Face %d' % (index + 1)


def resolve_sources(choice : str, active_images : List[str], source_paths : List[str]) -> List[str]:
	if choice == SELECTED:
		return list(active_images)
	if choice and choice in source_paths and media_kind(choice) == 'image':
		return [ choice ]
	return []


def is_kept(people : List[Dict[str, Any]], index : Optional[int]) -> bool:
	"""a face's person, unless removed from the list, a removed person keeps their place so the others keep their index"""
	return index is not None and 0 <= index < len(people) and not people[index].get('removed')


def is_mapped(people : List[Dict[str, Any]]) -> bool:
	return any(person.get('source') for person in people)


def box_overlap(box : List[float], other : List[float]) -> float:
	"""intersection over union of two boxes"""
	width = min(box[2], other[2]) - max(box[0], other[0])
	height = min(box[3], other[3]) - max(box[1], other[1])
	if width <= 0 or height <= 0:
		return 0.0
	inter = width * height
	union = (box[2] - box[0]) * (box[3] - box[1]) + (other[2] - other[0]) * (other[3] - other[1]) - inter
	return inter / union if union > 0 else 0.0


def merge_people(old : List[Dict[str, Any]], new : List[Dict[str, Any]]) -> List[Dict[str, Any]]:
	"""a new scan keeps the names and sources of the people it finds again at the same place"""
	merged = []
	taken = set()
	for person in new:
		match = next((index for index, before in enumerate(old) if index not in taken and before.get('frame') == person.get('frame') and box_overlap(before.get('box'), person.get('box')) > 0.5), None)
		if match is not None:
			taken.add(match)
			person = dict(person, name = old[match].get('name', ''), source = old[match].get('source', LEAVE), removed = old[match].get('removed', False))
		merged.append(dict({ 'name': '', 'source': LEAVE }, **person))
	# with everyone removed before, a new scan brings them all back
	if all(person.get('removed') for person in merged):
		merged = [ dict(person, removed = False) for person in merged ]
	return merged


def pass_distance(person : Dict[str, Any], distance : float) -> float:
	"""the reference face distance for one person's pass, below the distance to the most similar other person"""
	nearest = person.get('nearest')
	if nearest is not None and nearest < distance:
		# facefusion takes the distance in steps of 0.05, rounding down keeps it below nearest
		return round(max(0.05, math.floor(nearest * 0.75 / 0.05 + 1e-6) * 0.05), 2)
	return distance


def plan_passes(people : List[Dict[str, Any]], active_images : List[str], source_paths : List[str], distance : float) -> List[Dict[str, Any]]:
	"""one run per mapped person, or a single run when every person gets the same sources"""
	mapped = []
	for index, person in enumerate(people):
		sources = resolve_sources(person.get('source') or LEAVE, active_images, source_paths)
		if sources:
			mapped.append((index, person, sources))
	if not mapped:
		return []
	if len(mapped) == len(people) and all(sources == mapped[0][2] for _, _, sources in mapped):
		return [ { 'many': True, 'people': [ index for index, _, _ in mapped ], 'sources': mapped[0][2], 'label': 'everyone' } ]
	return\
	[
		{
			'many': False,
			'person': index,
			'sources': sources,
			'frame': int(person.get('frame') or 0),
			'box': person.get('box'),
			'position': int(person.get('position') or 0),
			'distance': pass_distance(person, distance),
			'label': person_name(people, index)
		}
		for index, person, sources in mapped
	]


class PersonRow(QWidget):
	changed = Signal()
	# a new name changes no run, so it does not refresh the preview
	renamed = Signal()
	removed = Signal(int)

	def __init__(self, card : 'FaceMappingCard', index : int, person : Dict[str, Any]) -> None:
		super().__init__(card)
		self.card = card
		self.index = index
		self.person = person
		layout = QHBoxLayout(self)
		layout.setContentsMargins(0, 2, 0, 2)
		layout.setSpacing(8)
		self.remove_button = TransparentToolButton(FluentIcon.CLOSE.colored(QColor('#d13438'), QColor('#ff6b6b')), self)
		self.remove_button.setFixedSize(22, 22)
		self.remove_button.setIconSize(QSize(10, 10))
		self.remove_button.setToolTip('Remove %s from the list, their face is left unchanged' % person_name(card.people, index))
		self.remove_button.installEventFilter(ToolTipFilter(self.remove_button))
		self.remove_button.clicked.connect(lambda: self.removed.emit(self.index))
		layout.addWidget(self.remove_button)
		self.crop = QLabel(self)
		self.crop.setFixedSize(44, 44)
		self.crop.setAlignment(Qt.AlignmentFlag.AlignCenter)
		color = person_color(index).name()
		self.crop.setStyleSheet('QLabel { border: 2px solid %s; border-radius: 6px; color: %s; }' % (color, color))
		pixmap = QPixmap(person.get('crop') or '')
		if pixmap.isNull():
			self.crop.setText('%d' % (index + 1))
		else:
			self.crop.setPixmap(pixmap.scaled(40, 40, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation))
		layout.addWidget(self.crop)

		text = QVBoxLayout()
		text.setSpacing(0)
		self.name_edit = LineEdit(self)
		self.name_edit.setPlaceholderText('Face %d' % (index + 1))
		self.name_edit.setText(person.get('name') or '')
		self.name_edit.setFixedHeight(30)
		self.name_edit.editingFinished.connect(self.on_name_edited)
		text.addWidget(self.name_edit)
		self.info_label = CaptionLabel(self.describe(), self)
		self.info_label.setTextColor('#707070', '#9a9a9a')
		self.info_label.installEventFilter(ToolTipFilter(self.info_label))
		nearest = person.get('nearest')
		if nearest is not None and nearest < SAME_PERSON_DISTANCE:
			self.info_label.setTextColor('#b45309', '#f0b35a')
			self.info_label.setToolTip('FaceFusion sees this face as almost the same person as another one (distance %.2f), a swap of one may also change the other.' % nearest)
		text.addWidget(self.info_label)
		layout.addLayout(text, 1)

		arrow = QLabel('→', self)
		arrow.setStyleSheet('QLabel { color: #9a9a9a; }')
		layout.addWidget(arrow)
		self.source_combo = ComboBox(self)
		self.source_combo.setMinimumWidth(170)
		self.source_combo.setMaximumWidth(220)
		self.fill_sources()
		self.source_combo.currentIndexChanged.connect(self.on_source_picked)
		layout.addWidget(self.source_combo)

	def describe(self) -> str:
		samples = self.card.samples
		count = self.person.get('count') or 0
		age = self.person.get('age') or [ 0, 0 ]
		seen = ('in %d%% of frames · best at #%d' % (round(100 * count / samples), self.person.get('frame') or 0)) if samples > 1 else 'in the image'
		return '%s %d-%d · %s' % ((self.person.get('gender') or '?')[0].upper(), age[0], age[1], seen)

	def fill_sources(self) -> None:
		self.source_combo.blockSignals(True)
		self.source_combo.clear()
		choices = [ (LEAVE, 'Leave unchanged'), (SELECTED, 'Selected sources') ]
		choices += [ (path, self.card.page.source_name(path)) for path in self.card.page.source_paths if media_kind(path) == 'image' ]
		current = self.person.get('source') or LEAVE
		if current not in [ value for value, _ in choices ]:
			current = LEAVE
		for value, label in choices:
			self.source_combo.addItem(label, userData = value)
		self.source_combo.setCurrentIndex([ value for value, _ in choices ].index(current))
		self.source_combo.blockSignals(False)

	def on_name_edited(self) -> None:
		name = self.name_edit.text().strip()
		if name != (self.person.get('name') or ''):
			self.person['name'] = name
			self.renamed.emit()

	def on_source_picked(self, index : int) -> None:
		self.person['source'] = self.source_combo.itemData(index) or LEAVE
		self.changed.emit()

	def set_source(self, value : str) -> None:
		values = [ self.source_combo.itemData(index) or LEAVE for index in range(self.source_combo.count()) ]
		if value in values:
			self.source_combo.setCurrentIndex(values.index(value))


class FaceMappingCard(CollapsibleCard):
	"""finds the people in the target and lets each one get a name and a source of their own

	collapsed, the header still says who gets which source, so the preview keeps its room
	"""
	changed = Signal()
	renamed = Signal()

	def __init__(self, page : Any, find : Callable[[], None]) -> None:
		super().__init__('Face mapping', icon = FluentIcon.PEOPLE, expanded = page.session.config.get('face_mapping_expanded', True), parent = page)
		self.page = page
		self.people : List[Dict[str, Any]] = []
		self.samples = 0
		self.rows : List[PersonRow] = []
		self.finding = False
		# off keeps the people, their names and sources for when it is turned on again, Start runs as usual meanwhile
		self.enabled = True
		self.find = find
		self.status_label = self.subtitle_label
		self.status_label.setMinimumWidth(0)
		self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		# a long summary is clipped rather than pushing the buttons away, so it takes the free room itself
		self.header.layout().setStretchFactor(self.status_label, 1)
		self.refresh_button = TransparentToolButton(FluentIcon.SYNC, self)
		self.refresh_button.setToolTip('Look through the target for people again, the names and sources you gave are kept')
		self.refresh_button.installEventFilter(ToolTipFilter(self.refresh_button))
		self.refresh_button.clicked.connect(self.find)
		self.add_tool(self.refresh_button)
		# the switch: in the accent color while face mapping is on or people are being looked for, the card gets an accent tint too
		self.find_button = TogglePushButton(FluentIcon.PEOPLE, 'Face mapping off', self)
		self.find_button.setToolTip('Turn face mapping on to give each person in the target a source of their own.\nThe first time it looks through the target for the people in it, turned off it keeps them for later.')
		self.find_button.installEventFilter(ToolTipFilter(self.find_button))
		self.find_button.clicked.connect(self.on_find_clicked)
		self.add_tool(self.find_button)
		rows = QWidget(self.body)
		self.rows_layout = QVBoxLayout(rows)
		self.rows_layout.setContentsMargins(0, 0, 0, 0)
		self.rows_layout.setSpacing(2)
		self.add_widget(rows)
		self.footer_label = CaptionLabel('', self.body)
		self.footer_label.setWordWrap(True)
		self.footer_label.setTextColor('#707070', '#9a9a9a')
		self.add_widget(self.footer_label)
		self.toggled.connect(self.on_toggled)
		qconfig.themeColorChanged.connect(self.update)
		self.set_people([], 0)

	def active(self) -> bool:
		return self.finding or self.enabled and bool(self.people)

	def kept_count(self) -> int:
		return sum(1 for person in self.people if not person.get('removed'))

	def active_people(self) -> List[Dict[str, Any]]:
		"""the people runs and the face boxes follow, none while face mapping is off"""
		return self.people if self.enabled else []

	def on_find_clicked(self) -> None:
		if self.active():
			self.enabled = False
			self.refresh_text()
			self.changed.emit()
		elif self.people:
			self.enabled = True
			self.refresh_text()
			self.changed.emit()
		else:
			self.find()
			# a click toggles the button, put it back to what the card really shows when nothing started
			self.find_button.setChecked(self.active())

	def paintEvent(self, event) -> None:
		super().paintEvent(event)
		if self.active():
			painter = QPainter(self)
			painter.setRenderHint(QPainter.RenderHint.Antialiasing)
			tint = QColor(themeColor())
			tint.setAlpha(22)
			painter.setBrush(tint)
			# an outline in the accent color around the whole card says it is on
			painter.setPen(QPen(themeColor(), 1.5))
			painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 7, 7)

	def on_toggled(self, expanded : bool) -> None:
		if hasattr(self, 'page'):
			self.page.session.config.set('face_mapping_expanded', expanded)
			self.refresh_text()

	def set_people(self, people : List[Dict[str, Any]], samples : int, enabled : bool = True) -> None:
		self.people = people
		self.samples = samples
		self.enabled = enabled
		for row in self.rows:
			# gone right away, deleteLater alone leaves the old rows showing next to the new ones until the next event loop pass
			row.hide()
			self.rows_layout.removeWidget(row)
			row.deleteLater()
		self.rows = []
		for index, person in enumerate(people):
			if person.get('removed'):
				continue
			row = PersonRow(self, index, person)
			row.changed.connect(self.on_row_changed)
			row.renamed.connect(self.on_row_renamed)
			row.removed.connect(self.remove_person)
			self.rows_layout.addWidget(row)
			self.rows.append(row)
		self.refresh_text()

	def set_finding(self, done : int, total : int) -> None:
		self.finding = True
		self.find_button.setEnabled(False)
		self.find_button.setChecked(True)
		self.find_button.setText('Face mapping on')
		self.refresh_button.setEnabled(False)
		self.update()
		if total:
			self.status_label.setText('Looking at frame %d of %d…' % (done + 1, total))
		elif done == 0:
			self.status_label.setText('Loading the face models…')

	def refresh_sources(self) -> None:
		for row in self.rows:
			row.fill_sources()
		self.refresh_text()

	def refresh_text(self) -> None:
		self.finding = False
		on = self.active()
		self.find_button.setEnabled(True)
		self.find_button.setChecked(on)
		self.find_button.setText('Face mapping on' if on else 'Face mapping off')
		self.refresh_button.setEnabled(True)
		self.refresh_button.setVisible(on)
		self.rows_layout.parentWidget().setVisible(on)
		self.update()
		count = self.kept_count()
		if self.people and not self.enabled:
			self.status_label.setText('%d %s saved' % (count, 'person' if count == 1 else 'people'))
			self.status_label.setToolTip('')
			self.footer_label.setText('Face mapping is off, Start runs as usual. Turn it on to use the people found before with their names and sources.')
			return
		if not self.people:
			self.status_label.setText('')
			self.footer_label.setText('Turn face mapping on to give each person in the target a source of their own.' if self.page.target_path else 'Choose a target first.')
			return
		frames = ' in %d frames' % self.samples if self.samples > 1 else ''
		summary = [ '%s → %s' % (person_name(self.people, index), self.page.source_label(person.get('source'))) for index, person in enumerate(self.people) if person.get('source') ]
		if self.body.isVisible() or not summary:
			self.status_label.setText('%d %s found%s' % (count, 'person' if count == 1 else 'people', frames))
		else:
			self.status_label.setText(' · '.join(summary))
		self.status_label.setToolTip('\n'.join(summary))
		passes = plan_passes(self.people, [ path for path in self.page.active_sources() if media_kind(path) == 'image' ], self.page.source_paths, 0.3)
		if not passes:
			self.footer_label.setText('Pick a source for each face you want to change. With none picked, Start runs as usual.')
		elif passes[0].get('many'):
			self.footer_label.setText('Everyone gets the same source, so it runs once like a normal run.')
		else:
			left = count - len(passes)
			self.footer_label.setText('Runs %d %s, one per mapped face.%s Pick the same source twice to put one person on both faces.' % (len(passes), 'pass' if len(passes) == 1 else 'passes', ' %d left unchanged.' % left if left else ''))

	def on_row_changed(self) -> None:
		self.refresh_text()
		self.changed.emit()

	def on_row_renamed(self) -> None:
		self.refresh_text()
		self.renamed.emit()

	def remove_person(self, index : int) -> None:
		"""takes a person off the list and leaves their face unchanged, the last one leaves no people at all"""
		if not is_kept(self.people, index):
			return
		if self.kept_count() == 1:
			self.clear()
			return
		self.people[index].update(removed = True, source = LEAVE)
		self.set_people(self.people, self.samples, self.enabled)
		self.changed.emit()

	def restore_person(self, index : int) -> None:
		if 0 <= index < len(self.people) and self.people[index].get('removed'):
			self.people[index]['removed'] = False
			self.set_people(self.people, self.samples, self.enabled)
			self.changed.emit()

	def clear(self) -> None:
		self.set_people([], 0)
		self.changed.emit()

	def row(self, index : int) -> Optional[PersonRow]:
		return next((row for row in self.rows if row.index == index), None)
