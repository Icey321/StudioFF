import re
from typing import Optional

from PySide6.QtGui import QColor, QFont, QGuiApplication, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon, PlainTextEdit, PushButton, SubtitleLabel, isDarkTheme, qconfig

from ffstudio.session import Session, is_error_line

MAX_BLOCKS = 20000
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
STRING_PATTERN = re.compile(r'"[^"]*"|(?<!\w)\'[^\']*\'')
PATH_PATTERN = re.compile(r'(?<![\w"\'])[A-Za-z]:[\\/][^\s"\'<>|,;]*')
URL_PATTERN = re.compile(r'https?://[^\s"\'<>]+')
SUCCESS_PATTERN = re.compile(r'\b(?:[Ss]uccessfully|[Ff]inished|[Cc]ompleted|[Dd]one)\b')
FUNCTION_PATTERN = re.compile(r'(?<=, in )[\w<>.]+$')
WARNING_PATTERN = re.compile(r'\b\w*Warning\b|^WARNING\b|^warning:')
TOOL_ERROR_PATTERN = re.compile(r'^(?:ERROR|error|fatal)\b:?')


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

		layout = QVBoxLayout(self)
		layout.setContentsMargins(24, 16, 24, 16)
		layout.setSpacing(12)
		header = QHBoxLayout()
		header.addWidget(SubtitleLabel('Console', self))
		self.task_label = CaptionLabel('Idle', self)
		self.task_label.setTextColor('#707070', '#9a9a9a')
		header.addWidget(self.task_label)
		header.addStretch(1)
		copy_button = PushButton(FluentIcon.COPY, 'Copy last command', self)
		copy_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.last_command))
		clear_button = PushButton(FluentIcon.DELETE, 'Clear', self)
		clear_button.clicked.connect(lambda: self.output.clear())
		header.addWidget(copy_button)
		header.addWidget(clear_button)
		layout.addLayout(header)

		self.output = PlainTextEdit(self)
		self.output.setReadOnly(True)
		self.output.setMaximumBlockCount(MAX_BLOCKS)
		self.output.setLineWrapMode(PlainTextEdit.LineWrapMode.NoWrap)
		font = QFont('Cascadia Mono')
		font.setStyleHint(QFont.StyleHint.Monospace)
		font.setPointSize(9)
		self.output.setFont(font)
		self.highlighter = ConsoleHighlighter(self.output.document())
		layout.addWidget(self.output, 1)

		for runner in [ session.runner, session.schema_runner ]:
			runner.started.connect(self.on_started)
			runner.log.connect(self.append)
			runner.finished.connect(self.on_finished)
		session.busy_changed.connect(lambda busy, task: self.task_label.setText(task if busy else 'Idle'))

	def append(self, line : str) -> None:
		self.output.appendPlainText(line)
		self.output.moveCursor(QTextCursor.MoveOperation.End)
		# back to the left edge, a long line before would keep the start of this one out of view
		self.output.moveCursor(QTextCursor.MoveOperation.StartOfBlock)

	def on_started(self, command : str) -> None:
		self.last_command = command
		self.append('')
		self.append('> ' + command)

	def on_finished(self, exit_code : int, elapsed : float) -> None:
		label = { 0: 'finished', -1: 'stopped' }.get(exit_code, 'failed with exit code %d' % exit_code)
		self.append('— %s after %.1f s' % (label, elapsed))
