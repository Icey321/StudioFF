import json
import os
import shutil
import time
from typing import Any, Dict

APP_DIR = os.path.dirname(os.path.abspath(__file__))
APPDATA_DIR = os.environ.get('APPDATA') or os.path.expanduser('~')
DATA_DIR = os.environ.get('FFSTUDIO_DATA_DIR') or os.path.join(APPDATA_DIR, 'StudioFF')
# the app was called FaceFusion Studio before v0.40
LEGACY_DATA_DIR = os.path.join(APPDATA_DIR, 'FaceFusionStudio')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
SCHEMA_FILE = os.path.join(DATA_DIR, 'schema.json')
PREVIEW_DIR = os.path.join(DATA_DIR, 'preview')



def replace_path_prefix(value : Any, old_prefix : str, new_prefix : str) -> Any:
	if isinstance(value, str) and os.path.normcase(value).startswith(os.path.normcase(old_prefix + os.sep)):
		return new_prefix + value[len(old_prefix):]
	if isinstance(value, list):
		return [ replace_path_prefix(item, old_prefix, new_prefix) for item in value ]
	if isinstance(value, dict):
		return { key: replace_path_prefix(item, old_prefix, new_prefix) for key, item in value.items() }
	return value


def migrate_data_dir(old_dir : str = LEGACY_DATA_DIR, new_dir : str = DATA_DIR) -> None:
	"""moves the data folder of an older version to the new name once, and points the saved paths into it at the new folder"""
	if os.environ.get('FFSTUDIO_DATA_DIR') or os.path.exists(new_dir) or not os.path.isdir(old_dir):
		return
	# right after an update the helper may still hold its log open for a moment
	for attempt in range(10):
		try:
			os.rename(old_dir, new_dir)
			break
		except OSError:
			time.sleep(0.3)
	else:
		try:
			shutil.copytree(old_dir, new_dir)
		except OSError:
			return
		shutil.rmtree(old_dir, ignore_errors = True)
	config_file = os.path.join(new_dir, 'config.json')
	try:
		with open(config_file, encoding = 'utf-8') as file:
			data = json.load(file)
		with open(config_file, 'w', encoding = 'utf-8') as file:
			json.dump(replace_path_prefix(data, old_dir, new_dir), file, indent = 1)
	except (OSError, ValueError):
		pass


DEFAULT_CONFIG : Dict[str, Any] =\
{
	'facefusion_root': '',
	'python_path': '',
	'theme': 'auto',
	'output_dir': os.path.join(os.path.expanduser('~'), 'Videos', 'FaceFusion'),
	'open_output_when_done': False,
	# 0 keeps the preview models loaded until the app closes
	'preview_keep_loaded_minutes': 5,
	'output_subfolders': False,
	# graphics card, processor and memory use in the title bar
	'show_system_monitor': True,
	'source_paths': [],
	'target_path': '',
	'options': {},
	'batch': {}
}


class AppConfig:
	def __init__(self) -> None:
		self.data : Dict[str, Any] = dict(DEFAULT_CONFIG)
		# false when a settings file was there but could not be read, save then moves it aside instead of writing over it
		self.loaded = True
		self.load()

	def load(self) -> None:
		# another program, a virus scanner say, can hold the file for a moment, so a failed read is tried again
		for attempt in range(10):
			if not os.path.exists(CONFIG_FILE):
				return
			try:
				with open(CONFIG_FILE, encoding = 'utf-8') as config_file:
					self.data.update(json.load(config_file))
				return
			except (OSError, ValueError):
				time.sleep(0.2)
		self.loaded = False

	def save(self) -> None:
		os.makedirs(DATA_DIR, exist_ok = True)
		if not self.loaded and os.path.exists(CONFIG_FILE):
			try:
				os.replace(CONFIG_FILE, os.path.join(DATA_DIR, time.strftime('config.unread-%Y%m%d-%H%M%S.json')))
			except OSError:
				return
			self.loaded = True
		temp_file = CONFIG_FILE + '.tmp'
		with open(temp_file, 'w', encoding = 'utf-8') as config_file:
			json.dump(self.data, config_file, indent = 1)
		os.replace(temp_file, CONFIG_FILE)

	def get(self, key : str, fallback : Any = None) -> Any:
		return self.data.get(key, fallback)

	def set(self, key : str, value : Any) -> None:
		self.data[key] = value
