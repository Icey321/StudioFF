import os
from typing import Optional

from PySide6.QtCore import QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QTableWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, FluentIcon, MessageBox, PrimaryPushButton, ProgressBar, PushButton, SubtitleLabel, TableWidget

from ffstudio.pages.setup import form_row, section_card
from ffstudio.session import Session

SCOPE_LABELS = { 'lite': 'Lite — the default models', 'full': 'Full — every model of every processor' }


def models_dir(root : str) -> str:
	return os.path.join(root, '.assets', 'models')


def format_size(size : int) -> str:
	return '%.2f GB' % (size / 1073741824) if size >= 1073741824 else '%.1f MB' % (size / 1048576)


def model_files(folder : str, name : str) -> list:
	"""the model and the files facefusion keeps beside it, like its .hash checksum"""
	return [ os.path.join(folder, file) for file in os.listdir(folder) if os.path.splitext(file)[0] == name ] if os.path.isdir(folder) else []


class ModelScanner(QThread):
	scanned = Signal(list)

	def __init__(self, folder : str) -> None:
		super().__init__()
		self.folder = folder

	def run(self) -> None:
		files = []
		if os.path.isdir(self.folder):
			for name in os.listdir(self.folder):
				if name.endswith('.onnx'):
					files.append((name, os.path.getsize(os.path.join(self.folder, name))))
		self.scanned.emit(sorted(files))


class ModelsPage(QWidget):
	def __init__(self, session : Session, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.setObjectName('models')
		self.session = session
		self.running = False
		self.scanner : Optional[ModelScanner] = None
		self.files : list = []

		layout = QVBoxLayout(self)
		layout.setContentsMargins(24, 16, 24, 16)
		layout.setSpacing(12)
		layout.addWidget(SubtitleLabel('Models', self))

		card, form = section_card('Download models', 'FaceFusion downloads each model the first time it is used. Download them ahead of time to work offline or to avoid waiting mid-run. The download providers are set in Setup.', self)
		self.scope_combo = ComboBox(card)
		form.addLayout(form_row('Scope', [ self.scope_combo ], card))
		buttons = QHBoxLayout()
		buttons.addSpacing(138)
		self.download_button = PrimaryPushButton(FluentIcon.CLOUD_DOWNLOAD, 'Download models', card)
		self.download_button.clicked.connect(self.download)
		self.stop_button = PushButton(FluentIcon.CLOSE, 'Stop', card)
		self.stop_button.setEnabled(False)
		self.stop_button.clicked.connect(session.runner.stop)
		buttons.addWidget(self.download_button)
		buttons.addWidget(self.stop_button)
		buttons.addStretch(1)
		form.addLayout(buttons)
		progress_row = QHBoxLayout()
		progress_row.addSpacing(138)
		status = QVBoxLayout()
		self.status_label = CaptionLabel('', card)
		self.progress = ProgressBar(card)
		self.progress.setRange(0, 1000)
		status.addWidget(self.status_label)
		status.addWidget(self.progress)
		progress_row.addLayout(status, 1)
		form.addLayout(progress_row)
		layout.addWidget(card)

		list_card, list_layout = section_card('Installed models', '', self)
		header = QHBoxLayout()
		self.summary = BodyLabel('', list_card)
		header.addWidget(self.summary)
		header.addStretch(1)
		self.delete_button = PushButton(FluentIcon.DELETE, 'Delete', list_card)
		self.delete_button.setToolTip('Delete the selected models to free disk space (Delete key). FaceFusion downloads a model again the next time it is used.')
		self.delete_button.setEnabled(False)
		self.delete_button.clicked.connect(self.delete_selected)
		header.addWidget(self.delete_button)
		refresh_button = PushButton(FluentIcon.SYNC, 'Refresh', list_card)
		refresh_button.clicked.connect(self.scan)
		open_button = PushButton(FluentIcon.FOLDER, 'Open folder', list_card)
		open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(models_dir(self.session.facefusion_root))))
		header.addWidget(refresh_button)
		header.addWidget(open_button)
		list_layout.addLayout(header)
		self.table = TableWidget(list_card)
		self.table.setColumnCount(2)
		self.table.setHorizontalHeaderLabels([ 'Model', 'Size' ])
		self.table.verticalHeader().hide()
		self.table.setBorderVisible(True)
		self.table.setBorderRadius(6)
		self.table.horizontalHeader().setStretchLastSection(False)
		self.table.horizontalHeader().setSectionResizeMode(0, self.table.horizontalHeader().ResizeMode.Stretch)
		self.table.setColumnWidth(1, 120)
		self.table.horizontalHeaderItem(1).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
		self.table.setEditTriggers(TableWidget.EditTrigger.NoEditTriggers)
		self.table.setSelectionBehavior(TableWidget.SelectionBehavior.SelectRows)
		self.table.setSelectionMode(TableWidget.SelectionMode.ExtendedSelection)
		self.table.itemSelectionChanged.connect(self.refresh_delete_button)
		QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.table, self.delete_selected, context = Qt.ShortcutContext.WidgetShortcut)
		list_layout.addWidget(self.table, 1)
		layout.addWidget(list_card, 1)

		session.schema_changed.connect(self.refresh_scopes)
		session.busy_changed.connect(self.on_busy_changed)
		session.runner.progress.connect(self.on_progress)
		session.runner.log.connect(self.on_log)
		session.runner.finished.connect(self.on_finished)
		self.refresh_scopes()

	def on_busy_changed(self, busy : bool, _ : str) -> None:
		self.download_button.setEnabled(not busy)
		# a running facefusion may have the models open
		self.refresh_delete_button()

	def showEvent(self, event) -> None:
		super().showEvent(event)
		self.scan()

	def refresh_scopes(self) -> None:
		scopes = (self.session.schema.data.get('download_scopes') if self.session.schema else None) or [ 'lite', 'full' ]
		self.scope_combo.clear()
		for scope in scopes:
			self.scope_combo.addItem(SCOPE_LABELS.get(scope, scope), userData = scope)

	def scan(self) -> None:
		if self.scanner and self.scanner.isRunning():
			return
		self.scanner = ModelScanner(models_dir(self.session.facefusion_root))
		self.scanner.scanned.connect(self.on_scanned)
		self.scanner.start()

	def on_scanned(self, files : list) -> None:
		self.files = files
		self.table.setRowCount(len(files))
		for row, (name, size) in enumerate(files):
			self.table.setItem(row, 0, QTableWidgetItem(name[:-5]))
			size_item = QTableWidgetItem(format_size(size))
			size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
			self.table.setItem(row, 1, size_item)
		total = sum(size for _, size in files)
		self.summary.setText('%d models · %.2f GB' % (len(files), total / 1073741824) if self.session.facefusion_root else 'FaceFusion is not set up yet')
		self.summary.setToolTip(models_dir(self.session.facefusion_root))
		self.refresh_delete_button()

	def selected_models(self) -> list:
		rows = sorted({ index.row() for index in self.table.selectionModel().selectedRows() })
		return [ self.files[row] for row in rows if row < len(self.files) ]

	def refresh_delete_button(self) -> None:
		selected = self.selected_models()
		self.delete_button.setEnabled(bool(selected) and not self.session.runner.is_running())
		self.delete_button.setText('Delete (%s)' % format_size(sum(size for _, size in selected)) if selected else 'Delete')

	def delete_selected(self) -> None:
		selected = self.selected_models()
		if not selected or not self.delete_button.isEnabled():
			return
		total = sum(size for _, size in selected)
		names = [ name[:-5] for name, _ in selected ]
		listing = '\n'.join(names[:12]) + ('\n… and %d more' % (len(names) - 12) if len(names) > 12 else '')
		box = MessageBox('Delete %d model%s?' % (len(names), '' if len(names) == 1 else 's'), '%s\n\nThis frees %s. The files are deleted, not moved to the Recycle Bin. FaceFusion downloads a model again the next time it is used.' % (listing, format_size(total)), self.window())
		box.yesButton.setText('Delete')
		box.cancelButton.setText('Cancel')
		if not box.exec():
			return
		folder = models_dir(self.session.facefusion_root)
		failed = []
		for name in names:
			for path in model_files(folder, name):
				try:
					os.remove(path)
				except OSError:
					failed.append(os.path.basename(path))
		if failed:
			self.session.notify.emit('error', 'Some files could not be deleted', 'They may be in use by FaceFusion: ' + ', '.join(failed[:5]))
		else:
			self.session.notify.emit('success', 'Models deleted', 'Freed %s.' % format_size(total))
		self.scan()

	def download(self) -> None:
		args = [ 'force-download', '--download-scope', self.scope_combo.currentData() or 'lite' ]
		providers = self.session.values.get('download_providers')
		if providers:
			args += [ '--download-providers' ] + list(providers)
		self.running = self.session.run_facefusion('Downloading models', args)
		self.stop_button.setEnabled(self.running)

	def on_progress(self, title : str, ratio : float, status : str) -> None:
		if self.running:
			self.progress.setValue(int(ratio * 1000))
			self.status_label.setText('%s  %d%%  %s' % (title, round(ratio * 100), status))

	def on_log(self, line : str) -> None:
		if self.running:
			self.status_label.setText(line[-140:])

	def on_finished(self, exit_code : int, elapsed : float) -> None:
		if not self.running:
			return
		self.running = False
		self.stop_button.setEnabled(False)
		self.status_label.setText('All models are downloaded' if exit_code == 0 else 'Stopped' if exit_code == -1 else 'Download failed (exit code %d). See Console.' % exit_code)
		if exit_code == 0:
			self.progress.setValue(1000)
		self.scan()
