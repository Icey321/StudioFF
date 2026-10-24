import glob
import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, FluentIcon, LineEdit, ListWidget, PrimaryPushButton, ProgressBar, PushButton, SimpleCardWidget, StrongBodyLabel, SubtitleLabel, ToolTipFilter

from ffstudio.media import media_kind
from ffstudio.nsfw import BLOCKED_TITLE, NsfwDialog, show_blocked_bar
from ffstudio.pages.setup import form_row, section_card
from ffstudio.session import Session

SOURCE_FILTERS = [ ('*.jpg', 'JPG images'), ('*.png', 'PNG images'), ('*.webp', 'WebP images'), ('*.*', 'All files') ]
TARGET_FILTERS = [ ('*.mp4', 'MP4 videos'), ('*.jpg', 'JPG images'), ('*.png', 'PNG images'), ('*.mov', 'MOV videos'), ('*.webm', 'WebM videos'), ('*.*', 'All files') ]
DEFAULT_TEMPLATE = '{target_name}-{source_name}{target_extension}'
LIST_LIMIT = 500
STATUS_PREFIX =\
{
	'done': '✓  ',
	'blocked': '⛔  NSFW content detected  ·  ',
	'skip': '⛔  NSFW content detected, will be skipped  ·  ',
	'failed': '✗  Failed  ·  ',
	'running': '▶  '
}


class BatchPage(QWidget):
	def __init__(self, session : Session, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.setObjectName('batch')
		self.session = session
		self.running = False
		# one entry per output: source, target, output path, status
		self.items : List[Dict[str, Any]] = []
		# nsfw check results by target, from NSFW Check or from confirming refused images after a batch
		self.check_results : Dict[str, Dict[str, Any]] = {}
		self.current = -1
		self.started_count = 0
		self.confirming = False
		self.stopped = False
		stored = session.config.get('batch') or {}

		layout = QVBoxLayout(self)
		layout.setContentsMargins(24, 16, 24, 16)
		layout.setSpacing(12)
		title = QHBoxLayout()
		title.addWidget(SubtitleLabel('Batch', self))
		hint = CaptionLabel('Runs every source against every target, one output at a time, using the processors and options from Studio', self)
		hint.setTextColor('#707070', '#9a9a9a')
		title.addWidget(hint)
		title.addStretch(1)
		layout.addLayout(title)

		card, form = section_card('Inputs and output', '', self)
		self.source_dir, self.source_filter = self.folder_row(form, card, 'Source faces', stored.get('source_dir', ''), SOURCE_FILTERS, stored.get('source_filter', '*.jpg'), optional = True)
		self.target_dir, self.target_filter = self.folder_row(form, card, 'Targets', stored.get('target_dir', ''), TARGET_FILTERS, stored.get('target_filter', '*.mp4'))
		self.output_dir = LineEdit(card)
		self.output_dir.setText(stored.get('output_dir', os.path.join(session.config.get('output_dir') or '', 'batch')))
		output_browse = PushButton(FluentIcon.FOLDER, 'Browse', card)
		output_browse.clicked.connect(lambda: self.browse(self.output_dir))
		form.addLayout(form_row('Output folder', [ self.output_dir, output_browse ], card))
		self.template = LineEdit(card)
		self.template.setText(stored.get('template', DEFAULT_TEMPLATE))
		form.addLayout(form_row('File name', [ self.template ], card))
		tokens = CaptionLabel('Placeholders: {index}  {source_name}  {target_name}  {target_extension} (includes the dot)', card)
		tokens.setTextColor('#707070', '#9a9a9a')
		form.addLayout(form_row('', [ tokens ], card))
		layout.addWidget(card)

		for edit in [ self.source_dir, self.target_dir, self.output_dir, self.template ]:
			edit.textChanged.connect(self.refresh_matches)

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
		self.refresh_matches()

	def folder_row(self, form : QVBoxLayout, card : QWidget, label : str, folder : str, filters, current_filter : str, optional : bool = False):
		edit = LineEdit(card)
		edit.setText(folder)
		edit.setPlaceholderText('Optional, leave empty for processors without a source' if optional else 'Folder')
		edit.setClearButtonEnabled(True)
		combo = ComboBox(card)
		for pattern, name in filters:
			combo.addItem('%s  (%s)' % (name, pattern), userData = pattern)
		combo.setCurrentIndex(max(0, [ pattern for pattern, _ in filters ].index(current_filter) if current_filter in [ pattern for pattern, _ in filters ] else 0))
		combo.setFixedWidth(210)
		combo.currentIndexChanged.connect(self.refresh_matches)
		browse = PushButton(FluentIcon.FOLDER, 'Browse', card)
		browse.clicked.connect(lambda: self.browse(edit))
		form.addLayout(form_row(label, [ edit, combo, browse ], card))
		return edit, combo

	def browse(self, edit : LineEdit) -> None:
		path = QFileDialog.getExistingDirectory(self, 'Choose folder', edit.text() or os.path.expanduser('~'))
		if path:
			edit.setText(os.path.normpath(path))

	def pattern(self, edit : LineEdit, combo : ComboBox) -> str:
		folder = edit.text().strip()
		return os.path.join(folder, combo.currentData()) if folder else ''

	def effective_template(self) -> str:
		template = self.template.text().strip() or DEFAULT_TEMPLATE
		if not self.source_dir.text().strip():
			template = template.replace('-{source_name}', '').replace('{source_name}', '')
		return template

	def refresh_matches(self) -> None:
		if self.running:
			return
		sources = sorted(glob.glob(self.pattern(self.source_dir, self.source_filter))) if self.source_dir.text().strip() else []
		targets = sorted(glob.glob(self.pattern(self.target_dir, self.target_filter))) if self.target_dir.text().strip() else []
		pairs = [ (source, target) for source in sources for target in targets ] if sources else [ (None, target) for target in targets ]
		template = self.effective_template()
		output_dir = self.output_dir.text().strip()
		self.items = []
		for index, (source, target) in enumerate(pairs):
			target_name, target_extension = os.path.splitext(os.path.basename(target))
			try:
				name = template.format(index = index, source_name = os.path.splitext(os.path.basename(source))[0] if source else '', target_name = target_name, target_extension = target_extension.lower())
			except (KeyError, IndexError, ValueError):
				name = ''
			self.items.append({ 'source': source, 'target': target, 'name': name, 'output': os.path.join(output_dir, name) if name else '', 'status': '' })
		self.refresh_list()
		if sources:
			self.summary.setText('%d sources × %d targets = %d outputs' % (len(sources), len(targets), len(pairs)))
		else:
			self.summary.setText('%d targets' % len(targets))
		self.pairs_count = len(pairs)

	def item_status(self, item : Dict[str, Any]) -> str:
		if item.get('status'):
			return item.get('status')
		result = self.check_results.get(item.get('target'))
		return 'skip' if result and result.get('blocked') else ''

	def item_text(self, item : Dict[str, Any]) -> str:
		name = item.get('name') or '⚠ invalid file name template'
		source = item.get('source')
		return STATUS_PREFIX.get(self.item_status(item), '') + '%s  ←  %s%s' % (name, os.path.basename(item.get('target')), ('  +  ' + os.path.basename(source)) if source else '')

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
			'source_dir': self.source_dir.text().strip(),
			'source_filter': self.source_filter.currentData(),
			'target_dir': self.target_dir.text().strip(),
			'target_filter': self.target_filter.currentData(),
			'output_dir': self.output_dir.text().strip(),
			'template': self.template.text().strip()
		})

	# running, one facefusion run per output so a refused target only fails its own output

	def start(self) -> None:
		self.save()
		self.refresh_matches()
		if not self.pairs_count:
			self.session.notify.emit('warning', 'Nothing to do', 'No target files match the folder and filter.')
			return
		output_dir = self.output_dir.text().strip()
		if not output_dir:
			self.session.notify.emit('warning', 'No output folder', 'Choose where the results go.')
			return
		if not all(item.get('name') for item in self.items):
			self.session.notify.emit('warning', 'Invalid file name', 'The file name template has an unknown placeholder.')
			return
		os.makedirs(output_dir, exist_ok = True)
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
		if item.get('source'):
			args += [ '-s', item.get('source') ]
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
			self.session.notify.emit('warning' if counts.get('failed') else 'success', 'Batch finished', '%s in %s' % (summary, self.output_dir.text()))

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
