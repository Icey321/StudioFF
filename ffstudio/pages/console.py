from typing import Optional

from PySide6.QtGui import QFont, QGuiApplication, QTextCursor
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon, PlainTextEdit, PushButton, SubtitleLabel

from ffstudio.session import Session

MAX_BLOCKS = 20000


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
		layout.addWidget(self.output, 1)

		for runner in [ session.runner, session.schema_runner ]:
			runner.started.connect(self.on_started)
			runner.log.connect(self.append)
			runner.finished.connect(self.on_finished)
		session.busy_changed.connect(lambda busy, task: self.task_label.setText(task if busy else 'Idle'))

	def append(self, line : str) -> None:
		self.output.appendPlainText(line)
		self.output.moveCursor(QTextCursor.MoveOperation.End)

	def on_started(self, command : str) -> None:
		self.last_command = command
		self.append('')
		self.append('> ' + command)

	def on_finished(self, exit_code : int, elapsed : float) -> None:
		label = { 0: 'finished', -1: 'stopped' }.get(exit_code, 'failed with exit code %d' % exit_code)
		self.append('— %s after %.1f s' % (label, elapsed))
