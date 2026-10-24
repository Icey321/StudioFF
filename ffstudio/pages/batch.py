import glob
import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, FluentIcon, Flyout, FlyoutAnimationType, LineEdit, ListWidget, PrimaryPushButton, ProgressBar, PushButton, SimpleCardWidget, StrongBodyLabel, SubtitleLabel, TogglePushButton, ToolTipFilter

from ffstudio.media import media_kind
from ffstudio.nsfw import BLOCKED_TITLE, NsfwDialog, show_blocked_bar
from ffstudio.output_name import build_output, unknown_tags
from ffstudio.pages.setup import form_row, section_card
from ffstudio.pages.studio import NameTagsView, SourceCard, SourceSelection, elastic, run_tag_values
from ffstudio.session import Session, explain_error, folder_is_writable
from ffstudio.widgets import add_edit_button

TARGET_FILTERS = [ ('*.mp4', 'MP4 videos'), ('*.jpg', 'JPG images'), ('*.png', 'PNG images'), ('*.mov', 'MOV videos'), ('*.webm', 'WebM videos'), ('*.*', 'All files') ]
DEFAULT_TEMPLATE = '{target}_{source}'
# the placeholders of the first batch file names, read as the tags that replaced them
OLD_PLACEHOLDERS = { '{target_name}': '{target}', '{source_name}': '{source}', '{target_extension}': '', '{index}': '{n}' }
LIST_LIMIT = 500
STATUS_PREFIX =\
{
	'done': '✓  ',
	'blocked': '⛔  NSFW content detected  ·  ',
	'skip': '⛔  NSFW content detected, will be skipped  ·  ',
	'failed': '✗  Failed  ·  ',
	'running': '▶  '
}


def upgrade_template(template : str) -> str:
	for old, new in OLD_PLACEHOLDERS.items():
		template = template.replace(old, new)
	return template


class BatchPage(SourceSelection, QWidget):
	SOURCE_KEYS = ('batch_source_paths', 'batch_unused_source_paths', 'batch_source_names')

	def __init__(self, session : Session, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.setObjectName('batch')
		self.session = session
		self.running = False
		# one entry per output: sources, target, output path, status
		self.items : List[Dict[str, Any]] = []
		self.pairs_count = 0
		# nsfw check results by target, from NSFW Check or from confirming refused images after a batch
		self.check_results : Dict[str, Dict[str, Any]] = {}
		self.current = -1
		self.started_count = 0
		self.confirming = False
		self.stopped = False
		self.load_sources()
		stored = session.config.get('batch') or {}
		# typing and setting changes replan the outputs once they settle
		self.refresh_timer = QTimer(self)
		self.refresh_timer.setSingleShot(True)
		self.refresh_timer.setInterval(200)
		self.refresh_timer.timeout.connect(self.refresh_matches)

		layout = QVBoxLayout(self)
		layout.setContentsMargins(24, 16, 24, 16)
		layout.setSpacing(12)
		title = QHBoxLayout()
		title.addWidget(SubtitleLabel('Batch', self))
		hint = CaptionLabel('Runs every selected face against every target, one output at a time, using the processors and options from Studio. Selected audio goes into every run.', self)
		hint.setTextColor('#707070', '#9a9a9a')
		title.addWidget(elastic(hint), 1)
		layout.addLayout(title)

		self.source_card = SourceCard(self, 'Source faces', 'Drop face images (or audio for lip sync) here or click to browse\nLeave empty for processors that need no source')
		layout.addWidget(self.source_card)

		card, form = section_card('Targets and output', '', self)
		self.target_dir = LineEdit(card)
		self.target_dir.setText(stored.get('target_dir', ''))
		self.target_dir.setPlaceholderText('Folder with the target images or videos')
		self.target_dir.setClearButtonEnabled(True)
		self.target_filter = ComboBox(card)
		patterns = [ pattern for pattern, _ in TARGET_FILTERS ]
		for pattern, name in TARGET_FILTERS:
			self.target_filter.addItem('%s  (%s)' % (name, pattern), userData = pattern)
		self.target_filter.setCurrentIndex(patterns.index(stored.get('target_filter')) if stored.get('target_filter') in patterns else 0)
		self.target_filter.setFixedWidth(210)
		self.target_filter.currentIndexChanged.connect(self.refresh_matches)
		target_browse = PushButton(FluentIcon.FOLDER, 'Browse', card)
		target_browse.clicked.connect(lambda: self.browse(self.target_dir))
		form.addLayout(form_row('Target folder', [ self.target_dir, self.target_filter, target_browse ], card))
		self.output_dir_edit = LineEdit(card)
		self.output_dir_edit.setText(stored.get('output_dir', os.path.join(session.config.get('output_dir') or '', 'batch')))
		output_browse = PushButton(FluentIcon.FOLDER, 'Browse', card)
		output_browse.clicked.connect(lambda: self.browse(self.output_dir_edit))
		form.addLayout(form_row('Output folder', [ self.output_dir_edit, output_browse ], card))
		self.output_name_edit = LineEdit(card)
		self.output_name_edit.setPlaceholderText('File name (automatic)')
		self.output_name_edit.setClearButtonEnabled(True)
		self.output_name_edit.setText(upgrade_template(stored.get('template', DEFAULT_TEMPLATE)))
		self.tags_button = add_edit_button(self.output_name_edit, FluentIcon.TAG, 'File name tags and examples')
		self.tags_button.clicked.connect(self.show_name_tags)
		self.subfolders_button = TogglePushButton('Sub-Folders', card)
		self.subfolders_button.setToolTip('Save into <output folder>\\<source name>\\Images or Videos, created as needed')
		self.subfolders_button.installEventFilter(ToolTipFilter(self.subfolders_button))
		self.subfolders_button.setChecked(bool(stored.get('subfolders')))
		self.subfolders_button.toggled.connect(self.refresh_matches)
		form.addLayout(form_row('File name', [ self.output_name_edit, self.subfolders_button ], card))
		# what the first file will be called, so tags can be checked before a run
		self.name_preview_label = elastic(CaptionLabel('', card))
		self.name_preview_label.setTextColor('#707070', '#9a9a9a')
		self.name_preview_label.installEventFilter(ToolTipFilter(self.name_preview_label))
		form.addLayout(form_row('', [ self.name_preview_label ], card))
		layout.addWidget(card)

		for edit in [ self.target_dir, self.output_dir_edit, self.output_name_edit ]:
			edit.textChanged.connect(self.refresh_timer.start)

		matches_card = SimpleCardWidget(self)
		matches_layout = QVBoxLayout(matches_card)
		matches_layout.setContentsMargins(20, 14, 20, 16)
		header = QHBoxLayout()
		header.addWidget(StrongBodyLabel('Planned outputs', matches_card))
		self.summary = CaptionLabel('', matches_card)
		self.summary.setTextColor('#707070', '#9a9a9a')
		header.addWidget(self.summary)
		header.addStretch(1)
		matches_layout.addLayout(header)
		self.match_list = ListWidget(matches_card)
		self.match_list.itemDoubleClicked.connect(self.show_item_result)
		matches_layout.addWidget(self.match_list, 1)
		layout.addWidget(matches_card, 1)

		run_card = SimpleCardWidget(self)
		run_layout = QHBoxLayout(run_card)
		run_layout.setContentsMargins(16, 12, 16, 12)
		self.start_button = PrimaryPushButton(FluentIcon.PLAY, 'Start batch', run_card)
		self.start_button.clicked.connect(self.start)
		self.stop_button = PushButton(FluentIcon.CLOSE, 'Stop', run_card)
		self.stop_button.setEnabled(False)
		self.stop_button.clicked.connect(self.stop)
		self.check_button = PushButton(FluentIcon.CERTIFICATE, 'NSFW Check', run_card)
		self.check_button.setToolTip("Run FaceFusion's NSFW filter on every target without processing them. Start skips the targets it flags.")
		self.check_button.installEventFilter(ToolTipFilter(self.check_button))
		self.check_button.clicked.connect(self.check_targets)
		run_layout.addWidget(self.start_button)
		run_layout.addWidget(self.stop_button)
		run_layout.addWidget(self.check_button)
		run_layout.addSpacing(10)
		status = QVBoxLayout()
		self.status_label = BodyLabel('Ready', run_card)
		self.progress = ProgressBar(run_card)
		self.progress.setRange(0, 1000)
		status.addWidget(self.status_label)
		status.addWidget(self.progress)
		run_layout.addLayout(status, 1)
		layout.addWidget(run_card)

		session.busy_changed.connect(self.on_busy_changed)
		session.runner.progress.connect(self.on_progress)
		session.runner.log.connect(self.on_log)
		session.runner.finished.connect(self.on_finished)
		session.content_checked.connect(self.on_content_checked)
		session.value_changed.connect(lambda *_: self.refresh_timer.start())
		self.source_card.refresh(self.source_paths)
		self.refresh_matches()

	def on_sources_changed(self, change : str) -> None:
		self.refresh_matches()

	def last_dir(self) -> str:
		folder = self.target_dir.text().strip()
		if os.path.isdir(folder):
			return folder
		return os.path.dirname(self.source_paths[0]) if self.source_paths else os.path.expanduser('~')

	def browse(self, edit : LineEdit) -> None:
		path = QFileDialog.getExistingDirectory(self, 'Choose folder', edit.text() or os.path.expanduser('~'))
		if path:
			edit.setText(os.path.normpath(path))

	def output_dir(self) -> str:
		return self.output_dir_edit.text().strip()

	def target_pattern(self) -> str:
		folder = self.target_dir.text().strip()
		return os.path.join(folder, self.target_filter.currentData()) if folder else ''

	def runs(self) -> List[Dict[str, Any]]:
		"""one run per selected face and target, the selected audio goes into each of them"""
		targets = sorted(glob.glob(self.target_pattern())) if self.target_pattern() else []
		sources = self.active_sources()
		images = [ path for path in sources if media_kind(path) == 'image' ]
		audio = [ path for path in sources if media_kind(path) == 'audio' ]
		return [ { 'source': image, 'sources': ([ image ] if image else []) + audio, 'target': target } for image in images or [ None ] for target in targets ]

	def name_tag_values(self, run : Optional[Dict[str, Any]] = None) -> Dict[str, str]:
		"""what each file name tag stands for in a run, the first planned one when none is given"""
		run = run or (self.items[0] if self.items else { 'sources': self.active_sources()[:1], 'target': '' })
		values = run_tag_values(self.session)
		values.update(
		{
			'target': os.path.splitext(os.path.basename(run.get('target') or ''))[0],
			'source': '+'.join(self.source_name(path) for path in run.get('sources')),
			'frame': '0'
		})
		return values

	def output_extension(self, run : Optional[Dict[str, Any]] = None) -> str:
		run = run or (self.items[0] if self.items else { 'sources': [], 'target': '' })
		target = run.get('target') or ''
		if media_kind(target) == 'image' and any(media_kind(path) == 'audio' for path in run.get('sources')):
			return '.mp4'
		return os.path.splitext(target)[1].lower() or '.mp4'

	def refresh_matches(self) -> None:
		if self.running:
			return
		self.refresh_timer.stop()
		self.save()
		template = self.output_name_edit.text()
		output_dir = self.output_dir()
		subfolders = self.subfolders_button.isChecked()
		# outputs planned earlier in the list count as taken, so {n} keeps counting across the batch
		taken : set = set()
		self.items = []
		for run in self.runs():
			path, replaces = build_output(template, self.name_tag_values(run), output_dir, self.output_extension(run), subfolders, taken)
			key = os.path.normcase(path)
			name = os.path.relpath(path, output_dir) if subfolders and output_dir else os.path.basename(path)
			self.items.append(dict(run, output = path, name = name, replaces = replaces, repeated = key in taken, status = ''))
			taken.add(key)
		self.pairs_count = len(self.items)
		self.refresh_list()
		faces = len({ item.get('source') for item in self.items if item.get('source') })
		targets = len({ item.get('target') for item in self.items })
		self.summary.setText('%d faces × %d targets = %d outputs' % (faces, targets, len(self.items)) if faces else '%d targets' % targets)
		self.refresh_name_preview()

	def refresh_name_preview(self) -> None:
		template = self.output_name_edit.text()
		if not self.items:
			self.name_preview_label.setText('Choose a target folder to see the file names')
			self.name_preview_label.setToolTip('')
			return
		first = self.items[0]
		notes = [ 'and %d more' % (len(self.items) - 1) ] if len(self.items) > 1 else []
		if not template.strip():
			notes.append('automatic name, click the tag button to build your own')
		repeated = sum(1 for item in self.items if item.get('repeated'))
		if repeated:
			notes.append('%d %s the same name as another, add {source} or {n}' % (repeated, 'output has' if repeated == 1 else 'outputs have'))
		replaces = sum(1 for item in self.items if item.get('replaces') and not item.get('repeated'))
		if replaces:
			notes.append('%s, add {n} to keep both' % ('replaces an existing file' if replaces == 1 else '%d replace existing files' % replaces))
		unknown = unknown_tags(template)
		if unknown:
			notes.append('unknown tag %s is kept as typed' % ', '.join(unknown))
		self.name_preview_label.setText('Saves As:  %s%s' % (first.get('name'), ('   ·   ' + '   ·   '.join(notes)) if notes else ''))
		self.name_preview_label.setToolTip(first.get('output'))

	def show_name_tags(self) -> None:
		Flyout.make(NameTagsView(self), self.tags_button, self.window(), FlyoutAnimationType.DROP_DOWN)

	def item_status(self, item : Dict[str, Any]) -> str:
		if item.get('status'):
			return item.get('status')
		result = self.check_results.get(item.get('target'))
		return 'skip' if result and result.get('blocked') else ''

	def item_text(self, item : Dict[str, Any]) -> str:
		reason = item.get('reason') if self.item_status(item) == 'failed' else ''
		sources = ''.join('  +  ' + self.source_name(path) for path in item.get('sources'))
		return STATUS_PREFIX.get(self.item_status(item), '') + '%s  ←  %s%s' % (item.get('name'), os.path.basename(item.get('target')), sources) + ('  ·  ' + reason if reason else '')

	def refresh_list(self) -> None:
		self.match_list.clear()
		for item in self.items[:LIST_LIMIT]:
			self.match_list.addItem(self.item_text(item))

	def refresh_item(self, index : int) -> None:
		if index < min(len(self.items), LIST_LIMIT):
			self.match_list.item(index).setText(self.item_text(self.items[index]))

	def show_item_result(self, list_item) -> None:
		index = self.match_list.row(list_item)
		item = self.items[index] if 0 <= index < len(self.items) else None
		result = self.check_results.get(item.get('target')) if item else None
		if result:
			NsfwDialog(result, item.get('status') == 'blocked', self.window()).exec()

	def save(self) -> None:
		self.session.config.set('batch',
		{
			'target_dir': self.target_dir.text().strip(),
			'target_filter': self.target_filter.currentData(),
			'output_dir': self.output_dir(),
			'template': self.output_name_edit.text().strip(),
			'subfolders': self.subfolders_button.isChecked()
		})

	# running, one facefusion run per output so a refused target only fails its own output

	def start(self) -> None:
		self.save()
		self.refresh_matches()
		if not self.pairs_count:
			self.session.notify.emit('warning', 'Nothing to do', 'No target files match the folder and filter.')
			return
		if not self.output_dir():
			self.session.notify.emit('warning', 'No output folder', 'Choose where the results go.')
			return
		if any(item.get('repeated') for item in self.items):
			self.session.notify.emit('warning', 'Same file name', 'Some outputs would get the same name and replace each other. Add {source} or {n} to the file name.')
			return
		# with Sub-Folders on every source has its own folder, each is checked and created here
		for folder in sorted({ os.path.dirname(item.get('output')) for item in self.items }):
			if not folder_is_writable(folder):
				self.session.notify.emit('error', 'Cannot save to this folder', 'Windows does not allow saving to %s. Choose another output folder, such as Videos or Documents.' % folder)
				return
		for item in self.items:
			# targets the nsfw check flagged would only be refused, so they are not run
			item['status'] = 'blocked' if self.item_status(item) == 'skip' else ''
		self.refresh_list()
		self.running = True
		self.stopped = False
		self.confirming = False
		self.started_count = 0
		self.current = -1
		self.progress.setValue(0)
		self.progress.resume()
		self.run_next()

	def stop(self) -> None:
		self.stopped = True
		self.session.runner.stop()

	def queued(self) -> List[int]:
		return [ index for index, item in enumerate(self.items) if not item.get('status') ]

	def run_next(self) -> None:
		queued = self.queued()
		if self.stopped or not queued:
			self.finish()
			return
		self.current = queued[0]
		item = self.items[self.current]
		item['status'] = 'running'
		self.refresh_item(self.current)
		self.started_count += 1
		args = [ 'run' ]
		if item.get('sources'):
			args += [ '-s' ] + item.get('sources')
		args += [ '-t', item.get('target'), '-o', item.get('output') ]
		values = dict(self.session.values)
		values.update({ 'reference_frame_index': 0, 'trim_frame_start': None, 'trim_frame_end': None })
		args += self.session.option_args(values)
		total = self.started_count + len(queued) - 1
		task = 'Batch %d of %d: %s' % (self.started_count, total, item.get('name'))
		self.status_label.setText(task)
		if not self.session.run_facefusion(task, args):
			item['status'] = ''
			self.refresh_item(self.current)
			self.current = -1
			self.running = False

	def on_busy_changed(self, busy : bool, task : str) -> None:
		self.start_button.setEnabled(not busy and not self.running)
		self.check_button.setEnabled(not busy and not self.running)
		self.stop_button.setEnabled(busy and self.running)

	def on_progress(self, title : str, ratio : float, status : str) -> None:
		if self.running and self.current >= 0:
			total = self.started_count + len(self.queued())
			self.progress.setValue(int((self.started_count - 1 + ratio) / max(total, 1) * 1000))

	def on_log(self, line : str) -> None:
		pass

	def on_finished(self, exit_code : int, elapsed : float) -> None:
		if not self.running or self.current < 0:
			return
		index, self.current = self.current, -1
		item = self.items[index]
		if exit_code == 0:
			item['status'] = 'done'
		elif exit_code == -1:
			item['status'] = ''
		elif self.session.nsfw_block_is_certain():
			item['status'] = 'blocked'
			self.check_results[item.get('target')] = { 'path': item.get('target'), 'kind': 'video', 'blocked': True, 'rate': self.session.content_rate, 'checked': 0, 'flagged': [], 'previews': [] }
		else:
			item['status'] = 'failed'
			error = self.session.run_error() or ''
			item['reason'] = (explain_error(error) or error).split('. ')[0][:120]
			# an image gives no reason when it is refused, the nsfw check confirms it once the batch is done
			item['confirm'] = bool(self.session.nsfw_block_message(media_kind(item.get('target')) == 'image'))
		self.refresh_item(index)
		# let every listener see this run end before the next one starts
		QTimer.singleShot(0, self.run_next)

	def finish(self) -> None:
		unconfirmed = sorted({ item.get('target') for item in self.items if item.pop('confirm', False) })
		if unconfirmed and not self.stopped:
			self.confirming = True
			self.status_label.setText('Checking %d refused image%s with the NSFW filter…' % (len(unconfirmed), '' if len(unconfirmed) == 1 else 's'))
			if self.session.run_content_check('batch', unconfirmed):
				return
			self.confirming = False
		self.running = False
		self.on_busy_changed(self.session.runner.is_running(), self.session.task)
		counts = { status: sum(1 for item in self.items if item.get('status') == status) for status in [ 'done', 'blocked', 'failed' ] }
		parts = [ '%d done' % counts.get('done') ]
		if counts.get('blocked'):
			parts.append('%d skipped: NSFW content detected' % counts.get('blocked'))
		if counts.get('failed'):
			parts.append('%d failed, see Console' % counts.get('failed'))
		summary = ', '.join(parts)
		if self.stopped:
			self.progress.pause()
			self.status_label.setText('Stopped  ·  ' + summary)
			return
		self.progress.setValue(1000)
		if counts.get('failed'):
			self.progress.error()
		self.status_label.setText('Batch finished  ·  ' + summary + ('. Double-click a ⛔ output to see what was flagged.' if counts.get('blocked') else ''))
		if counts.get('blocked'):
			show_blocked_bar(self.window(), "FaceFusion's NSFW filter flagged %d of the outputs in this batch, so they were skipped. The other outputs were made as normal." % counts.get('blocked'))
		else:
			self.session.notify.emit('warning' if counts.get('failed') else 'success', 'Batch finished', '%s in %s' % (summary, self.output_dir()))

	# nsfw check

	def check_targets(self) -> None:
		self.refresh_matches()
		targets = sorted({ item.get('target') for item in self.items })
		if not targets:
			self.session.notify.emit('warning', 'Nothing to check', 'No target files match the folder and filter.')
			return
		self.confirming = False
		self.progress.setValue(0)
		self.progress.resume()
		self.status_label.setText('Checking %d target%s with the NSFW filter…' % (len(targets), '' if len(targets) == 1 else 's'))
		self.session.run_content_check('batch', targets)

	def on_content_checked(self, owner : str, results : list, exit_code : int) -> None:
		if owner != 'batch':
			return
		for result in results:
			self.check_results[result.get('path')] = result
		if self.confirming:
			# the check after a batch: refused images it flags were nsfw, the rest failed for another reason
			self.confirming = False
			for index, item in enumerate(self.items):
				result = self.check_results.get(item.get('target'))
				if item.get('status') == 'failed' and result and result.get('blocked'):
					item['status'] = 'blocked'
					self.refresh_item(index)
			self.finish()
			return
		if exit_code == -1:
			self.progress.pause()
			self.status_label.setText('NSFW check stopped')
			return
		if exit_code != 0:
			self.progress.error()
			self.status_label.setText('NSFW check failed (exit code %d). See Console.' % exit_code)
			return
		self.progress.setValue(1000)
		self.refresh_list()
		flagged = [ result for result in results if result.get('blocked') ]
		if flagged:
			skipped = sum(1 for item in self.items if self.item_status(item) == 'skip')
			self.status_label.setText('⛔ %s in %d of %d targets. Start skips those %d outputs. Double-click one to see what was flagged.' % (BLOCKED_TITLE, len(flagged), len(results), skipped))
			show_blocked_bar(self.window(), "FaceFusion's NSFW filter flagged %d of the %d targets. Start will skip them and make the rest." % (len(flagged), len(results)))
		else:
			self.status_label.setText('✓ No NSFW content detected in the %d targets' % len(results))
			self.session.notify.emit('success', 'No NSFW content detected', "FaceFusion's NSFW filter found nothing in the %d targets." % len(results))
