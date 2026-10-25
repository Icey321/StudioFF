"""checks for the parts of Studio that need no window or FaceFusion, run with: python -m unittest discover tests"""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
import zipfile

import cv2
import numpy

from unittest import mock

from ffstudio import config
from ffstudio.config import AppConfig, migrate_data_dir
from ffstudio.environment import is_newer_version
from ffstudio.face_mapping import SELECTED, pass_distance, plan_passes
from ffstudio.media import VideoReader
from ffstudio.output_name import build_output, build_output_path, clean_name, expand, unknown_tags
from ffstudio.runner import PreviewWorker, ProcessRunner
from ffstudio.schema import Schema, build_option_args
from ffstudio.updater import APP_ROOT, RETIRED_FILES, download_release


class OutputNameTest(unittest.TestCase):
	def test_expand_fills_tags(self) -> None:
		self.assertEqual(expand('{target} - {source}', { 'target': 'clip', 'source': 'Ann' }), 'clip - Ann')

	def test_expand_closes_empty_tags(self) -> None:
		self.assertEqual(expand('{target}_{source}', { 'target': 'clip', 'source': '' }), 'clip')
		self.assertEqual(expand('{source}_{target}', { 'target': 'clip', 'source': '' }), 'clip')

	def test_expand_keeps_unknown_tags(self) -> None:
		self.assertEqual(expand('{target}_{foo}', { 'target': 'clip' }), 'clip_{foo}')
		self.assertEqual(unknown_tags('{target}_{foo}'), [ '{foo}' ])

	def test_clean_name(self) -> None:
		self.assertEqual(clean_name('a:b?. '), 'a_b_')

	def test_automatic_name_never_overwrites(self) -> None:
		with tempfile.TemporaryDirectory() as folder:
			values = { 'target': 'clip', 'processors': 'swap' }
			path, replaces = build_output_path('', values, folder, '.mp4')
			self.assertEqual(path, os.path.join(folder, 'clip_swap.mp4'))
			self.assertFalse(replaces)
			open(path, 'w').close()
			path, replaces = build_output_path('', values, folder, '.mp4')
			self.assertEqual(path, os.path.join(folder, 'clip_swap_2.mp4'))
			self.assertFalse(replaces)

	def test_typed_name_replaces(self) -> None:
		with tempfile.TemporaryDirectory() as folder:
			open(os.path.join(folder, 'clip.mp4'), 'w').close()
			path, replaces = build_output_path('{target}', { 'target': 'clip' }, folder, '.mp4')
			self.assertEqual(path, os.path.join(folder, 'clip.mp4'))
			self.assertTrue(replaces)

	def test_counter_skips_taken_paths(self) -> None:
		with tempfile.TemporaryDirectory() as folder:
			taken = { os.path.normcase(os.path.join(folder, 'clip-1.mp4')) }
			path, _ = build_output_path('{target}-{n}', { 'target': 'clip' }, folder, '.mp4', taken)
			self.assertEqual(path, os.path.join(folder, 'clip-2.mp4'))

	def test_only_media_extensions_count(self) -> None:
		with tempfile.TemporaryDirectory() as folder:
			path, _ = build_output_path('clip.v2', {}, folder, '.mp4')
			self.assertEqual(path, os.path.join(folder, 'clip.v2.mp4'))
			path, _ = build_output_path('clip.png', {}, folder, '.mp4')
			self.assertEqual(path, os.path.join(folder, 'clip.png'))

	def test_subfolders(self) -> None:
		with tempfile.TemporaryDirectory() as folder:
			path, _ = build_output('{target}', { 'target': 'clip', 'source': 'Ann' }, folder, '.jpg', True)
			self.assertEqual(path, os.path.join(folder, 'Ann', 'Images', 'clip.jpg'))
			path, _ = build_output('{target}', { 'target': 'clip', 'source': '' }, folder, '.mp4', True)
			self.assertEqual(path, os.path.join(folder, 'No source', 'Videos', 'clip.mp4'))


SCHEMA = Schema(
{
	'processors': [ 'face_swapper', 'face_enhancer' ],
	'options':
	{
		'processors': { 'flag': '--processors', 'group': 'processors', 'default': [ 'face_swapper' ], 'multiple': True },
		'face_detector_score': { 'flag': '--face-detector-score', 'group': 'face_detector', 'default': 0.5 },
		'face_detector_angles': { 'flag': '--face-detector-angles', 'group': 'face_detector', 'default': [ 0 ], 'multiple': True },
		'keep_temp': { 'flag': '--keep-temp', 'group': 'misc', 'type': 'bool', 'default': False },
		'face_detector_model': { 'flag': '--face-detector-model', 'group': 'face_detector', 'default': 'yolo_face', 'choices': [ 'yolo_face', 'retinaface' ] },
		'face_detector_size': { 'flag': '--face-detector-size', 'group': 'face_detector', 'default': '640x640', 'choices': [ '640x640' ] },
		'face_enhancer_blend': { 'flag': '--face-enhancer-blend', 'group': 'face_enhancer', 'default': 80 }
	},
	'dependencies':
	{
		'face_detector_size': { 'driver': 'face_detector_model', 'variants': { 'retinaface': { 'choices': [ '320x320', '640x640' ] } } }
	}
})


class BuildOptionArgsTest(unittest.TestCase):
	def test_defaults_are_left_out(self) -> None:
		self.assertEqual(build_option_args(SCHEMA, { 'face_detector_score': 0.5, 'keep_temp': False, 'face_detector_angles': [ 0 ] }), [])

	def test_changed_values(self) -> None:
		args = build_option_args(SCHEMA, { 'face_detector_score': 0.60, 'keep_temp': True, 'face_detector_angles': [ 0, 90 ] })
		self.assertEqual(args, [ '--face-detector-score', '0.6', '--face-detector-angles', '0', '90', '--keep-temp' ])

	def test_managed_options_are_left_out(self) -> None:
		self.assertEqual(build_option_args(SCHEMA, { 'processors': [ 'face_enhancer' ] }), [])

	def test_options_of_processors_that_are_off(self) -> None:
		self.assertEqual(build_option_args(SCHEMA, { 'face_enhancer_blend': 50 }), [])
		self.assertEqual(build_option_args(SCHEMA, { 'processors': [ 'face_enhancer' ], 'face_enhancer_blend': 50 }), [ '--face-enhancer-blend', '50' ])

	def test_choices_follow_the_driver(self) -> None:
		self.assertEqual(build_option_args(SCHEMA, { 'face_detector_size': '320x320' }), [])
		self.assertEqual(build_option_args(SCHEMA, { 'face_detector_model': 'retinaface', 'face_detector_size': '320x320' }), [ '--face-detector-model', 'retinaface', '--face-detector-size', '320x320' ])

	def test_groups(self) -> None:
		self.assertEqual(build_option_args(SCHEMA, { 'face_detector_score': 0.7, 'keep_temp': True }, [ 'misc' ]), [ '--keep-temp' ])


class FaceMappingTest(unittest.TestCase):
	def test_pass_distance(self) -> None:
		self.assertEqual(pass_distance({}, 0.3), 0.3)
		self.assertEqual(pass_distance({ 'nearest': 0.4 }, 0.3), 0.3)
		self.assertEqual(pass_distance({ 'nearest': 0.2 }, 0.3), 0.15)
		self.assertEqual(pass_distance({ 'nearest': 0.04 }, 0.3), 0.05)

	def test_nothing_mapped(self) -> None:
		self.assertEqual(plan_passes([ { 'source': '' }, { 'source': '' } ], [ 'a.jpg' ], [ 'a.jpg' ], 0.3), [])

	def test_same_sources_run_once(self) -> None:
		passes = plan_passes([ { 'source': SELECTED }, { 'source': SELECTED } ], [ 'a.jpg' ], [ 'a.jpg' ], 0.3)
		self.assertEqual(len(passes), 1)
		self.assertTrue(passes[0].get('many'))
		self.assertEqual(passes[0].get('people'), [ 0, 1 ])

	def test_a_pass_per_person(self) -> None:
		people = [ { 'source': 'a.jpg', 'frame': 4, 'position': 1, 'nearest': 0.2, 'name': 'Ann' }, { 'source': '' }, { 'source': 'b.jpg' } ]
		passes = plan_passes(people, [ 'a.jpg' ], [ 'a.jpg', 'b.jpg' ], 0.3)
		self.assertEqual([ item.get('person') for item in passes ], [ 0, 2 ])
		self.assertEqual(passes[0].get('sources'), [ 'a.jpg' ])
		self.assertEqual((passes[0].get('frame'), passes[0].get('position'), passes[0].get('distance'), passes[0].get('label')), (4, 1, 0.15, 'Ann'))
		self.assertEqual(passes[1].get('label'), 'Face 3')


class VersionTest(unittest.TestCase):
	def test_is_newer_version(self) -> None:
		self.assertTrue(is_newer_version('v0.3.1', '0.3'))
		self.assertTrue(is_newer_version('v0.10', '0.9'))
		self.assertFalse(is_newer_version('v0.3.0', '0.3'))
		self.assertFalse(is_newer_version('v0.3', '0.3.0'))
		self.assertFalse(is_newer_version('v0.2.9', '0.3'))
		self.assertFalse(is_newer_version('latest', '0.3'))
		# the release names after v0.30
		self.assertTrue(is_newer_version('v0.40', '0.30'))
		self.assertTrue(is_newer_version('v1.00', '0.30'))
		self.assertTrue(is_newer_version('v1.00', '0.40'))
		self.assertFalse(is_newer_version('v0.30', '0.30'))
		self.assertFalse(is_newer_version('v0.2', '0.30'))


class ProgressParsingTest(unittest.TestCase):
	def run_output(self, text : str) -> tuple:
		runner = ProcessRunner()
		lines, progress = [], []
		runner.log.connect(lines.append)
		runner.progress.connect(lambda title, ratio, status: progress.append((title, ratio, status)))
		runner.buffer = text
		runner.flush_buffer(final = True)
		return lines, progress

	def test_lines(self) -> None:
		lines, progress = self.run_output('\x1b[32mfirst\x1b[0m\nsecond\rthird\n\n')
		self.assertEqual(lines, [ 'first', 'second', 'third' ])
		self.assertEqual(progress, [])

	def test_progress_bar(self) -> None:
		# as facefusion's cli_progress.render writes it, ending in erase line
		lines, progress = self.run_output('before\n\x1b[Gprocessing \x1b[94m===\x1b[90m=\x1b[0m 75% | 30/40 frames\x1b[K\nafter\n')
		self.assertEqual(lines, [ 'before', 'after' ])
		self.assertEqual(progress, [ ('processing', 0.75, '75%  ·  30/40 frames') ])

	def test_growing_status_waits(self) -> None:
		runner = ProcessRunner()
		progress = []
		runner.progress.connect(lambda title, ratio, status: progress.append(ratio))
		runner.buffer = '\x1b[Gprocessing \x1b[94m=\x1b[90m===\x1b[0m 25'
		runner.flush_buffer(final = False)
		self.assertEqual(progress, [])
		runner.flush_buffer(final = True)
		self.assertEqual(progress, [ 0.25 ])


class WorkerOutputTest(unittest.TestCase):
	def test_output_and_result_split(self) -> None:
		worker = PreviewWorker()
		worker.process = mock.Mock()
		worker.running_id = 1
		output, done = [], []
		worker.output.connect(output.append)
		worker.done.connect(done.append)
		bar = '\x1b[Gprocessing \x1b[94m==\x1b[90m==\x1b[0m 50%\x1b[K'
		# the result line arrives in two pieces, right after a progress bar
		for chunk in [ 'log line\n' + bar, 'PREV', 'IEW_DONE {"id": 1, "code": 0}\nidle talk\n' ]:
			worker.process.readAllStandardOutput.return_value.data.return_value = chunk.encode()
			worker.on_output()
		self.assertEqual(''.join(output), 'log line\n' + bar)
		self.assertEqual(done, [ 0 ])
		worker.idle_timer.stop()


class VideoReaderTest(unittest.TestCase):
	def test_reads_in_any_order(self) -> None:
		with tempfile.TemporaryDirectory() as folder:
			path = os.path.join(folder, 'frames.avi')
			# each frame is one flat gray level, lossless so the levels read back exactly
			writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*'FFV1'), 10, (32, 32))
			for level in range(0, 100, 10):
				writer.write(numpy.full((32, 32, 3), level, numpy.uint8))
			writer.release()
			reader = VideoReader(path)
			try:
				for index in [ 0, 1, 2, 2, 7, 8, 3, 0, 9 ]:
					self.assertEqual(int(reader.read(index)[0, 0, 0]), index * 10)
				self.assertIsNone(reader.read(10))
				self.assertEqual(int(reader.read(4)[0, 0, 0]), 40)
			finally:
				reader.release()


class DownloadReleaseTest(unittest.TestCase):
	def make_zip(self, folder : str) -> str:
		path = os.path.join(folder, 'release.zip')
		with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
			archive.writestr('owner-repo-abc123/ffstudio/__init__.py', '# ' + 'studio ' * 200)
		return path

	def test_good_download(self) -> None:
		with tempfile.TemporaryDirectory() as folder:
			source = download_release(pathlib.Path(self.make_zip(folder)).as_uri(), lambda done, total: None)
			self.assertEqual(os.path.basename(source), 'owner-repo-abc123')

	def test_damaged_download(self) -> None:
		with tempfile.TemporaryDirectory() as folder:
			path = self.make_zip(folder)
			data = bytearray(open(path, 'rb').read())
			data[60] ^= 0xff
			open(path, 'wb').write(bytes(data))
			with self.assertRaises(RuntimeError):
				download_release(pathlib.Path(path).as_uri(), lambda done, total: None)

	def test_not_a_zip(self) -> None:
		with tempfile.TemporaryDirectory() as folder:
			path = os.path.join(folder, 'release.zip')
			open(path, 'w').write('<html>rate limited</html>')
			with self.assertRaises(RuntimeError):
				download_release(pathlib.Path(path).as_uri(), lambda done, total: None)


if __name__ == '__main__':
	unittest.main()


class DataFolderTest(unittest.TestCase):
	def test_moves_old_folder_and_its_paths(self) -> None:
		saved = os.environ.pop('FFSTUDIO_DATA_DIR', None)
		try:
			with tempfile.TemporaryDirectory() as folder:
				old_dir = os.path.join(folder, 'FaceFusionStudio')
				new_dir = os.path.join(folder, 'StudioFF')
				os.makedirs(os.path.join(old_dir, 'people'))
				picture = os.path.join(old_dir, 'people', 'a.jpg')
				with open(os.path.join(old_dir, 'config.json'), 'w', encoding = 'utf-8') as file:
					json.dump({ 'theme': 'dark', 'people': [ picture ], 'target_path': os.path.join('C:' + os.sep, 'clip.mp4') }, file)
				migrate_data_dir(old_dir, new_dir)
				self.assertFalse(os.path.exists(old_dir))
				with open(os.path.join(new_dir, 'config.json'), encoding = 'utf-8') as file:
					data = json.load(file)
				self.assertEqual(data['people'], [ os.path.join(new_dir, 'people', 'a.jpg') ])
				self.assertEqual(data['target_path'], os.path.join('C:' + os.sep, 'clip.mp4'))
				self.assertEqual(data['theme'], 'dark')
				# a second start leaves everything alone
				os.makedirs(old_dir)
				migrate_data_dir(old_dir, new_dir)
				self.assertTrue(os.path.isdir(old_dir))
		finally:
			if saved is not None:
				os.environ['FFSTUDIO_DATA_DIR'] = saved


class AppConfigTest(unittest.TestCase):
	def test_a_file_held_for_a_moment_is_read_again(self) -> None:
		with tempfile.TemporaryDirectory() as data_dir:
			config_file = os.path.join(data_dir, 'config.json')
			with open(config_file, 'w', encoding = 'utf-8') as file:
				json.dump({ 'facefusion_root': 'C:\ff' }, file)
			real_open = open
			calls = []

			def busy_once(*args, **kwargs):
				calls.append(args[0])
				if len(calls) == 1:
					raise PermissionError('held by another program')
				return real_open(*args, **kwargs)
			with mock.patch.object(config, 'CONFIG_FILE', config_file), mock.patch('builtins.open', busy_once):
				app_config = AppConfig()
			self.assertTrue(app_config.loaded)
			self.assertEqual(app_config.get('facefusion_root'), 'C:\ff')

	def test_an_unread_file_is_moved_aside_not_written_over(self) -> None:
		with tempfile.TemporaryDirectory() as data_dir:
			config_file = os.path.join(data_dir, 'config.json')
			with open(config_file, 'w', encoding = 'utf-8') as file:
				file.write('{ "facefusion_root": ')
			with mock.patch.object(config, 'CONFIG_FILE', config_file), mock.patch.object(config, 'DATA_DIR', data_dir), mock.patch('time.sleep'):
				app_config = AppConfig()
				self.assertFalse(app_config.loaded)
				self.assertEqual(app_config.get('facefusion_root'), '')
				app_config.set('theme', 'dark')
				app_config.save()
			kept = [ name for name in os.listdir(data_dir) if name.startswith('config.unread-') ]
			self.assertEqual(len(kept), 1)
			with open(os.path.join(data_dir, kept[0]), encoding = 'utf-8') as file:
				self.assertEqual(file.read(), '{ "facefusion_root": ')
			with open(config_file, encoding = 'utf-8') as file:
				self.assertEqual(json.load(file)['theme'], 'dark')


class RetiredFilesTest(unittest.TestCase):
	def test_every_removed_top_level_file_is_retired(self) -> None:
		git = shutil.which('git')
		if not git or not os.path.isdir(os.path.join(APP_ROOT, '.git')):
			self.skipTest('needs the git checkout')
		log = subprocess.run([ git, '-C', APP_ROOT, 'log', '--diff-filter=DR', '--name-status', '--format=' ], capture_output = True, text = True, check = True).stdout
		current = set(subprocess.run([ git, '-C', APP_ROOT, 'ls-files' ], capture_output = True, text = True, check = True).stdout.splitlines())
		removed = { line.split('	')[1] for line in log.splitlines() if line[:1] in 'DR' and '	' in line }
		missing = sorted(path for path in removed - current if not path.startswith('ffstudio/') and path not in RETIRED_FILES)
		self.assertEqual(missing, [])
