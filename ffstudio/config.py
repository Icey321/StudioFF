import json
import os
from typing import Any, Dict

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('FFSTUDIO_DATA_DIR') or os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'), 'FaceFusionStudio')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
SCHEMA_FILE = os.path.join(DATA_DIR, 'schema.json')
PREVIEW_DIR = os.path.join(DATA_DIR, 'preview')

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
	'source_paths': [],
	'target_path': '',
	'options': {},
	'batch': {}
}


class AppConfig:
	def __init__(self) -> None:
		self.data : Dict[str, Any] = dict(DEFAULT_CONFIG)
		self.load()

	def load(self) -> None:
		try:
			with open(CONFIG_FILE, encoding = 'utf-8') as config_file:
				self.data.update(json.load(config_file))
		except (OSError, ValueError):
			pass

	def save(self) -> None:
		os.makedirs(DATA_DIR, exist_ok = True)
		temp_file = CONFIG_FILE + '.tmp'
		with open(temp_file, 'w', encoding = 'utf-8') as config_file:
			json.dump(self.data, config_file, indent = 1)
		os.replace(temp_file, CONFIG_FILE)

	def get(self, key : str, fallback : Any = None) -> Any:
		return self.data.get(key, fallback)

	def set(self, key : str, value : Any) -> None:
		self.data[key] = value
