from typing import Any, Dict, List, Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath

WIDTH = 900
SCALE = 2
PADDING = 32
BACKGROUND = QColor('#16181d')
CARD = QColor('#1f2229')
LINE = QColor('#2c303a')
TEXT = QColor('#eceef2')
MUTED = QColor('#9aa1ad')
ACCENT = QColor('#4cc2ff')
TABLE_COLUMNS = [ ('Resolution', 0.0), ('FPS', 0.17), ('Average (sec)', 0.47), ('Fastest (sec)', 0.64), ('Slowest (sec)', 0.81) ]


RESOLUTIONS = [ '240p', '360p', '540p', '720p', '1080p', '1440p', '2160p' ]


def make_font(size : float, bold : bool = False) -> QFont:
	# pixel sizes, so measuring and drawing agree whatever resolution the image reports
	font = QFont('Segoe UI')
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

		self.y += self.text(QRectF(left, self.y, width, 0), 'FPS: frames processed per second over all timed cycles, higher is faster. Average, fastest and slowest: seconds for one complete run of FaceFusion\'s example video, including reading frames, the processors and writing the video.', make_font(8.5), MUTED)
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
		settings += [ 'Mode: %s' % first.get('mode'), 'Cycles: %s' % first.get('cycles') ]
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
			bar_left = cell_left + cell_width * 0.17
			bar_width = cell_width * 0.27
			self.fill(QRectF(bar_left, self.y + 8, bar_width, row_height - 16), QColor(255, 255, 255, 12), 4)
			self.fill(QRectF(bar_left, self.y + 8, max(4.0, bar_width * fps / best), row_height - 16), QColor(ACCENT.red(), ACCENT.green(), ACCENT.blue(), 70), 4)
			cells = [ row.get('resolution') or '', '%.2f' % fps, '%.2f' % float(row.get('average') or 0), '%.2f' % float(row.get('fastest') or 0), '%.2f' % float(row.get('slowest') or 0) ]
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
