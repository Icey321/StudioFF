import os
import re
import time
from collections import deque
from typing import List, Optional, Tuple

from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPaintEvent, QResizeEvent, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtCore import QRect, QStandardPaths, Qt
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, FluentIcon, PlainTextEdit, PushButton, SubtitleLabel, SwitchButton, ToolTipFilter, isDarkTheme, qconfig

from ffstudio.media import format_seconds
from ffstudio.privacy import COMPUTER_NAME, COMPUTER_NAME_STAND_IN, FOLDERS_STAND_IN, USER_NAME, USER_NAME_STAND_IN, FileNames, hide_private, program_folders
from ffstudio.session import EXCEPTION_PATTERN, Session, describe_error, is_error_line

MAX_BLOCKS = 20000
# the plain explanation under a failed command's last line
WHY_PREFIX = '  Why: '
FIX_PREFIX = '  Fix: '
# VS Code's Light+ and Dark+ colours, (light, dark)
COLORS ={
	'keyword': ('#0000ff', '#569cd6'),
	'function': ('#795e26', '#dcdcaa'),
	'flag': ('#001080', '#9cdcfe'),
	'string': ('#a31515', '#ce9178'),
	'number': ('#098658', '#b5cea8'),
	'tag': ('#267f99', '#4ec9b0'),
	'muted': ('#008000', '#6a9955'),
	'link': ('#006ab1', '#3794ff'),
	'success': ('#388a34', '#89d185'),
	'warning': ('#bf8803', '#cca700'),
	'error': ('#cd3131', '#f48771')
}
COMMAND_TOKEN_PATTERN = re.compile(r'"[^"]*"|\S+')
TAG_PATTERN = re.compile(r'^\[[\w.\-]+\]')
NUMBER_PATTERN = re.compile(r'(?<![\w.])\d+(?:\.\d+)*%?')
# both numbers of a resolution, 428x500: NUMBER_PATTERN takes the 500 for part of a word
RESOLUTION_PATTERN = re.compile(r'(?<![\w.])(\d+)x(\d+)(?!\w|\.\d)')
STRING_PATTERN = re.compile(r'"[^"]*"|(?<!\w)\'[^\']*\'')
# C:\ or C:/ paths, and /home/... paths of linux and macos. not the slashes of 12.3it/s or a url
PATH_PATTERN = re.compile(r'(?<![\w"\'])[A-Za-z]:[\\/][^\s"\'<>|,;]*|(?<![\w"\'/:.])/[\w.\-][^\s"\'<>|,;]*')
URL_PATTERN = re.compile(r'https?://[^\s"\'<>]+')
SUCCESS_PATTERN = re.compile(r'\b(?:[Ss]uccessfully|[Ff]inished|[Cc]ompleted|[Dd]one)\b')
FUNCTION_PATTERN = re.compile(r'(?<=, in )[\w<>.]+$')
WARNING_PATTERN = re.compile(r'\b\w*Warning\b|^WARNING\b|^warning:')
TOOL_ERROR_PATTERN = re.compile(r'^(?:ERROR|error|fatal)\b:?')
# a finished facefusion stage: · Extracting  3.2 s  1240/1240 frames
STAGE_PREFIX = '  · '
# the line number column, (light, dark)
GUTTER_COLORS = { 'background': ('#f0f0f0', '#2b2b2b'), 'border': ('#dcdcdc', '#3a3a3a'), 'number': ('#9a9a9a', '#7a7a7a') }


def stage_name(title : str) -> str:
	"""facefusion names its stages in lower case, extracting reads Extracting"""
	return title[:1].upper() + title[1:]


class LineNumbers(QWidget):
	"""the numbers left of the log, one per log line however many rows it wraps to"""

	def __init__(self, editor : 'LogView') -> None:
		super().__init__(editor)
		self.editor = editor

	def paintEvent(self, event : QPaintEvent) -> None:
		editor = self.editor
		index = 1 if isDarkTheme() else 0
		painter = QPainter(self)
		painter.fillRect(event.rect(), QColor(GUTTER_COLORS['background'][index]))
		painter.setPen(QColor(GUTTER_COLORS['border'][index]))
		painter.drawLine(self.width() - 1, event.rect().top(), self.width() - 1, event.rect().bottom())
		painter.setFont(editor.font())
		painter.setPen(QColor(GUTTER_COLORS['number'][index]))
		height = editor.fontMetrics().height()
		block = editor.firstVisibleBlock()
		top = editor.blockBoundingGeometry(block).translated(editor.contentOffset()).top()
		while block.isValid() and top <= event.rect().bottom():
			if block.isVisible():
				painter.drawText(QRect(0, int(top), self.width() - 8, height), Qt.AlignmentFlag.AlignRight, str(editor.line_number(block.blockNumber())))
			top += editor.blockBoundingRect(block).height()
			block = block.next()


class LogView(PlainTextEdit):
	"""the log with its line numbers, counted from the last Clear: lines the log drops keep their count, so a number told to someone still points at the same line"""

	def __init__(self, parent : QWidget) -> None:
		super().__init__(parent)
		# lines dropped off the top since the last Clear
		self.dropped = 0
		self.numbers = LineNumbers(self)
		self.blockCountChanged.connect(lambda _: self.place_numbers())
		self.updateRequest.connect(self.on_update_request)
		qconfig.themeChanged.connect(self.numbers.update)
		self.place_numbers()

	def line_number(self, block_number : int) -> int:
		return self.dropped + block_number + 1

	def numbers_width(self) -> int:
		digits = max(3, len(str(self.line_number(self.blockCount()))))
		return self.fontMetrics().horizontalAdvance('9' * digits) + 16

	def place_numbers(self) -> None:
		width = self.numbers_width()
		self.setViewportMargins(width, 0, 0, 0)
		rect = self.contentsRect()
		self.numbers.setGeometry(rect.left(), rect.top(), width, rect.height())

	def on_update_request(self, rect : QRect, dy : int) -> None:
		if dy:
			self.numbers.scroll(0, dy)
		else:
			self.numbers.update(0, rect.y(), self.numbers.width(), rect.height())

	def resizeEvent(self, event : QResizeEvent) -> None:
		super().resizeEvent(event)
		self.place_numbers()

	def numbered_text(self) -> str:
		"""the log as Save Log writes it, each line with its number: '  14 | text'"""
		lines = self.toPlainText().split('\n')
		width = len(str(self.line_number(len(lines) - 1)))
		return '\n'.join('%*d | %s' % (width, self.line_number(index), line) for index, line in enumerate(lines))


class ConsoleHighlighter(QSyntaxHighlighter):
	"""colours the log the way VS Code colours code, picked again when the theme changes"""

	def __init__(self, document : QTextDocument) -> None:
		super().__init__(document)
		self.formats = {}
		self.load_formats()
		qconfig.themeChanged.connect(self.on_theme_changed)

	def load_formats(self) -> None:
		index = 1 if isDarkTheme() else 0
		for key, colors in COLORS.items():
			text_format = QTextCharFormat()
			text_format.setForeground(QColor(colors[index]))
			if key == 'error':
				text_format.setFontWeight(600)
			if key == 'link':
				text_format.setFontUnderline(True)
			self.formats[key] = text_format

	def on_theme_changed(self) -> None:
		self.load_formats()
		self.rehighlight()

	def paint(self, pattern : re.Pattern, text : str, key : str) -> None:
		for match in pattern.finditer(text):
			self.setFormat(match.start(), match.end() - match.start(), self.formats.get(key))

	def highlightBlock(self, text : str) -> None:
		if text.startswith('> '):
			self.setFormat(0, 1, self.formats.get('keyword'))
			for index, match in enumerate(COMMAND_TOKEN_PATTERN.finditer(text, 2)):
				token = match.group()
				key = 'function' if index == 0 else 'flag' if token.startswith('-') else 'number' if NUMBER_PATTERN.fullmatch(token) else 'string'
				self.setFormat(match.start(), len(token), self.formats.get(key))
			return
		if text.startswith('— '):
			key = 'success' if ' finished ' in text else 'warning' if ' stopped ' in text else 'error'
			self.setFormat(0, len(text), self.formats.get(key))
			return
		if text.startswith(STAGE_PREFIX):
			self.setFormat(0, len(text), self.formats.get('muted'))
			return
		if text.startswith(WHY_PREFIX) or text.startswith(FIX_PREFIX):
			self.setFormat(0, len(text), self.formats.get('error' if text.startswith(WHY_PREFIX) else 'keyword'))
			return
		if is_error_line(text) or TOOL_ERROR_PATTERN.match(text):
			self.setFormat(0, len(text), self.formats.get('error'))
			return
		if text.startswith('Traceback '):
			self.setFormat(0, len(text), self.formats.get('keyword'))
			return
		warning = WARNING_PATTERN.search(text)
		if warning:
			self.setFormat(0, len(text), self.formats.get('warning'))
		else:
			self.paint(NUMBER_PATTERN, text, 'number')
			for match in RESOLUTION_PATTERN.finditer(text):
				for group in [ 1, 2 ]:
					self.setFormat(match.start(group), match.end(group) - match.start(group), self.formats.get('number'))
			self.paint(SUCCESS_PATTERN, text, 'success')
		self.paint(STRING_PATTERN, text, 'string')
		self.paint(PATH_PATTERN, text, 'string')
		self.paint(FUNCTION_PATTERN, text, 'function')
		self.paint(URL_PATTERN, text, 'link')
		self.paint(TAG_PATTERN, text, 'tag')


class ConsolePage(QWidget):
	def __init__(self, session : Session, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.setObjectName('console')
		self.session = session
		self.last_command = ''
		# the lines as they came, unmasked, as many as the log keeps
		self.lines : deque = deque(maxlen = MAX_BLOCKS)
		self.program_folder_paths : Tuple[str, str] = ('', '')
		self.program_folder_list = program_folders()

		layout = QVBoxLayout(self)
		layout.setContentsMargins(24, 16, 24, 16)
		layout.setSpacing(12)
		header = QHBoxLayout()
		header.addWidget(SubtitleLabel('Console', self))
		self.task_label = CaptionLabel('Idle', self)
		self.task_label.setTextColor('#707070', '#9a9a9a')
		header.addWidget(self.task_label)
		header.addStretch(1)
		self.privacy_switch = self.add_switch(header, 'Hide User Name', 'console_hide_user_name', 'Shows %s in place of your user name (%s) and %s in place of your computer name (%s), in the log, Copy Last Command and Save Log' % (USER_NAME_STAND_IN, USER_NAME, COMPUTER_NAME_STAND_IN, COMPUTER_NAME))
		self.folders_switch = self.add_switch(header, 'Hide Folders', 'console_hide_folders', 'Shows %s in place of the folders of your own files, M:\\Clients\\Smith\\photo.png reads M:\\%s\\photo.png. The folders of StudioFF, FaceFusion and Python stay, they help find a problem' % (FOLDERS_STAND_IN, FOLDERS_STAND_IN))
		self.files_switch = self.add_switch(header, 'Hide File Names', 'console_hide_file_names', 'Shows file 1, file 2 and so on in place of the names of your own files, keeping the file type: Smith wedding.jpg reads file 1.jpg, the same each time it comes. The files of StudioFF, FaceFusion and Python keep their names')
		self.file_names = FileNames()
		copy_button = PushButton(FluentIcon.COPY, 'Copy Last Command', self)
		copy_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.shown(self.last_command)))
		save_button = PushButton(FluentIcon.SAVE, 'Save Log', self)
		save_button.setToolTip('Saves the log as a text file, as shown here: with names, folders and file names hidden if those are on')
		save_button.installEventFilter(ToolTipFilter(save_button))
		save_button.clicked.connect(self.save_log)
		clear_button = PushButton(FluentIcon.DELETE, 'Clear', self)
		clear_button.clicked.connect(self.clear)
		for button in [ copy_button, save_button, clear_button ]:
			# the buttons keep their full width, the gray task text gives way when the window is narrow
			button.setMinimumWidth(button.sizeHint().width())
			header.addWidget(button)
		self.task_label.setMinimumWidth(0)
		self.task_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		layout.addLayout(header)

		self.output = LogView(self)
		self.output.setReadOnly(True)
		self.output.setMaximumBlockCount(MAX_BLOCKS)
		self.output.setLineWrapMode(PlainTextEdit.LineWrapMode.WidgetWidth)
		font = QFont('Cascadia Mono')
		font.setStyleHint(QFont.StyleHint.Monospace)
		font.setPointSize(9)
		self.output.setFont(font)
		self.highlighter = ConsoleHighlighter(self.output.document())
		layout.addWidget(self.output, 1)

		# per runner, the schema can be read while facefusion runs: the task name and the error lines of its current command
		# and the stage facefusion is on: its name, when it began, how far it got and its last status
		self.runs = { session.runner: { 'task': '', 'errors': [], 'stage': None }, session.schema_runner: { 'task': "Reading FaceFusion's options", 'errors': [], 'stage': None } }
		for runner in [ session.runner, session.schema_runner ]:
			runner.started.connect(self.on_started)
			runner.log.connect(self.on_log)
			runner.progress.connect(self.on_progress)
			runner.finished.connect(self.on_finished)
		self.task_text = 'Idle'
		session.busy_changed.connect(lambda busy, task: self.show_task(task if busy else 'Idle'))

	def add_switch(self, header : QHBoxLayout, text : str, key : str, tip : str) -> SwitchButton:
		label = BodyLabel(text, self)
		label.setMinimumWidth(label.sizeHint().width())
		label.setToolTip(tip)
		label.installEventFilter(ToolTipFilter(label))
		switch = SwitchButton(self)
		# no On / Off words beside the switch, the header has no room for them
		switch.label.hide()
		switch.setSpacing(0)
		switch.setChecked(bool(self.session.config.get(key, False)))
		switch.checkedChanged.connect(lambda checked: self.on_privacy_changed(key, checked))
		header.addWidget(label)
		header.addWidget(switch)
		header.addSpacing(8)
		return switch

	def shown(self, text : str) -> str:
		names = self.privacy_switch.isChecked()
		folders = self.folders_switch.isChecked()
		files = self.file_names if self.files_switch.isChecked() else None
		if not names and not folders and files is None:
			return text
		return hide_private(text, names, folders, self.program_folders, files)

	def show_task(self, task : str) -> None:
		self.task_text = task
		# the task can name a file, NSFW check: photo.png
		self.task_label.setText(self.shown(task))

	def clear(self) -> None:
		self.lines.clear()
		self.file_names.clear()
		self.output.dropped = 0
		self.output.clear()
		self.output.place_numbers()

	def save_log(self) -> None:
		folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
		name = 'StudioFF log %s.txt' % time.strftime('%Y-%m-%d %H-%M')
		path, _ = QFileDialog.getSaveFileName(self, 'Save Log', os.path.join(folder, name), 'Text files (*.txt)')
		if not path:
			return
		try:
			with open(path, 'w', encoding = 'utf-8') as log_file:
				log_file.write(self.output.numbered_text() + '\n')
		except OSError as error:
			self.session.notify.emit('error', 'Log not saved', str(error))
			return
		self.session.notify.emit('success', 'Log saved', os.path.basename(path))

	@property
	def program_folders(self) -> List[str]:
		# read again each time, FaceFusion's folder or its Python can change on the FaceFusion page
		paths = (self.session.facefusion_root, self.session.python_path)
		if paths != self.program_folder_paths:
			self.program_folder_paths = paths
			self.program_folder_list = program_folders(*paths)
		return self.program_folder_list

	def on_privacy_changed(self, key : str, checked : bool) -> None:
		self.session.config.set(key, checked)
		self.session.config.save()
		# the log as it came, so the switch works both ways on lines already shown
		# appendPlainText skips blank lines at the very start, so the same goes here
		self.output.setPlainText('\n'.join(self.shown(line) for line in self.lines).lstrip('\n'))
		self.output.moveCursor(QTextCursor.MoveOperation.End)
		self.output.moveCursor(QTextCursor.MoveOperation.StartOfBlock)
		self.show_task(self.task_text)

	def append(self, line : str) -> None:
		if len(self.lines) == self.lines.maxlen:
			# the log drops its first line now, the numbers of the others stay
			self.output.dropped += 1
		self.lines.append(line)
		self.output.appendPlainText(self.shown(line))
		self.output.moveCursor(QTextCursor.MoveOperation.End)
		# back to the left edge, a long line before would keep the start of this one out of view
		self.output.moveCursor(QTextCursor.MoveOperation.StartOfBlock)

	def on_started(self, command : str) -> None:
		self.last_command = command
		run = self.runs.get(self.sender())
		if run is not None:
			run['errors'] = []
			run['stage'] = None
			# set_task comes before the runner starts
			if self.sender() is self.session.runner:
				run['task'] = self.session.task
		self.append('')
		self.append('> ' + command)

	def on_log(self, line : str) -> None:
		run = self.runs.get(self.sender())
		if run is not None and (is_error_line(line) or TOOL_ERROR_PATTERN.match(line)):
			run['errors'].append(line)
		self.append(line)

	def on_progress(self, title : str, ratio : float, status : str) -> None:
		run = self.runs.get(self.sender())
		if run is None or not title:
			return
		stage = run['stage']
		if stage is None or stage['title'] != title:
			self.end_stage(run)
			run['stage'] = { 'title': title, 'start': time.monotonic(), 'ratio': ratio, 'status': status }
			return
		stage['ratio'] = ratio
		stage['status'] = status

	def end_stage(self, run : dict) -> None:
		"""one line for the stage facefusion just left: how long it took and its last status"""
		stage = run.get('stage')
		if stage is None:
			return
		run['stage'] = None
		status = stage['status'].replace('  ·  ', '  ')
		self.append('%s%s  %s%s' % (STAGE_PREFIX, stage_name(stage['title']), format_seconds(time.monotonic() - stage['start']), '  ' + status if status else ''))

	def on_finished(self, exit_code : int, elapsed : float) -> None:
		run = self.runs.get(self.sender()) or { 'task': '', 'errors': [], 'stage': None }
		# the stage it failed in, unless it got to the end of it
		stage = run.get('stage')
		failed_stage = stage_name(stage['title']) if stage and stage['ratio'] < 1.0 else ''
		self.end_stage(run)
		if exit_code in [ 0, -1 ]:
			self.append('— %s after %s' % ('finished' if exit_code == 0 else 'stopped', format_seconds(elapsed)))
			return
		during = ' while %s' % failed_stage if failed_stage else ''
		self.append('— %s failed%s after %s (exit code %d)' % (run['task'] or 'The command', during, format_seconds(elapsed), exit_code))
		why, fix = self.explain_failure(run['errors'], self.sender() is self.session.runner)
		self.append(WHY_PREFIX + why)
		if fix:
			self.append(FIX_PREFIX + fix)

	def explain_failure(self, error_lines : List[str], facefusion_run : bool) -> Tuple[str, str]:
		# a python exception says the most, then facefusion's own error lines, then a tool's
		exceptions = [ line for line in error_lines if EXCEPTION_PATTERN.match(line) ]
		line = (exceptions or error_lines or [ '' ])[-1]
		described = describe_error(line) if line else None
		if described:
			return described
		if facefusion_run and self.session.nsfw_block_is_certain():
			return self.session.nsfw_block_message(False), 'Pick another target. Pre-Check tells you before a run.'
		if line:
			return line, ''
		return 'It stopped without giving a reason.', 'The lines above are the whole log. If the target is an image, the NSFW filter may have refused it: try Pre-Check.'
