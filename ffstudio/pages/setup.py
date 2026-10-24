import os
import shutil
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractButton, QApplication, QButtonGroup, QFileDialog, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, CheckBox, ColorPickerButton, ComboBox, EditableComboBox, FluentIcon, HyperlinkButton, IconWidget, InfoBarIcon, LineEdit, MessageBox, PrimaryPushButton, ProgressBar, PushButton, SearchLineEdit, SimpleCardWidget, SingleDirectionScrollArea, StrongBodyLabel, SubtitleLabel, SwitchButton, ToolTipFilter, TransparentToolButton

from ffstudio import APP_REPOSITORY, APP_VERSION
from ffstudio.environment import check_dependencies, create_process_environment, fetch_github_build, fetch_latest_release, find_conda, find_conda_base, find_python_candidates, has_nvidia_gpu, is_facefusion_root, is_newer_version, probe_python_version, read_facefusion_build, read_facefusion_version, read_windows_version, version_tuple
from ffstudio.main_window import DEFAULT_ACCENT, apply_theme
from ffstudio.option_panel import OptionPanel
from ffstudio.session import MINIMUM_PYTHON, Session
from ffstudio.updater import download_release, is_git_checkout, start_helper
from ffstudio.widgets import ChoiceButton

# the theme buttons: key, text, icon and tooltip
# minutes, 0 keeps them until the app closes
KEEP_LOADED_CHOICES = [ (5, '5 minutes'), (10, '10 minutes'), (30, '30 minutes'), (60, '1 hour'), (0, 'Until the app closes') ]
THEME_CHOICES =[
	('auto', 'Auto', FluentIcon.CONSTRACT, 'Follow the Windows light or dark setting'),
	('light', 'Light', FluentIcon.BRIGHTNESS, 'Always light'),
	('dark', 'Dark', FluentIcon.QUIET_HOURS, 'Always dark')
]
SYSTEM_GROUPS = [ 'execution', 'memory', 'download', 'misc', 'paths' ]
REPOSITORY_URL = 'https://github.com/facefusion/facefusion.git'
DEFAULT_REPOSITORY = 'facefusion/facefusion'
CONDA_ENV_NAME = 'facefusion-v4'
ONNXRUNTIME_CHOICES =\
[
	('cuda@12', 'CUDA 12 — NVIDIA GPU'),
	('cuda@13', 'CUDA 13 — NVIDIA GPU, newest drivers'),
	('directml', 'DirectML — any DirectX 12 GPU'),
	('openvino', 'OpenVINO — Intel CPU / GPU'),
	('default', 'CPU only')
]


class StatusLine(QWidget):
	def __init__(self, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		layout = QHBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(6)
		self.icon = IconWidget(InfoBarIcon.INFORMATION, self)
		self.icon.setFixedSize(14, 14)
		self.label = CaptionLabel('', self)
		self.label.setWordWrap(True)
		self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		layout.addWidget(self.icon)
		layout.addWidget(self.label, 1)

	def set_status(self, level : str, text : str) -> None:
		self.icon.setIcon({ 'success': InfoBarIcon.SUCCESS, 'warning': InfoBarIcon.WARNING, 'error': InfoBarIcon.ERROR }.get(level, InfoBarIcon.INFORMATION))
		self.label.setText(text)


def form_row(label : str, widgets : List[QWidget], parent : QWidget) -> QHBoxLayout:
	row = QHBoxLayout()
	row.setSpacing(8)
	caption = BodyLabel(label, parent)
	caption.setFixedWidth(130)
	row.addWidget(caption)
	# a first widget with a maximum width would sit centred in the space it cannot fill, so pack the row to the left instead
	limited = bool(widgets) and widgets[0].maximumWidth() < 16777215
	for index, widget in enumerate(widgets):
		row.addWidget(widget, 1 if index == 0 and not limited else 0)
	if limited:
		row.addStretch(1)
	return row


def format_build(version : str, branch : str, commit : str, date : str) -> str:
	parts = [ version or branch or 'unknown version' ]
	if branch and branch not in (version, 'HEAD'):
		parts.append('%s branch' % branch)
	if commit:
		parts.append('commit ' + commit[:7])
	if date:
		parts.append(date[:10])
	return '  ·  '.join(parts)


def github_page(repository : str) -> str:
	"""web page of the checkout's own repository, so a fork links to the fork"""
	return 'https://github.com/' + (repository or DEFAULT_REPOSITORY)


def section_card(title : str, description : str, parent : QWidget) -> Tuple[QWidget, QVBoxLayout]:
	# a section inside a group card draws no card of its own, so the group reads as one
	card = QWidget(parent) if isinstance(parent, SimpleCardWidget) else SimpleCardWidget(parent)
	layout = QVBoxLayout(card)
	layout.setContentsMargins(20, 16, 20, 18)
	layout.setSpacing(10)
	layout.addWidget(StrongBodyLabel(title, card))
	if description:
		caption = CaptionLabel(description, card)
		caption.setWordWrap(True)
		caption.setTextColor('#606060', '#a0a0a0')
		layout.addWidget(caption)
	return card, layout


class SetupPage(QWidget):
	# the github answer arrives from a worker thread
	update_checked = Signal(object)
	studio_update_checked = Signal(object, bool)
	# true while github has a newer StudioFF release than this one
	studio_update_found = Signal(bool)
	studio_download_progress = Signal(int, int)
	# the folder of the downloaded files, or an error
	studio_download_finished = Signal(str, str)
	# probes that start python or git, they run on worker threads so the window keeps responding
	python_probed = Signal(str, object)
	pythons_detected = Signal(object, str)
	build_read = Signal(str, object)

	def __init__(self, session : Session, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.setObjectName('setup')
		self.session = session
		self.install_steps : List[Tuple[str, Callable[[], bool]]] = []
		self.install_index = 0

		root = QVBoxLayout(self)
		root.setContentsMargins(0, 0, 0, 0)
		root.setSpacing(0)
		header = QHBoxLayout()
		header.setContentsMargins(24, 16, 34, 8)
		header.addWidget(SubtitleLabel('Setup', self))
		header.addStretch(1)
		root.addLayout(header)
		# the search box spans the settings below it, as wide as the cards
		self.search_edit = SearchLineEdit(self)
		self.search_edit.setPlaceholderText('Search settings')
		self.search_edit.setMaximumWidth(1280 - 48)
		self.search_edit.textChanged.connect(self.apply_filter)
		search_row = QHBoxLayout()
		search_row.setContentsMargins(24, 4, 34, 10)
		search_row.addWidget(self.search_edit, 1)
		# on a wide window the leftover space goes to the right of the box, so it stays lined up with the cards
		search_row.addStretch(0)
		root.addLayout(search_row)

		self.scroll = SingleDirectionScrollArea(self, Qt.Orientation.Vertical)
		self.scroll.setWidgetResizable(True)
		self.scroll.enableTransparentBackground()
		self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
		root.addWidget(self.scroll, 1)

		content = QWidget()
		content.setMaximumWidth(1280)
		content.setStyleSheet('background: transparent')
		# clicking empty space takes focus away from a control, so the wheel scrolls the page again
		content.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
		layout = QVBoxLayout(content)
		layout.setContentsMargins(24, 8, 24, 24)
		layout.setSpacing(12)
		# the FaceFusion sections share one card, apart from the StudioFF updates below them
		self.facefusion_group = SimpleCardWidget(content)
		group_layout = QVBoxLayout(self.facefusion_group)
		group_layout.setContentsMargins(0, 0, 0, 0)
		group_layout.setSpacing(12)
		self.facefusion_cards = [ self.create_location_card(self.facefusion_group), self.create_install_card(self.facefusion_group), self.create_update_card(self.facefusion_group) ]
		for card in self.facefusion_cards:
			group_layout.addWidget(card)
		layout.addWidget(self.facefusion_group)
		self.section_cards = self.facefusion_cards + [ self.create_studio_update_card(content), self.create_appearance_card(content) ]
		for card in self.section_cards[len(self.facefusion_cards):]:
			layout.addWidget(card)
		self.system_title = StrongBodyLabel('Performance & system', content)
		layout.addSpacing(6)
		layout.addWidget(self.system_title)
		self.system_panel = OptionPanel(session, SYSTEM_GROUPS, expanded = [ 'execution' ], parent = content, info_icons = True, expansion_key = 'setup_groups_expanded')
		layout.addWidget(self.system_panel)
		self.no_match_label = CaptionLabel('No settings match your search.', content)
		self.no_match_label.setTextColor('#707070', '#9a9a9a')
		self.no_match_label.hide()
		layout.addWidget(self.no_match_label)
		layout.addStretch(1)
		self.scroll.setWidget(content)

		session.schema_changed.connect(self.system_panel.rebuild)
		session.schema_changed.connect(self.refresh_status)
		session.runner.finished.connect(self.on_runner_finished)
		self.update_checked.connect(self.on_update_checked)
		self.studio_update_checked.connect(self.on_studio_update_checked)
		self.studio_download_progress.connect(self.on_studio_download_progress)
		self.studio_download_finished.connect(self.on_studio_download_finished)
		self.python_probed.connect(self.on_python_probed)
		self.pythons_detected.connect(self.on_pythons_detected)
		self.build_read.connect(self.show_installed_build)
		self.refresh_status()

	# search

	@staticmethod
	def card_text(card : QWidget) -> str:
		texts = [ label.text() for label in card.findChildren(QLabel) ]
		texts += [ button.text() for button in card.findChildren(QAbstractButton) ]
		for combo in card.findChildren(ComboBox):
			texts += [ combo.itemText(index) for index in range(combo.count()) ]
		return ' '.join(texts).lower()

	def apply_filter(self, text : str) -> None:
		text = text.strip().lower()
		for card in self.section_cards:
			haystack = self.card_text(card)
			card.setVisible(all(word in haystack for word in text.split()))
		self.facefusion_group.setVisible(not all(card.isHidden() for card in self.facefusion_cards))
		self.system_panel.set_filter(text)
		has_system = self.system_panel.has_visible_cards()
		self.system_title.setVisible(has_system)
		self.no_match_label.setVisible(bool(text) and not has_system and all(card.isHidden() for card in self.section_cards))
		self.scroll.verticalScrollBar().setValue(0)

	# location

	def create_location_card(self, parent : QWidget) -> QWidget:
		card, layout = section_card('FaceFusion v4 location', 'FaceFusion v4 has no built-in UI. This app drives its command line, so it needs the FaceFusion folder and a Python environment with Python 3.13 or newer and the FaceFusion requirements installed.', parent)
		self.root_edit = LineEdit(card)
		self.root_edit.setPlaceholderText('Folder that contains facefusion.py')
		self.root_edit.setText(self.session.facefusion_root)
		self.root_edit.editingFinished.connect(self.on_root_edited)
		root_browse = PushButton(FluentIcon.FOLDER, 'Browse', card)
		root_browse.clicked.connect(self.browse_root)
		layout.addLayout(form_row('FaceFusion folder', [ self.root_edit, root_browse ], card))
		self.root_status = StatusLine(card)
		layout.addLayout(form_row('', [ self.root_status ], card))

		self.python_combo = EditableComboBox(card)
		self.python_combo.setPlaceholderText('python.exe of the FaceFusion environment')
		self.python_combo.setText(self.session.python_path)
		python_browse = PushButton(FluentIcon.FOLDER, 'Browse', card)
		python_browse.clicked.connect(self.browse_python)
		detect_button = PushButton(FluentIcon.SEARCH, 'Detect', card)
		detect_button.clicked.connect(self.detect_pythons)
		layout.addLayout(form_row('Python', [ self.python_combo, python_browse, detect_button ], card))
		self.python_status = StatusLine(card)
		layout.addLayout(form_row('', [ self.python_status ], card))

		buttons = QHBoxLayout()
		buttons.addSpacing(138)
		self.apply_button = PrimaryPushButton(FluentIcon.ACCEPT, 'Save and load options', card)
		self.apply_button.clicked.connect(self.apply_location)
		self.schema_status = CaptionLabel('', card)
		self.schema_status.setTextColor('#606060', '#a0a0a0')
		self.schema_status.setWordWrap(True)
		self.schema_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		buttons.addWidget(self.apply_button)
		buttons.addSpacing(8)
		buttons.addWidget(self.schema_status, 1)
		layout.addLayout(buttons)
		return card

	def browse_root(self) -> None:
		path = QFileDialog.getExistingDirectory(self, 'Choose the FaceFusion folder', self.root_edit.text() or os.path.expanduser('~'))
		if path:
			self.root_edit.setText(os.path.normpath(path))
			self.on_root_edited()

	def browse_python(self) -> None:
		path, _ = QFileDialog.getOpenFileName(self, 'Choose python.exe', os.path.dirname(self.python_combo.text()) or os.path.expanduser('~'), 'Python (python.exe);;All files (*)')
		if path:
			self.python_combo.setText(os.path.normpath(path))
			self.refresh_python_status()

	def on_root_edited(self) -> None:
		self.refresh_root_status()
		if not self.python_combo.text().strip():
			self.detect_pythons()

	def detect_pythons(self) -> None:
		root, current = self.root_edit.text().strip(), self.python_combo.text()
		self.python_status.set_status('info', 'Looking for Python environments…')

		def probe() -> None:
			candidates = find_python_candidates(root)
			self.pythons_detected.emit([ (candidate, probe_python_version(candidate)) for candidate in candidates ], current)
		threading.Thread(target = probe, daemon = True).start()

	def on_pythons_detected(self, found : List[Tuple[str, Optional[str]]], current : str) -> None:
		self.python_combo.clear()
		best = ''
		for candidate, version in found:
			self.python_combo.addItem(candidate)
			if not best and version_tuple(version) >= MINIMUM_PYTHON:
				best = candidate
		self.python_combo.setText(best or current or (found[0][0] if found else ''))
		self.refresh_python_status()
		if not found:
			self.session.notify.emit('warning', 'No environments found', 'No conda or venv Python was found. Use "Install FaceFusion v4" below.')

	def refresh_root_status(self) -> None:
		root = self.root_edit.text().strip()
		if not root:
			self.root_status.set_status('info', 'Not set')
		elif not is_facefusion_root(root):
			self.root_status.set_status('error', 'facefusion.py was not found in this folder')
		else:
			version = read_facefusion_version(root) or 'unknown version'
			if version.startswith('v4') or version_tuple(version.lstrip('v')) >= (4, 0):
				self.root_status.set_status('success', 'FaceFusion %s' % version)
			else:
				self.root_status.set_status('warning', 'FaceFusion %s found. This app is built for v4, so options may not match.' % version)

	def refresh_python_status(self) -> None:
		path = self.python_combo.text().strip()
		if not path:
			self.python_status.set_status('info', 'Not set')
			return
		if not os.path.isfile(path):
			self.python_status.set_status('error', 'Not a working Python executable')
			return
		self.python_status.set_status('info', 'Checking Python…')
		threading.Thread(target = lambda: self.python_probed.emit(path, probe_python_version(path)), daemon = True).start()

	def on_python_probed(self, path : str, version : Optional[str]) -> None:
		if path == self.session.python_path:
			self.session.python_version = version
		# the box may have changed while python started
		if path != self.python_combo.text().strip():
			return
		if not version:
			self.python_status.set_status('error', 'Not a working Python executable')
		elif version_tuple(version) < MINIMUM_PYTHON:
			self.python_status.set_status('warning', 'Python %s. FaceFusion v4 needs Python %d.%d or newer.' % ((version,) + MINIMUM_PYTHON))
		else:
			self.python_status.set_status('success', 'Python %s' % version)

	def refresh_status(self) -> None:
		self.refresh_root_status()
		self.refresh_python_status()
		self.refresh_installed_build(wait = False)
		schema = self.session.schema
		if schema:
			self.schema_status.setText('Loaded %d options and %d processors from FaceFusion %s' % (len(schema.options), len(schema.processors), schema.version))
		else:
			self.schema_status.setText('No options loaded yet')

	def apply_location(self) -> None:
		self.session.config.set('facefusion_root', self.root_edit.text().strip())
		self.session.config.set('python_path', self.python_combo.text().strip())
		self.session.config.save()
		self.refresh_status()
		self.session.check_python()
		if not self.session.is_configured():
			self.session.notify.emit('error', 'Incomplete setup', 'Both the FaceFusion folder and python.exe are required.')
			return
		if not self.session.python_supported():
			self.session.notify.emit('warning', 'Python too old', 'FaceFusion v4 refuses to run on Python %s.' % self.session.python_version)
		self.session.refresh_schema(force = True)

	# installation

	def create_install_card(self, parent : QWidget) -> QWidget:
		card, layout = section_card('Install FaceFusion v4', 'Clones the v4 branch, creates a Python 3.13 environment and installs the requirements with the onnxruntime build you pick. It downloads a few GB and the progress shows in Console.', parent)
		self.conda_path = find_conda()
		self.uv_path = shutil.which('uv')
		default_folder = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), 'facefusion-v4')
		self.install_folder_edit = LineEdit(card)
		self.install_folder_edit.setText(default_folder)
		install_browse = PushButton(FluentIcon.FOLDER, 'Browse', card)
		install_browse.clicked.connect(self.browse_install_folder)
		layout.addLayout(form_row('Install into', [ self.install_folder_edit, install_browse ], card))

		self.env_combo = ComboBox(card)
		if self.conda_path:
			self.env_combo.addItem('Conda environment "%s"' % CONDA_ENV_NAME, userData = 'conda')
		if self.uv_path:
			self.env_combo.addItem('uv virtual environment (.venv in the install folder)', userData = 'uv')
		if not self.conda_path and not self.uv_path:
			self.env_combo.addItem('Install Miniconda or uv first', userData = None)
		self.env_combo.currentIndexChanged.connect(self.refresh_cuda_option)
		layout.addLayout(form_row('Environment', [ self.env_combo ], card))

		self.onnx_combo = ComboBox(card)
		for key, label in ONNXRUNTIME_CHOICES:
			self.onnx_combo.addItem(label, userData = key)
		self.onnx_combo.setCurrentIndex(0 if has_nvidia_gpu() else 2)
		self.onnx_combo.currentIndexChanged.connect(self.refresh_cuda_option)
		layout.addLayout(form_row('Acceleration', [ self.onnx_combo ], card))

		self.cuda_check = CheckBox('Also install the CUDA runtime and cuDNN into the conda environment (recommended for CUDA)', card)
		self.cuda_check.setChecked(True)
		layout.addLayout(form_row('', [ self.cuda_check ], card))

		self.dependency_status = StatusLine(card)
		layout.addLayout(form_row('', [ self.dependency_status ], card))
		buttons = QHBoxLayout()
		buttons.addSpacing(138)
		self.install_button = PrimaryPushButton(FluentIcon.DOWNLOAD, 'Install FaceFusion v4', card)
		self.install_button.clicked.connect(self.start_install)
		self.install_status = CaptionLabel('', card)
		self.install_status.setWordWrap(True)
		self.install_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
		buttons.addWidget(self.install_button)
		buttons.addSpacing(8)
		buttons.addWidget(self.install_status, 1)
		layout.addLayout(buttons)
		self.refresh_cuda_option()
		self.refresh_dependency_status()
		return card

	def browse_install_folder(self) -> None:
		path = QFileDialog.getExistingDirectory(self, 'Choose a parent folder', os.path.dirname(self.install_folder_edit.text()))
		if path:
			self.install_folder_edit.setText(os.path.join(os.path.normpath(path), 'facefusion-v4'))

	def refresh_cuda_option(self) -> None:
		is_cuda = str(self.onnx_combo.currentData()).startswith('cuda')
		self.cuda_check.setVisible(is_cuda and self.env_combo.currentData() == 'conda')

	def refresh_dependency_status(self) -> None:
		missing = [ name for name, found in check_dependencies().items() if not found ]
		if missing:
			self.dependency_status.set_status('warning', 'Missing on PATH: %s. FaceFusion needs ffmpeg, ffprobe and curl, and installing needs git.' % ', '.join(missing))
		else:
			self.dependency_status.set_status('success', 'git, ffmpeg, ffprobe and curl were found')

	def env_python_path(self) -> str:
		if self.env_combo.currentData() == 'conda':
			return os.path.join(find_conda_base() or '', 'envs', CONDA_ENV_NAME, 'python.exe')
		return os.path.join(self.install_folder_edit.text().strip(), '.venv', 'Scripts', 'python.exe')

	def start_install(self) -> None:
		folder = self.install_folder_edit.text().strip()
		env_kind = self.env_combo.currentData()
		onnxruntime = self.onnx_combo.currentData()
		if not folder or not env_kind:
			self.session.notify.emit('error', 'Cannot install', 'Choose an install folder and environment type.')
			return
		if os.path.exists(folder) and os.listdir(folder) and not is_facefusion_root(folder):
			self.session.notify.emit('error', 'Folder not empty', 'Choose an empty or new folder.')
			return
		python_path = self.env_python_path()
		steps : List[Tuple[str, Callable[[], bool]]] = []

		if not is_facefusion_root(folder):
			steps.append(('Cloning FaceFusion v4', lambda: self.session.run_program('Cloning FaceFusion v4', 'git', [ 'clone', '--branch', 'v4', '--depth', '1', REPOSITORY_URL, folder ], os.path.dirname(folder) or '.')))
		if not os.path.isfile(python_path):
			if env_kind == 'conda':
				steps.append(('Creating conda environment', lambda: self.session.run_program('Creating conda environment', 'cmd.exe', [ '/c', self.conda_path, 'create', '-y', '-n', CONDA_ENV_NAME, 'python=3.13', 'pip' ], folder)))
			else:
				steps.append(('Creating virtual environment', lambda: self.session.run_program('Creating virtual environment', self.uv_path, [ 'venv', '--python', '3.13', '--seed', os.path.join(folder, '.venv') ], folder)))
		if env_kind == 'conda' and str(onnxruntime).startswith('cuda') and self.cuda_check.isChecked():
			cuda_major = onnxruntime.split('@')[1]
			steps.append(('Installing CUDA runtime and cuDNN', lambda: self.session.run_program('Installing CUDA runtime and cuDNN', 'cmd.exe', [ '/c', self.conda_path, 'install', '-y', '-n', CONDA_ENV_NAME, '-c', 'conda-forge', 'cuda-runtime=' + cuda_major, 'cudnn=9' ], folder)))
		steps.append(('Installing requirements', lambda: self.session.run_program('Installing FaceFusion requirements', python_path, [ 'install.py', onnxruntime, '--skip-conda' ], folder, create_process_environment(python_path))))
		steps.append(('Finishing', lambda: self.finish_install(folder, python_path)))

		self.install_steps = steps
		self.install_index = 0
		self.install_button.setEnabled(False)
		self.run_install_step()

	def run_install_step(self) -> None:
		if self.install_index >= len(self.install_steps):
			return
		title, action = self.install_steps[self.install_index]
		self.install_status.setText('Step %d of %d: %s…' % (self.install_index + 1, len(self.install_steps), title))
		if not action():
			self.install_failed('Could not start: ' + title)

	def on_runner_finished(self, exit_code : int, _ : float) -> None:
		if not self.install_steps or self.install_index >= len(self.install_steps):
			return
		if exit_code != 0:
			self.install_failed('%s failed (exit code %d). See Console.' % (self.install_steps[self.install_index][0], exit_code))
			return
		self.install_index += 1
		self.run_install_step()

	def install_failed(self, message : str) -> None:
		self.install_steps = []
		self.install_button.setEnabled(True)
		self.install_status.setText(message)
		self.session.notify.emit('error', 'Installation stopped', message)

	def finish_install(self, folder : str, python_path : str) -> bool:
		self.install_steps = []
		self.install_button.setEnabled(True)
		self.install_status.setText('Installed. Models download on first use, or ahead of time from the Models page.')
		self.root_edit.setText(folder)
		self.python_combo.setText(python_path)
		self.apply_location()
		return True

	# updates

	def create_update_card(self, parent : QWidget) -> QWidget:
		card, layout = section_card('FaceFusion updates', 'Checks the FaceFusion v4 folder this app drives against its GitHub repository.', parent)
		self.windows_label = BodyLabel(read_windows_version(), card)
		self.windows_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
		layout.addLayout(form_row('Windows', [ self.windows_label ], card))
		self.installed_label = BodyLabel('', card)
		self.installed_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
		layout.addLayout(form_row('FaceFusion', [ self.installed_label ], card))
		self.github_label = BodyLabel('Not checked yet', card)
		self.github_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
		layout.addLayout(form_row('On GitHub', [ self.github_label ], card))
		buttons = QHBoxLayout()
		buttons.addSpacing(138)
		self.update_button = PushButton(FluentIcon.UPDATE, 'Check for FaceFusion Updates', card)
		self.update_button.clicked.connect(self.check_for_updates)
		self.github_link = HyperlinkButton(github_page(''), 'Open GitHub', card, FluentIcon.GITHUB)
		self.update_status = StatusLine(card)
		self.update_status.hide()
		buttons.addWidget(self.update_button)
		buttons.addSpacing(4)
		buttons.addWidget(self.github_link)
		# keep both at their own width, the hidden status line would otherwise leave the button to fill the row
		for button in [ self.update_button, self.github_link ]:
			button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
		buttons.addSpacing(8)
		buttons.addWidget(self.update_status, 1)
		buttons.addStretch(0)
		layout.addLayout(buttons)
		self.installed_build : Dict[str, str] = {}
		return card

	def refresh_installed_build(self, wait : bool = True) -> None:
		root = self.session.facefusion_root
		if not is_facefusion_root(root):
			self.installed_build = {}
			self.installed_label.setText('Not installed or not set')
			return
		if wait:
			self.show_installed_build(root, read_facefusion_build(root))
			return
		# four git calls, off the ui thread
		threading.Thread(target = lambda: self.build_read.emit(root, read_facefusion_build(root)), daemon = True).start()

	def show_installed_build(self, root : str, build : Dict[str, str]) -> None:
		if root != self.session.facefusion_root:
			return
		self.installed_build = build
		self.github_link.setUrl(github_page(build['repository']))
		self.installed_label.setText(format_build(build['version'], build['branch'], build['commit'], build['date']))
		self.installed_label.setToolTip(build['commit'])

	def check_for_updates(self) -> None:
		self.refresh_installed_build()
		if not self.installed_build:
			self.update_status.set_status('error', 'Set the FaceFusion folder first.')
			self.update_status.show()
			return
		build = dict(self.installed_build)
		build['repository'] = build['repository'] or DEFAULT_REPOSITORY
		self.update_button.setEnabled(False)
		self.update_status.set_status('info', 'Checking GitHub…')
		self.update_status.show()
		threading.Thread(target = self.fetch_update, args = (build,), daemon = True).start()

	def fetch_update(self, build : Dict[str, str]) -> None:
		try:
			remote = fetch_github_build(build['repository'], build['branch'], build['commit'])
		except Exception as exception:
			remote = { 'error': str(exception) }
		self.update_checked.emit(remote)

	def on_update_checked(self, remote : Dict[str, Any]) -> None:
		self.update_button.setEnabled(True)
		if remote.get('error'):
			self.github_label.setText('Not checked yet')
			self.update_status.set_status('error', 'Could not reach GitHub: %s' % remote['error'])
			return
		build = self.installed_build
		text = format_build(remote['branch'], '', remote['commit'], remote['date']) if remote['commit'] else ''
		if remote['release']:
			text += ('  ·  ' if text else '') + 'latest release %s' % remote['release']
		self.github_label.setText(text or 'Nothing found')
		self.github_label.setToolTip(remote['commit'])
		status, behind = remote['status'], remote['behind']
		if status == 'identical':
			self.update_status.set_status('success', 'You have the most current version installed.')
		elif status == 'ahead':
			self.update_status.set_status('warning', 'Update available: %d new commit%s on the %s branch.' % (behind, '' if behind == 1 else 's', remote['branch']))
		elif status == 'diverged':
			self.update_status.set_status('warning', 'Your copy differs from GitHub, which has %d commit%s you do not.' % (behind, '' if behind == 1 else 's'))
		elif status == 'behind':
			self.update_status.set_status('success', 'Your copy is newer than the %s branch on GitHub.' % remote['branch'])
		elif remote['release'] and version_tuple(build.get('version', '').lstrip('v')) and version_tuple(build['version'].lstrip('v')) < version_tuple(remote['release'].lstrip('v')):
			self.update_status.set_status('warning', 'Update available: release %s.' % remote['release'])
		elif remote['release'] and not build.get('commit'):
			self.update_status.set_status('info', 'This FaceFusion folder is not a git checkout, so it cannot be compared commit by commit.')
		else:
			self.update_status.set_status('success', 'You have the most current version installed.')

	# studioff updates

	def create_studio_update_card(self, parent : QWidget) -> QWidget:
		card, layout = section_card('StudioFF updates', 'Checks GitHub for a newer release of this app. It also checks quietly each time the app starts, and an orange dot on the Setup gear means one is out.', parent)
		self.studio_version_label = BodyLabel('v' + APP_VERSION, card)
		self.studio_version_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
		layout.addLayout(form_row('This app', [ self.studio_version_label ], card))
		self.studio_release_label = BodyLabel('Not checked yet', card)
		self.studio_release_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
		layout.addLayout(form_row('Latest release', [ self.studio_release_label ], card))
		buttons = QHBoxLayout()
		buttons.addSpacing(138)
		self.studio_install_button = PrimaryPushButton(FluentIcon.DOWNLOAD, 'Update StudioFF', card)
		self.studio_install_button.clicked.connect(self.install_studio_update)
		self.studio_install_button.hide()
		self.studio_release = {}
		buttons.addWidget(self.studio_install_button)
		buttons.addSpacing(4)
		self.studio_update_button = PushButton(FluentIcon.UPDATE, 'Check for Studio Updates', card)
		self.studio_update_button.clicked.connect(lambda: self.check_studio_update())
		self.studio_release_link = HyperlinkButton('https://github.com/%s/releases' % APP_REPOSITORY, 'Open Releases', card, FluentIcon.GITHUB)
		self.studio_update_status = StatusLine(card)
		self.studio_update_status.hide()
		buttons.addWidget(self.studio_update_button)
		buttons.addSpacing(4)
		buttons.addWidget(self.studio_release_link)
		for button in [ self.studio_install_button, self.studio_update_button, self.studio_release_link ]:
			button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
		buttons.addSpacing(8)
		buttons.addWidget(self.studio_update_status, 1)
		buttons.addStretch(0)
		layout.addLayout(buttons)
		self.studio_download_bar = ProgressBar(card)
		self.studio_download_bar.hide()
		download_row = QHBoxLayout()
		download_row.addSpacing(138)
		download_row.addWidget(self.studio_download_bar, 1)
		layout.addLayout(download_row)
		return card

	def check_studio_update(self, quiet : bool = False) -> None:
		"""quiet is the check at startup, it only fills in the card and the dot and stays silent when github cannot be reached"""
		if not self.studio_update_button.isEnabled():
			return
		self.studio_update_button.setEnabled(False)
		if not quiet:
			self.studio_update_status.set_status('info', 'Checking GitHub…')
			self.studio_update_status.show()
		threading.Thread(target = self.fetch_studio_update, args = (quiet,), daemon = True).start()

	def fetch_studio_update(self, quiet : bool) -> None:
		try:
			release = fetch_latest_release(APP_REPOSITORY)
		except Exception as exception:
			release = { 'error': str(exception) }
		self.studio_update_checked.emit(release, quiet)

	def on_studio_update_checked(self, release : Dict[str, str], quiet : bool) -> None:
		self.studio_update_button.setEnabled(self.studio_download_bar.isHidden())
		if release.get('error'):
			if not quiet:
				self.studio_update_status.set_status('error', 'Could not reach GitHub: %s' % release['error'])
			return
		tag = release['tag']
		self.studio_release_label.setText('  ·  '.join(part for part in [ tag, release['date'][:10] ] if part) or 'No releases yet')
		newer = is_newer_version(tag, APP_VERSION)
		self.studio_release = release if newer else {}
		self.studio_install_button.setText('Update to %s' % tag if newer else 'Update StudioFF')
		self.studio_install_button.setVisible(newer and bool(release.get('zip')))
		# a git checkout is updated with git, copying a release over it would mix the two
		self.studio_install_button.setEnabled(not is_git_checkout() and self.studio_download_bar.isHidden())
		self.studio_install_button.setToolTip('This copy of the app is a git checkout, so it updates through git' if is_git_checkout() else '')
		if newer:
			self.studio_release_link.setUrl(release['url'])
			self.studio_release_link.setText('Download %s' % tag)
			self.studio_update_status.set_status('warning', 'Update available: StudioFF %s.' % tag)
			self.studio_update_status.show()
		else:
			self.studio_release_link.setUrl('https://github.com/%s/releases' % APP_REPOSITORY)
			self.studio_release_link.setText('Open Releases')
			self.studio_update_status.set_status('success', 'You have the most current version of StudioFF.')
			self.studio_update_status.setVisible(not quiet or self.studio_update_status.isVisible())
		self.studio_release_link.adjustSize()
		self.studio_update_found.emit(newer)

	def install_studio_update(self, confirm : bool = True) -> None:
		tag = self.studio_release.get('tag', '')
		if not tag:
			return
		text = 'StudioFF downloads %s, closes, updates itself and opens again. Your settings, presets and FaceFusion stay as they are.' % tag
		if self.session.runner.is_running():
			text += '\n\nFaceFusion is still running "%s", the update stops it.' % self.session.task
		if confirm and not MessageBox('Update StudioFF to %s?' % tag, text, self.window()).exec():
			return
		self.studio_install_button.setEnabled(False)
		self.studio_update_button.setEnabled(False)
		self.studio_download_bar.setRange(0, 0)
		self.studio_download_bar.show()
		self.studio_update_status.set_status('info', 'Downloading %s…' % tag)
		self.studio_update_status.show()
		threading.Thread(target = self.download_studio_update, args = (self.studio_release['zip'],), daemon = True).start()

	def download_studio_update(self, zip_url : str) -> None:
		try:
			source = download_release(zip_url, self.studio_download_progress.emit)
		except Exception as exception:
			self.studio_download_finished.emit('', str(exception))
			return
		self.studio_download_finished.emit(source, '')

	def on_studio_download_progress(self, done : int, total : int) -> None:
		if total:
			self.studio_download_bar.setRange(0, total)
			self.studio_download_bar.setValue(done)
		self.studio_update_status.set_status('info', 'Downloading %s…  %.1f MB' % (self.studio_release.get('tag', ''), done / 1048576))

	def on_studio_download_finished(self, source : str, error : str) -> None:
		self.studio_download_bar.hide()
		self.studio_update_button.setEnabled(True)
		self.studio_install_button.setEnabled(True)
		if error:
			self.studio_update_status.set_status('error', 'The download failed: %s' % error)
			return
		self.studio_update_status.set_status('info', 'Closing to install %s…' % self.studio_release['tag'])
		window = self.window()
		# the running job was already confirmed in the update question
		if self.session.runner.is_running():
			self.session.runner.stop()
			self.session.clear_facefusion_temp()
		window.close()
		if window.isVisible():
			self.studio_update_status.set_status('warning', 'The update was not installed because the app stayed open.')
			return
		start_helper(source, self.studio_release['tag'])
		QApplication.quit()

	# appearance

	def create_appearance_card(self, parent : QWidget) -> QWidget:
		card, layout = section_card('Appearance and behaviour', '', parent)
		themes = QWidget(card)
		themes_layout = QHBoxLayout(themes)
		themes_layout.setContentsMargins(0, 0, 0, 0)
		themes_layout.setSpacing(6)
		self.theme_group = QButtonGroup(themes)
		current = self.session.config.get('theme') or 'auto'
		for key, text, icon, tip in THEME_CHOICES:
			button = ChoiceButton(text, None, icon, themes)
			button.setCheckable(True)
			button.setChecked(key == current)
			button.setToolTip(tip)
			button.installEventFilter(ToolTipFilter(button))
			button.setProperty('theme', key)
			self.theme_group.addButton(button)
			themes_layout.addWidget(button)
		themes_layout.addStretch(1)
		self.theme_group.buttonClicked.connect(lambda button: self.on_theme_changed(button.property('theme')))
		layout.addLayout(form_row('Theme', [ themes ], card))
		# one accent for each look, the one in use follows the theme
		accents = self.session.config.get('accent_colors') or {}
		self.accent_buttons : Dict[str, ColorPickerButton] = {}
		row = []
		for key, text in [ ('light', 'Light theme'), ('dark', 'Dark theme') ]:
			button = ColorPickerButton(QColor(accents.get(key) or DEFAULT_ACCENT), '%s accent color' % text.lower(), card)
			button.setFixedSize(56, 28)
			button.setToolTip('Click to choose the accent color of the %s' % text.lower())
			button.installEventFilter(ToolTipFilter(button))
			button.colorChanged.connect(lambda color, key = key: self.set_accent(key, color.name()))
			self.accent_buttons[key] = button
			row += [ BodyLabel(text.split()[0], card), button ]
		reset = TransparentToolButton(FluentIcon.HISTORY, card)
		reset.setToolTip('Back to the standard purple for both themes')
		reset.installEventFilter(ToolTipFilter(reset))
		reset.clicked.connect(self.reset_accents)
		accent_row = QWidget(card)
		accent_layout = QHBoxLayout(accent_row)
		accent_layout.setContentsMargins(0, 0, 0, 0)
		accent_layout.setSpacing(8)
		for index, widget in enumerate(row):
			accent_layout.addWidget(widget)
			if index == 1:
				accent_layout.addSpacing(10)
		accent_layout.addWidget(reset)
		accent_layout.addStretch(1)
		layout.addLayout(form_row('Accent color', [ accent_row ], card))
		self.open_switch = SwitchButton(card)
		self.open_switch.setChecked(bool(self.session.config.get('open_output_when_done')))
		self.open_switch.checkedChanged.connect(lambda checked: self.session.config.set('open_output_when_done', checked))
		layout.addLayout(form_row('Open result', [ BodyLabel('Open the output file when processing finishes', card), self.open_switch ], card))
		self.keep_loaded_combo = ComboBox(card)
		current = int(self.session.config.get('preview_keep_loaded_minutes'))
		for minutes, text in KEEP_LOADED_CHOICES:
			self.keep_loaded_combo.addItem(text, userData = minutes)
			if minutes == current:
				self.keep_loaded_combo.setCurrentIndex(self.keep_loaded_combo.count() - 1)
		self.keep_loaded_combo.setToolTip('After the first preview FaceFusion keeps its models loaded, so the next previews take about 2 s instead of 6 or more. They use graphics card memory while loaded. Start, Batch and Benchmark always free them first')
		self.keep_loaded_combo.installEventFilter(ToolTipFilter(self.keep_loaded_combo))
		self.keep_loaded_combo.currentIndexChanged.connect(self.on_keep_loaded_changed)
		layout.addLayout(form_row('Preview models', [ BodyLabel('Keep loaded after the last preview for', card), self.keep_loaded_combo ], card))
		keep_loaded_tip = CaptionLabel('Tip: keeping the models loaded makes the preview window refresh much faster after each change.', card)
		keep_loaded_tip.setWordWrap(True)
		keep_loaded_tip.setTextColor('#606060', '#a0a0a0')
		layout.addLayout(form_row('', [ keep_loaded_tip ], card))
		return card

	def on_keep_loaded_changed(self, _ : int) -> None:
		minutes = int(self.keep_loaded_combo.currentData())
		self.session.config.set('preview_keep_loaded_minutes', minutes)
		self.session.config.save()
		self.session.preview_worker.set_idle_minutes(minutes)

	def on_theme_changed(self, theme : str) -> None:
		self.session.config.set('theme', theme)
		self.session.config.save()
		apply_theme(theme, self.session.config.get('accent_colors'))

	def set_accent(self, key : str, color : str) -> None:
		accents = dict(self.session.config.get('accent_colors') or {})
		accents[key] = color
		self.session.config.set('accent_colors', accents)
		self.session.config.save()
		apply_theme(self.session.config.get('theme') or 'auto', accents)

	def reset_accents(self) -> None:
		for button in self.accent_buttons.values():
			button.setColor(QColor(DEFAULT_ACCENT))
		self.session.config.set('accent_colors', {})
		self.session.config.save()
		apply_theme(self.session.config.get('theme') or 'auto')
