from typing import Any, Dict, List, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath, QPen

from ffstudio.media import format_seconds
from ffstudio.platforms import UI_FONT


def color(text : str) -> QColor:
	"""a #rrggbb or #rrggbbaa color, Qt itself reads eight digits as #aarrggbb"""
	if len(text) == 9:
		return QColor(int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16), int(text[7:9], 16))
	return QColor(text)


WIDTH = 900
SCALE = 2
PADDING = 32
BACKGROUND = color('#16181d')
CARD = color('#1f2229')
LINE = color('#2c303a')
TEXT = color('#eceef2')
MUTED = color('#9aa1ad')
ACCENT = color('#4cc2ff')
TABLE_COLUMNS = [ ('Resolution', 0.0), ('FPS', 0.14), ('Average (sec)', 0.43), ('Fastest (sec)', 0.57), ('Slowest (sec)', 0.71), ('1-min clip', 0.86) ]
# where the fps bar sits in the table, as parts of the table width
FPS_BAR = (0.14, 0.25)


RESOLUTIONS = [ '240p', '360p', '540p', '720p', '1080p', '1440p', '2160p' ]
# a video plays in real time at 30 frames per second, the clips the estimates talk about are one minute of that
REAL_TIME_FPS = 30.0
CLIP_FRAMES = 60 * 30
CHART_TOP = 22
CHART_BAR = 28
CHART_GAP = 10
CHART_LABEL = 64
CHART_VALUE = 130
CARD_HEIGHT = 66
CHART_COLORS =\
{
	'dark': { 'text': '#eceef2', 'muted': '#9aa1ad', 'track': '#ffffff10', 'bar': ('#2f7dd6', '#4cc2ff'), 'ghost': '#ffffff1c', 'ghost_line': '#ffffff70', 'real_time': '#ffb84c', 'up': '#3ddc84', 'down': '#ff6b6b', 'card': '#ffffff0d', 'good': '#2fa86b', 'badge': '#55595f', 'badge_text': '#e0e0e0' },
	'light': { 'text': '#1b1b1b', 'muted': '#606060', 'track': '#0000000a', 'bar': ('#0067c0', '#3a9be0'), 'ghost': '#00000012', 'ghost_line': '#00000066', 'real_time': '#c47a00', 'up': '#0f7b0f', 'down': '#c42b1c', 'card': '#0000000a', 'good': '#0f7b0f', 'badge': '#d6d6d6', 'badge_text': '#303030' }
}


def make_font(size : float, bold : bool = False) -> QFont:
	# pixel sizes, so measuring and drawing agree whatever resolution the image reports
	font = QFont(UI_FONT)
	font.setPixelSize(round(size * 4 / 3))
	font.setBold(bold)
	return font


def sort_by_resolution(rows : List[Dict[str, Any]]) -> List[Dict[str, Any]]:
	return sorted(rows, key = lambda row: RESOLUTIONS.index(row.get('resolution')) if row.get('resolution') in RESOLUTIONS else len(RESOLUTIONS))


def format_processors(entry : Dict[str, Any]) -> str:
	"""processor names in run order, each with the model it used when that is known"""
	models = entry.get('models') or {}
	names = []
	for processor in entry.get('processors') or []:
		name = processor.replace('_', ' ')
		names.append('%s (%s)' % (name, models.get(processor)) if models.get(processor) else name)
	return ' → '.join(names)


def same_setup(entry : Dict[str, Any], other : Dict[str, Any]) -> bool:
	"""two results ran the same processors with the same models, so their speeds compare"""
	return list(entry.get('processors') or []) == list(other.get('processors') or []) and (entry.get('models') or {}) == (other.get('models') or {})


def run_key(entry : Dict[str, Any]) -> Any:
	return entry.get('run_id') or entry.get('when')


def best_earlier(rows : List[Dict[str, Any]], history : List[Dict[str, Any]]) -> Dict[str, float]:
	"""per resolution, the best fps another run with the same processors and models reached"""
	best : Dict[str, float] = {}
	if not rows:
		return best
	key = run_key(rows[0])
	for entry in history:
		if run_key(entry) == key or not same_setup(entry, rows[0]):
			continue
		resolution = entry.get('resolution')
		best[resolution] = max(best.get(resolution, 0.0), float(entry.get('fps') or 0))
	return best


def chart_height(row_count : int) -> float:
	return CHART_TOP + row_count * (CHART_BAR + CHART_GAP) - CHART_GAP


def draw_chart(painter : QPainter, left : float, top : float, width : float, rows : List[Dict[str, Any]], ghost : Dict[str, float], colors : Dict[str, Any]) -> None:
	"""one bar per resolution, the dashed outline behind it is the result it is compared with, the line is real time"""
	rows = sort_by_resolution(rows)
	bars_left = left + CHART_LABEL
	bars_width = max(40.0, width - CHART_LABEL - CHART_VALUE)
	scale = max([ float(row.get('fps') or 0) for row in rows ] + list(ghost.values()) + [ REAL_TIME_FPS ]) * 1.04
	painter.save()
	painter.setRenderHint(QPainter.RenderHint.Antialiasing)
	y = top + CHART_TOP
	for row in rows:
		fps = float(row.get('fps') or 0)
		resolution = row.get('resolution') or ''
		painter.setPen(color(colors.get('text')))
		painter.setFont(make_font(10.5, True))
		painter.drawText(QRectF(left, y, CHART_LABEL, CHART_BAR), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), resolution)
		fill_rounded(painter, QRectF(bars_left, y, bars_width, CHART_BAR), color(colors.get('track')), 5)
		earlier = ghost.get(resolution)
		if earlier:
			ghost_rect = QRectF(bars_left, y, max(6.0, bars_width * earlier / scale), CHART_BAR)
			fill_rounded(painter, ghost_rect, color(colors.get('ghost')), 5)
			pen = QPen(color(colors.get('ghost_line')), 1.2, Qt.PenStyle.DashLine)
			painter.setPen(pen)
			painter.setBrush(Qt.BrushStyle.NoBrush)
			painter.drawRoundedRect(ghost_rect.adjusted(0.6, 0.6, -0.6, -0.6), 5, 5)
		bar_rect = QRectF(bars_left, y + 3, max(6.0, bars_width * fps / scale), CHART_BAR - 6)
		gradient = QLinearGradient(bar_rect.topLeft(), bar_rect.topRight())
		gradient.setColorAt(0, color(colors.get('bar')[0]))
		gradient.setColorAt(1, color(colors.get('bar')[1]))
		path = QPainterPath()
		path.addRoundedRect(bar_rect, 4, 4)
		painter.fillPath(path, gradient)
		value_left = bars_left + max(bar_rect.width(), bars_width * (earlier or 0) / scale) + 8
		value_text = '%.1f' % fps
		change_text = ''
		if earlier:
			change = (fps - earlier) / earlier * 100
			change_text = '%+.0f%%' % change if abs(change) >= 0.5 else '±0%'
		# the value and its change start past the real time line when they would cross it
		line_x = bars_left + bars_width * REAL_TIME_FPS / scale
		label_width = QFontMetricsF(make_font(10.5, True)).horizontalAdvance(value_text) + (6 + QFontMetricsF(make_font(9, True)).horizontalAdvance(change_text) if change_text else 0)
		if value_left - 4 < line_x < value_left + label_width + 4:
			value_left = line_x + 8
		painter.setFont(make_font(10.5, True))
		painter.setPen(color(colors.get('text')))
		painter.drawText(QRectF(value_left, y, 80, CHART_BAR), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), value_text)
		if change_text:
			change_color = colors.get('up') if change >= 1 else colors.get('down') if change <= -1 else colors.get('muted')
			painter.setFont(make_font(9, True))
			painter.setPen(color(change_color))
			value_width = QFontMetricsF(make_font(10.5, True)).horizontalAdvance(value_text)
			painter.drawText(QRectF(value_left + value_width + 6, y, 80, CHART_BAR), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), change_text)
		y += CHART_BAR + CHART_GAP
	# real time line over all bars, its label above the first
	x = bars_left + bars_width * REAL_TIME_FPS / scale
	painter.setPen(QPen(color(colors.get('real_time')), 1.6, Qt.PenStyle.DashLine))
	painter.drawLine(QPointF(x, top + CHART_TOP - 4), QPointF(x, y - CHART_GAP + 4))
	painter.setFont(make_font(8.5, True))
	painter.setPen(color(colors.get('real_time')))
	painter.drawText(QRectF(x - 120, top, 240, CHART_TOP - 6), int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom), '30 fps = real time')
	painter.restore()


def estimate_cards(rows : List[Dict[str, Any]], history : List[Dict[str, Any]]) -> List[Dict[str, Any]]:
	"""what the speeds mean in plain words: how long a one minute clip takes, and the best result so far"""
	rows = sort_by_resolution(rows)
	if not rows:
		return []
	by_resolution = { row.get('resolution'): row for row in rows }
	picks = [ name for name in [ '1080p', '2160p' ] if name in by_resolution ]
	for row in [ rows[-1], rows[0] ]:
		if len(picks) < 2 and row.get('resolution') not in picks:
			picks.append(row.get('resolution'))
	picks = sorted(picks, key = lambda name: RESOLUTIONS.index(name) if name in RESOLUTIONS else len(RESOLUTIONS))
	cards = []
	for name in picks:
		fps = float(by_resolution.get(name).get('fps') or 0)
		if fps <= 0:
			continue
		seconds = CLIP_FRAMES / fps
		badge = 'Real Time' if fps >= REAL_TIME_FPS else '%.1f× slower' % (REAL_TIME_FPS / fps)
		cards.append({ 'title': 'A 1-minute %s clip' % name, 'value': '≈ ' + format_seconds(seconds), 'badge': badge, 'good': fps >= REAL_TIME_FPS })
	largest = rows[-1]
	same = [ entry for entry in history if entry.get('resolution') == largest.get('resolution') and same_setup(entry, largest) ] or [ largest ]
	best = max(same, key = lambda entry: float(entry.get('fps') or 0))
	cards.append({ 'title': 'Best %s result so far' % largest.get('resolution'), 'value': '%.1f fps' % float(best.get('fps') or 0), 'badge': '%s  ·  %s' % (best.get('provider') or 'default', (best.get('when') or '')[:10]), 'good': None })
	return cards


def draw_cards(painter : QPainter, left : float, top : float, width : float, cards : List[Dict[str, Any]], colors : Dict[str, Any]) -> None:
	if not cards:
		return
	painter.save()
	painter.setRenderHint(QPainter.RenderHint.Antialiasing)
	gap = 10.0
	card_width = (width - gap * (len(cards) - 1)) / len(cards)
	for index, card in enumerate(cards):
		x = left + index * (card_width + gap)
		fill_rounded(painter, QRectF(x, top, card_width, CARD_HEIGHT), color(colors.get('card')), 8)
		painter.setFont(make_font(9))
		painter.setPen(color(colors.get('muted')))
		painter.drawText(QRectF(x + 12, top + 8, card_width - 24, 16), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), card.get('title'))
		painter.setFont(make_font(14, True))
		painter.setPen(color(colors.get('text')))
		value_width = QFontMetricsF(make_font(14, True)).horizontalAdvance(card.get('value'))
		painter.drawText(QRectF(x + 12, top + 24, card_width - 24, 24), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), card.get('value'))
		badge = card.get('badge') or ''
		if card.get('good') is None:
			painter.setFont(make_font(8.5))
			painter.setPen(color(colors.get('muted')))
			painter.drawText(QRectF(x + 12, top + 46, card_width - 24, 16), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), badge)
			continue
		font = make_font(8, True)
		badge_width = QFontMetricsF(font).horizontalAdvance(badge) + 14
		badge_rect = QRectF(min(x + 12 + value_width + 10, x + card_width - badge_width - 10), top + 28, badge_width, 17)
		fill_rounded(painter, badge_rect, color(colors.get('good') if card.get('good') else colors.get('badge')), 8.5)
		painter.setFont(font)
		painter.setPen(color('#ffffff') if card.get('good') else color(colors.get('badge_text')))
		painter.drawText(badge_rect, int(Qt.AlignmentFlag.AlignCenter), badge)
	painter.restore()


def fill_rounded(painter : QPainter, rect : QRectF, color : QColor, radius : float) -> None:
	path = QPainterPath()
	path.addRoundedRect(rect, radius, radius)
	painter.fillPath(path, color)


class ReportPainter:
	"""lays the report out top to bottom, a pass without a painter only measures the height"""

	def __init__(self, painter : Optional[QPainter]) -> None:
		self.painter = painter
		self.y = 0.0

	def text(self, rect : QRectF, text : str, font : QFont, color : QColor, flags : int = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop) -> float:
		wrap_flags = int(flags) | int(Qt.TextFlag.TextWordWrap)
		height = QFontMetricsF(font).boundingRect(rect, wrap_flags, text).height()
		if self.painter:
			self.painter.setFont(font)
			self.painter.setPen(color)
			self.painter.drawText(QRectF(rect.x(), rect.y(), rect.width(), max(rect.height(), height)), wrap_flags, text)
		return height

	def fill(self, rect : QRectF, color : QColor, radius : float = 0) -> None:
		if self.painter:
			path = QPainterPath()
			path.addRoundedRect(rect, radius, radius)
			self.painter.fillPath(path, color)

	def draw(self, hardware : Dict[str, str], version : str, runs : List[List[Dict[str, Any]]]) -> float:
		left = PADDING
		width = WIDTH - PADDING * 2
		self.y = PADDING
		self.y += self.text(QRectF(left, self.y, width, 0), 'FaceFusion benchmark', make_font(20, True), TEXT)
		when = runs[0][0].get('when') if runs and runs[0] else ''
		self.text(QRectF(left, PADDING + 8, width, 0), when, make_font(10), MUTED, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
		self.y += 2
		self.y += self.text(QRectF(left, self.y, width, 0), 'FaceFusion %s · tested with StudioFF' % version, make_font(9.5), MUTED)
		self.y += 18

		# hardware
		card_top = self.y
		card_rect = QRectF(left, card_top, width, self.hardware_height(hardware, width))
		self.fill(card_rect, CARD, 10)
		self.y += 14
		for name, value in hardware.items():
			label_height = self.text(QRectF(left + 18, self.y, 130, 0), name, make_font(10), MUTED)
			value_height = self.text(QRectF(left + 150, self.y, width - 168, 0), value, make_font(10), TEXT)
			self.y += max(label_height, value_height) + 6
		self.y = card_rect.bottom() + 18

		for rows in runs:
			self.draw_run(rows, left, width)

		self.y += self.text(QRectF(left, self.y, width, 0), 'FPS: frames processed per second over all timed cycles, higher is faster. Average, fastest and slowest: seconds for one complete run of FaceFusion\'s example video, including reading frames, the processors and writing the video. 1-min clip: about how long a one minute, 30 fps video takes.', make_font(8.5), MUTED)
		return self.y + PADDING

	def hardware_height(self, hardware : Dict[str, str], width : float) -> float:
		measure = ReportPainter(None)
		height = 14.0
		for name, value in hardware.items():
			height += max(measure.text(QRectF(0, 0, 130, 0), name, make_font(10), MUTED), measure.text(QRectF(0, 0, width - 168, 0), value, make_font(10), TEXT)) + 6
		return height + 8

	def draw_run(self, rows : List[Dict[str, Any]], left : float, width : float) -> None:
		first = rows[0]
		self.y += self.text(QRectF(left, self.y, width, 0), format_processors(first), make_font(11, True), TEXT) + 4
		settings = [ 'Provider: %s' % (first.get('provider') or 'default') ]
		if first.get('threads'):
			settings.append('Threads: %s' % first.get('threads'))
		settings += [ 'Mode: %s' % first.get('mode'), 'Cycles: %s' % first.get('cycles'), first.get('when') or '' ]
		self.y += self.text(QRectF(left, self.y, width, 0), '   ·   '.join(settings), make_font(9.5), MUTED) + 10

		row_height = 34
		header_height = 30
		table_rect = QRectF(left, self.y, width, header_height + row_height * len(rows))
		self.fill(table_rect, CARD, 10)
		cell_left = left + 18
		cell_width = width - 36
		for title, position in TABLE_COLUMNS:
			self.text(QRectF(cell_left + cell_width * position, self.y + 8, 140, 0), title, make_font(9, True), MUTED)
		self.y += header_height
		best = max(float(row.get('fps') or 0) for row in rows) or 1.0
		for index, row in enumerate(rows):
			if index:
				self.fill(QRectF(left + 12, self.y, width - 24, 1), LINE)
			fps = float(row.get('fps') or 0)
			# a bar behind the fps value makes the resolutions easy to compare at a glance
			bar_left = cell_left + cell_width * FPS_BAR[0]
			bar_width = cell_width * FPS_BAR[1]
			self.fill(QRectF(bar_left, self.y + 8, bar_width, row_height - 16), QColor(255, 255, 255, 12), 4)
			self.fill(QRectF(bar_left, self.y + 8, max(4.0, bar_width * fps / best), row_height - 16), QColor(ACCENT.red(), ACCENT.green(), ACCENT.blue(), 70), 4)
			cells = [ row.get('resolution') or '', '%.2f' % fps, '%.2f' % float(row.get('average') or 0), '%.2f' % float(row.get('fastest') or 0), '%.2f' % float(row.get('slowest') or 0), '≈ ' + format_seconds(CLIP_FRAMES / fps) if fps > 0 else '' ]
			for column, (text, (_, position)) in enumerate(zip(cells, TABLE_COLUMNS)):
				font = make_font(10.5, column in (0, 1))
				self.text(QRectF(cell_left + cell_width * position + (8 if column == 1 else 0), self.y + 7, 140, 0), text, font, TEXT)
			self.y += row_height
		self.y += 22


def render_report(hardware : Dict[str, str], version : str, entries : List[Dict[str, Any]]) -> QImage:
	"""draws the given results as one image, grouped by run, ready to post"""
	runs : List[List[Dict[str, Any]]] = []
	keys : List[Any] = []
	for entry in entries:
		key = entry.get('run_id') or entry.get('when')
		if key not in keys:
			keys.append(key)
			runs.append([])
		runs[keys.index(key)].append(entry)
	runs = [ sort_by_resolution(rows) for rows in runs ]

	height = ReportPainter(None).draw(hardware, version, runs)
	image = QImage(int(WIDTH * SCALE), int(height * SCALE), QImage.Format.Format_RGB32)
	image.fill(BACKGROUND)
	painter = QPainter(image)
	painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
	painter.scale(SCALE, SCALE)
	ReportPainter(painter).draw(hardware, version, runs)
	painter.end()
	return image
