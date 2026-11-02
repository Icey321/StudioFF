from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon, ToolTipFilter, themeColor

from ffstudio import splash
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


def page_default(store : Any, dest : str) -> Any:
	"""the option's default on the page, its own one (Batch's Many mode) before facefusion's"""
	defaults = getattr(store, 'defaults', None) or {}
	return defaults[dest] if dest in defaults else store.schema.resolve(dest, store.values).get('default')


def merge_order(groups : List[str], saved : List[str]) -> List[str]:
	"""the groups in the saved order, a group missing from it goes right after the group in front of it in groups"""
	order = [ group for group in saved if group in groups ]
	for index, group in enumerate(groups):
		if group not in order:
			before = next((other for other in reversed(groups[:index]) if other in order), None)
			order.insert(order.index(before) + 1 if before else 0, group)
	return order


class DropSpot(QWidget):
	"""the open spot a dragged group lands in, a dashed outline in the accent color"""

	def paintEvent(self, _) -> None:
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.Antialiasing)
		accent = themeColor()
		fill = QColor(accent)
		fill.setAlpha(28)
		pen = QPen(accent, 1.5, Qt.PenStyle.DashLine)
		painter.setPen(pen)
		painter.setBrush(fill)
		painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 8, 8)


class OptionPanel(QWidget):
	"""a column of collapsible option groups generated from the facefusion schema"""

	def __init__(self, session : Session, groups : List[str], expanded : Optional[List[str]] = None, parent : Optional[QWidget] = None, favorites : bool = False, info_icons : bool = False, short_labels : bool = False, group_captions : bool = False, expansion_key : str = '', order_key : str = '') -> None:
		super().__init__(parent)
		self.session = session
		self.groups = groups
		# config key of the order the groups were dragged into, with one the groups can be dragged by their title
		self.order_key = order_key
		self.header_cards : Dict[QWidget, CollapsibleCard] = {}
		self.drag : Optional[Dict[str, Any]] = None
		self.drop_spot = DropSpot(self)
		self.drop_spot.hide()
		self.ghost = QLabel(self)
		shadow = QGraphicsDropShadowEffect(self.ghost)
		shadow.setBlurRadius(28)
		shadow.setOffset(0, 6)
		shadow.setColor(QColor(0, 0, 0, 110))
		self.ghost.setGraphicsEffect(shadow)
		self.ghost.hide()
		self.scroll_timer = QTimer(self)
		self.scroll_timer.setInterval(30)
		self.scroll_timer.timeout.connect(self.scroll_while_dragging)
		# rows get a star that adds them to the favorites box
		self.favorites = favorites
		# rows keep their description behind an (i) instead of under the label
		self.info_icons = info_icons
		# rows drop the group name from their label, it is already the title of their box
		self.short_labels = short_labels or group_captions
		# rows show their group words in small capitals under a short label, for boxes that mix groups
		self.group_captions = group_captions
		self.expanded = expanded or []
		# config key that remembers which groups were opened or closed by hand, they come back that way
		self.expansion_key = expansion_key
		self.cards : Dict[str, CollapsibleCard] = {}
		self.rows : Dict[str, OptionRow] = {}
		self.group_rows : Dict[str, List[OptionRow]] = {}
		self.filter_text = ''
		self.saved_expansion : Dict[str, bool] = {}
		# rows grayed out because something else decides them, dest -> (value shown or KEEP_VALUE, reason)
		self.locks : Dict[str, Tuple[Any, str]] = {}
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
		self.header_cards.clear()
		self.end_drag()
		schema = self.session.schema
		if not schema:
			return
		for group in self.ordered_groups():
			options = schema.group_options(group)
			if not options:
				continue
			is_processor = group in schema.processors
			card = CollapsibleCard(schema.group_title(group), '%d options' % len(options), PROCESSOR_ICON if is_processor else GROUP_ICONS.get(group, FluentIcon.SETTING), expanded = self.is_expanded(group), parent = self)
			card.toggled.connect(lambda expanded, group = group: self.remember_expanded(group, expanded))
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
			if self.order_key:
				card.header.setToolTip('Click to open or close, drag to move this group')
				card.header.installEventFilter(ToolTipFilter(card.header, 1000))
				card.header.installEventFilter(self)
				self.header_cards[card.header] = card
		self.refresh_visibility()
		self.refresh_subtitles()
		self.refresh_favorites()

	def ordered_groups(self) -> List[str]:
		saved = (self.session.config.get(self.order_key) or []) if self.order_key else []
		return merge_order(self.groups, saved)

	def eventFilter(self, watched : QObject, event : QEvent) -> bool:
		"""a group is dragged by its title, the others slide aside to open a spot for it. a click still opens or closes it"""
		card = self.header_cards.get(watched)
		if card is None or self.filter_text:
			return super().eventFilter(watched, event)
		kind = event.type()
		if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
			self.drag = { 'card': card, 'start': event.globalPosition().toPoint(), 'moved': False }
		elif kind == QEvent.Type.MouseMove and self.drag and self.drag.get('card') is card:
			point = event.globalPosition().toPoint()
			if not self.drag.get('moved'):
				if (point - self.drag.get('start')).manhattanLength() < QApplication.startDragDistance():
					return False
				self.drag['moved'] = True
				watched.setCursor(Qt.CursorShape.ClosedHandCursor)
				self.lift(card)
			self.drag['point'] = point
			self.drag_to(card, point)
			return True
		elif kind == QEvent.Type.MouseButtonRelease and self.drag and self.drag.get('card') is card:
			moved = self.drag.get('moved')
			if moved:
				watched.setCursor(Qt.CursorShape.PointingHandCursor)
				self.drop(card)
				self.save_order()
			self.drag = None
			if moved:
				# the card would take the release as a click on its title and open or close
				return True
		return super().eventFilter(watched, event)

	def lift(self, card : CollapsibleCard) -> None:
		"""the groups leave the layout while one is dragged, it floats with the pointer above a spot the others slide away from"""
		order = self.layout_cards()
		shown = [ other for other in order if other.isVisible() ]
		self.drag.update(
		{
			'order': order,
			'others': [ other for other in shown if other is not card ],
			'top': min(other.y() for other in shown),
			'offset': self.mapFromGlobal(self.drag.get('start')).y() - card.y(),
			'gap': shown.index(card),
			'targets': {},
			'animations': {},
			'minimum': self.minimumHeight()
		})
		# the column keeps its height, the layout has nothing in it until the drop
		self.setMinimumHeight(self.height())
		for other in order:
			self.layout_.removeWidget(other)
		# a solid picture of the group floats above everything, the group itself turns invisible but keeps the mouse
		self.ghost.setPixmap(self.window().grab(QRect(card.mapTo(self.window(), QPoint(0, 0)), card.size())))
		self.ghost.setGeometry(card.geometry())
		self.drop_spot.setGeometry(card.geometry())
		self.drop_spot.show()
		self.drop_spot.raise_()
		self.ghost.show()
		self.ghost.raise_()
		hidden = QGraphicsOpacityEffect(card)
		hidden.setOpacity(0)
		card.setGraphicsEffect(hidden)
		self.slide(card, animate = False)
		self.scroll_timer.start()

	def drag_to(self, card : CollapsibleCard, point : QPoint) -> None:
		pointer = self.mapFromGlobal(point).y()
		card.move(card.x(), max(0, min(pointer - self.drag.get('offset'), self.height() - card.height())))
		self.ghost.move(card.pos())
		# the spot opens in front of the first group whose middle is below the pointer
		targets = self.drag.get('targets')
		gap = sum(1 for other in self.drag.get('others') if targets.get(other, other.y()) + other.height() // 2 < pointer)
		if gap != self.drag.get('gap'):
			self.drag['gap'] = gap
			self.slide(card)

	def slide(self, card : CollapsibleCard, animate : bool = True) -> None:
		"""moves the other groups up or down so the spot for the dragged one is open"""
		spacing = self.layout_.spacing()
		y = self.drag.get('top')
		places : List[Tuple[QWidget, int]] = []
		for index, other in enumerate(self.drag.get('others')):
			if index == self.drag.get('gap'):
				places.append((self.drop_spot, y))
				y += card.height() + spacing
			places.append((other, y))
			y += other.height() + spacing
		if self.drag.get('gap') >= len(self.drag.get('others')):
			places.append((self.drop_spot, y))
		for widget, top in places:
			self.drag.get('targets')[widget] = top
			running = self.drag.get('animations').pop(widget, None)
			if running:
				running.stop()
				running.deleteLater()
			if not animate:
				widget.move(widget.x(), top)
				continue
			animation = QPropertyAnimation(widget, b'pos', self)
			animation.setDuration(180)
			animation.setEasingCurve(QEasingCurve.Type.OutCubic)
			animation.setEndValue(QPoint(widget.x(), top))
			animation.start()
			self.drag.get('animations')[widget] = animation

	def drop(self, card : CollapsibleCard) -> None:
		"""the groups go back into the layout in the new order, the dragged one glides into its spot"""
		for animation in self.drag.get('animations').values():
			animation.stop()
			animation.deleteLater()
		self.scroll_timer.stop()
		self.drop_spot.hide()
		self.ghost.hide()
		others = self.drag.get('others')
		order = [ other for other in self.drag.get('order') if other is not card ]
		gap = self.drag.get('gap')
		if gap < len(others):
			order.insert(order.index(others[gap]), card)
		else:
			order.insert(order.index(others[-1]) + 1 if others else len(order), card)
		for other in order:
			self.layout_.addWidget(other)
		self.setMinimumHeight(self.drag.get('minimum'))
		floating = card.pos()
		self.layout_.activate()
		card.setGraphicsEffect(None)
		landed = card.pos()
		card.move(floating)
		animation = QPropertyAnimation(card, b'pos', self)
		animation.setDuration(140)
		animation.setEasingCurve(QEasingCurve.Type.OutCubic)
		animation.setEndValue(landed)
		animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

	def end_drag(self) -> None:
		"""a rebuild in the middle of a drag drops it, the cards it moved are gone"""
		if self.drag and self.drag.get('moved'):
			self.scroll_timer.stop()
			self.drop_spot.hide()
			self.ghost.hide()
			self.setMinimumHeight(self.drag.get('minimum'))
		self.drag = None

	def scroll_while_dragging(self) -> None:
		"""held near the top or bottom of the scroll area, it keeps scrolling to the groups out of sight"""
		if not self.drag or not self.drag.get('point'):
			return
		area = self.parentWidget()
		while area and not isinstance(area, QAbstractScrollArea):
			area = area.parentWidget()
		if not area:
			return
		point = self.drag.get('point')
		y = area.viewport().mapFromGlobal(point).y()
		bar = area.verticalScrollBar()
		before = bar.value()
		if y < 40:
			bar.setValue(before - 16)
		elif y > area.viewport().height() - 40:
			bar.setValue(before + 16)
		if bar.value() != before:
			self.drag_to(self.drag.get('card'), point)

	def layout_cards(self) -> List[CollapsibleCard]:
		cards = set(self.cards.values())
		widgets = [ self.layout_.itemAt(index).widget() for index in range(self.layout_.count()) ]
		return [ widget for widget in widgets if widget in cards ]

	def save_order(self) -> None:
		groups = { card: group for group, card in self.cards.items() }
		shown = [ groups.get(card) for card in self.layout_cards() ]
		# groups not shown now (a schema without them) keep their place in the saved order
		saved = (self.session.config.get(self.order_key) or []) if self.order_key else []
		self.session.config.set(self.order_key, shown + [ group for group in saved if group not in shown ])

	def is_expanded(self, group : str) -> bool:
		saved = self.session.config.get(self.expansion_key) or {} if self.expansion_key else {}
		return saved.get(group, group in self.expanded)

	def remember_expanded(self, group : str, expanded : bool) -> None:
		# searching opens every matching group, that is not the user's choice
		if not self.expansion_key or self.filter_text:
			return
		saved = dict(self.session.config.get(self.expansion_key) or {})
		if saved.get(group) != expanded:
			saved[group] = expanded
			self.session.config.set(self.expansion_key, saved)

	def create_row(self, option : Dict[str, Any], card : CollapsibleCard, context : str = '') -> OptionRow:
		splash.step()
		return OptionRow(self.session.schema, option, self.session.values, self.session.set_value, card.body, on_favorite = self.toggle_favorite if self.favorites else None, context = context, info_icon = self.info_icons, short_label = self.short_labels, group_caption = self.group_captions, defaults = getattr(self.session, 'defaults', None))

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
		# every rebuild ends here, the new rows get the locks of the old ones
		self.set_locks(self.locks)

	def set_locks(self, locks : Dict[str, Tuple[Any, str]]) -> None:
		self.locks = dict(locks)
		for row in set(self.rows.values()):
			row.set_lock(self.locks.get(row.dest))

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
		# put them back while still searching, so the restore is not remembered as a choice
		if not text:
			for group, expanded in self.saved_expansion.items():
				if group in self.cards:
					self.cards[group].set_expanded(expanded)
			self.saved_expansion = {}
		self.filter_text = text
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
				if not same_value(self.session.values.get(dest), page_default(self.session, dest)):
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
		card = CollapsibleCard('Favorite Options', '', star_icon(), expanded = self.session.config.get('favorites_expanded', True), parent = self)
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
			self.hidden_label.setText('Hover over any option below and click its star to keep a copy of it here.')
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
		# a paired row (Age) is one option, however many of its values changed
		rows = set(self.rows.values())
		changed = len([ row for row in rows if any(not same_value(self.session.values.get(dest), page_default(self.session, dest)) for dest in row.dests()) ])
		count = len(rows)
		card.set_subtitle(('%d option%s' % (count, '' if count == 1 else 's') + (' · %d changed' % changed if changed else '')) if count else 'none yet')
