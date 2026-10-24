from typing import Any, Dict, List, Optional

from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon

from ffstudio.option_widgets import PAIRED_PARTNERS, REQUIRED_CHOICES, OptionRow
from ffstudio.schema import same_value
from ffstudio.session import Session
from ffstudio.widgets import CollapsibleCard, star_icon

GROUP_ICONS =\
{
	'face_detector': FluentIcon.SEARCH,
	'face_aligner': FluentIcon.ALIGNMENT,
	'face_selector': FluentIcon.PEOPLE,
	'face_tracker': FluentIcon.VIEW,
	'face_masker': FluentIcon.PALETTE,
	'voice_extractor': FluentIcon.MICROPHONE,
	'frame_extraction': FluentIcon.CUT,
	'frame_distribution': FluentIcon.TILES,
	'output_creation': FluentIcon.SAVE,
	'workflow': FluentIcon.SYNC,
	'execution': FluentIcon.SPEED_HIGH,
	'memory': FluentIcon.DICTIONARY,
	'download': FluentIcon.DOWNLOAD,
	'misc': FluentIcon.DEVELOPER_TOOLS,
	'paths': FluentIcon.FOLDER
}
PROCESSOR_ICON = FluentIcon.ROBOT


class OptionPanel(QWidget):
	"""a column of collapsible option groups generated from the facefusion schema"""

	def __init__(self, session : Session, groups : List[str], expanded : Optional[List[str]] = None, parent : Optional[QWidget] = None, favorites : bool = False, info_icons : bool = False, short_labels : bool = False, group_captions : bool = False) -> None:
		super().__init__(parent)
		self.session = session
		self.groups = groups
		# rows get a star that adds them to the favorites box
		self.favorites = favorites
		# rows keep their description behind an (i) instead of under the label
		self.info_icons = info_icons
		# rows drop the group name from their label, it is already the title of their box
		self.short_labels = short_labels or group_captions
		# rows show their group words in small capitals under a short label, for boxes that mix groups
		self.group_captions = group_captions
		self.expanded = expanded or []
		self.cards : Dict[str, CollapsibleCard] = {}
		self.rows : Dict[str, OptionRow] = {}
		self.group_rows : Dict[str, List[OptionRow]] = {}
		self.filter_text = ''
		self.saved_expansion : Dict[str, bool] = {}
		self.layout_ = QVBoxLayout(self)
		self.layout_.setContentsMargins(0, 0, 0, 0)
		self.layout_.setSpacing(10)
		self.session.value_changed.connect(self.on_value_changed)
		self.session.favorites_changed.connect(self.refresh_favorites)
		self.rebuild()

	def rebuild(self) -> None:
		for card in self.cards.values():
			card.setParent(None)
			card.deleteLater()
		self.cards.clear()
		self.rows.clear()
		self.group_rows.clear()
		schema = self.session.schema
		if not schema:
			return
		for group in self.groups:
			options = schema.group_options(group)
			if not options:
				continue
			is_processor = group in schema.processors
			card = CollapsibleCard(schema.group_title(group), '%d options' % len(options), PROCESSOR_ICON if is_processor else GROUP_ICONS.get(group, FluentIcon.SETTING), expanded = group in self.expanded, parent = self)
			self.group_rows[group] = []
			for option in options:
				if option.get('dest') in PAIRED_PARTNERS:
					continue
				row = self.create_row(option, card)
				card.add_widget(row)
				for dest in row.dests():
					self.rows[dest] = row
				self.group_rows[group].append(row)
			self.cards[group] = card
			self.layout_.addWidget(card)
		self.refresh_visibility()
		self.refresh_subtitles()
		self.refresh_favorites()

	def create_row(self, option : Dict[str, Any], card : CollapsibleCard, context : str = '') -> OptionRow:
		return OptionRow(self.session.schema, option, self.session.values, self.session.set_value, card.body, on_favorite = self.toggle_favorite if self.favorites else None, context = context, info_icon = self.info_icons, short_label = self.short_labels, group_caption = self.group_captions)

	def toggle_favorite(self, dest : str) -> None:
		"""a paired row is one favorite, a star on either of its options counts for both"""
		row = self.rows.get(dest)
		starred = [ item for item in (row.dests() if row else [ dest ]) if item in self.session.favorites() ]
		for item in starred or [ dest ]:
			self.session.toggle_favorite(item)

	def refresh_favorites(self) -> None:
		favorites = self.session.favorites()
		for row in set(self.rows.values()):
			row.set_favorite(any(dest in favorites for dest in row.dests()))

	def set_groups(self, groups : List[str]) -> None:
		if groups != self.groups:
			self.groups = groups
			self.rebuild()

	def refresh_visibility(self) -> None:
		schema = self.session.schema
		if not schema:
			return
		active = self.session.processors()
		for group, card in self.cards.items():
			allowed = group not in schema.processors or group in active
			if self.filter_text:
				title_hit = all(word in schema.group_title(group).lower() for word in self.filter_text.split())
				hits = 0
				for row in self.group_rows.get(group, []):
					hit = title_hit or row.matches(self.filter_text)
					row.setVisible(hit)
					hits += hit
				card.setVisible(allowed and hits > 0)
				card.set_expanded(True)
			else:
				for row in self.group_rows.get(group, []):
					row.setVisible(True)
				card.setVisible(allowed)

	def set_filter(self, text : str) -> None:
		text = text.strip().lower()
		if text == self.filter_text:
			return
		# open every matching group while searching and put the groups back the way they were afterwards
		if text and not self.filter_text:
			self.saved_expansion = { group: card.body.isVisible() for group, card in self.cards.items() }
		self.filter_text = text
		if not text:
			for group, expanded in self.saved_expansion.items():
				if group in self.cards:
					self.cards[group].set_expanded(expanded)
			self.saved_expansion = {}
		self.refresh_visibility()

	def has_visible_cards(self) -> bool:
		return any(not card.isHidden() for card in self.cards.values())

	def refresh_subtitles(self) -> None:
		schema = self.session.schema
		for group, card in self.cards.items():
			changed = 0
			options = schema.group_options(group)
			for option in options:
				dest = option.get('dest')
				if not same_value(self.session.values.get(dest), schema.resolve(dest, self.session.values).get('default')):
					changed += 1
			card.set_subtitle('%d options' % len(options) + (' · %d changed' % changed if changed else ''))

	def on_value_changed(self, dest : str, _ : Any) -> None:
		schema = self.session.schema
		if not schema:
			return
		if dest == 'processors':
			self.refresh_visibility()
		# the same option can be shown twice (in its group and in favorites), keep the other copy in line
		row = self.rows.get(dest)
		if row:
			row.sync()
		for dependent, dependency in schema.dependencies.items():
			if dependency.get('driver') == dest and dependent in self.rows:
				self.rows[dependent].refresh_option()
		for dependent, (driver, _) in REQUIRED_CHOICES.items():
			if driver == dest and dependent in self.rows:
				self.rows[dependent].refresh_available()
		self.refresh_subtitles()


class FavoritesPanel(OptionPanel):
	"""one box with a copy of every option that was starred, in the order they were starred"""

	def __init__(self, session : Session, parent : Optional[QWidget] = None) -> None:
		self.hidden_label : Optional[CaptionLabel] = None
		super().__init__(session, [], parent = parent, favorites = True, info_icons = True, group_captions = True)
		session.favorites_changed.connect(self.rebuild)

	def rebuild(self) -> None:
		for card in self.cards.values():
			card.setParent(None)
			card.deleteLater()
		self.cards.clear()
		self.rows.clear()
		self.group_rows.clear()
		schema = self.session.schema
		if not schema:
			return
		card = CollapsibleCard('Favorites', '', star_icon(), expanded = self.session.config.get('favorites_expanded', True), parent = self)
		card.toggled.connect(lambda expanded: self.session.config.set('favorites_expanded', expanded))
		rows = []
		for dest in self.session.favorites():
			# the upper option of a pair shows on the row of the lower one
			dest = PAIRED_PARTNERS.get(dest, dest)
			option = schema.options.get(dest)
			if option and dest not in self.rows:
				row = self.create_row(option, card, schema.group_title(option.get('group')))
				card.add_widget(row)
				for item in row.dests():
					self.rows[item] = row
				rows.append(row)
		self.hidden_label = CaptionLabel('', card.body)
		self.hidden_label.setWordWrap(True)
		self.hidden_label.setTextColor('#707070', '#9a9a9a')
		card.add_widget(self.hidden_label)
		self.group_rows['favorites'] = rows
		self.cards['favorites'] = card
		self.layout_.addWidget(card)
		self.refresh_visibility()
		self.refresh_subtitles()
		self.refresh_favorites()

	def refresh_visibility(self) -> None:
		schema = self.session.schema
		if not schema or not self.hidden_label:
			return
		active = self.session.processors()
		rows = self.group_rows.get('favorites', [])
		hidden = 0
		for row in rows:
			group = schema.options.get(row.dest, {}).get('group')
			allowed = group not in schema.processors or group in active
			row.setVisible(allowed)
			hidden += not allowed
		if not rows:
			self.hidden_label.setText('Hover over any setting below and click its star to keep a copy of it here.')
		else:
			self.hidden_label.setText('%d hidden because %s processor is switched off.' % (hidden, 'its' if hidden == 1 else 'their') if hidden else '')
		self.hidden_label.setVisible(bool(self.hidden_label.text()))

	def set_filter(self, text : str) -> None:
		pass

	def refresh_subtitles(self) -> None:
		schema = self.session.schema
		card = self.cards.get('favorites')
		if not schema or not card:
			return
		changed = len([ dest for dest in self.rows if not same_value(self.session.values.get(dest), schema.resolve(dest, self.session.values).get('default')) ])
		count = len(set(self.rows.values()))
		card.set_subtitle(('%d setting%s' % (count, '' if count == 1 else 's') + (' · %d changed' % changed if changed else '')) if count else 'none yet')
