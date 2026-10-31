import json
import os
from typing import Any, Dict, List, Optional, Tuple

from ffstudio.config import SCHEMA_FILE

GROUP_TITLES : Dict[str, str] =\
{
	'paths': 'Paths',
	'face_detector': 'Face detector',
	'face_aligner': 'Face aligner',
	'face_selector': 'Face selector',
	'face_tracker': 'Face tracker',
	'face_masker': 'Face masker',
	'voice_extractor': 'Voice extractor',
	'frame_extraction': 'Frame extraction',
	'frame_distribution': 'Frame distribution',
	'output_creation': 'Output',
	'workflow': 'Workflow',
	'processors': 'Processors',
	'execution': 'Execution',
	'download': 'Download',
	'memory': 'Memory',
	'misc': 'Misc'
}
# options the UI drives through dedicated controls instead of generic option rows
MANAGED_OPTIONS = [ 'config_path', 'source_paths', 'target_path', 'output_path', 'processors', 'face_selector_maps' ]
# facefusion picks the reference face with --face-selector-map "to <frame>:<position>",
# these two keep their rows and are turned into that map. the position counts the faces largest first
MAP_OPTIONS =\
{
	'reference_face_position': { 'help': 'specify the position used to create the reference face, counting the faces largest first', 'after': 'face_selector_maps' },
	'reference_frame_index': { 'help': 'specify the frame used to create the reference face', 'after': 'reference_face_distance' }
}
# split_label cuts these after the first word, the first two words would leave a name that says little
SPLIT_AFTER_ONE = [ 'temp_frame_format', 'temp_pixel_format', 'reference_frame_index', 'target_frame_amount' ]
# short labels split_label cannot make, two rows of the face masker would both be called Model
SHORT_LABELS =\
{
	'face_occluder_model': 'Occluder',
	'face_parser_model': 'Parser'
}
# output options keep their image, audio or video word, the output box has a Quality, Encoder, Scale and FPS of several
SPLIT_AFTER_ONE_PREFIXES = [ 'output_' ]


def humanize(name : str) -> str:
	return name.replace('_', ' ').replace('-', ' ').strip().capitalize()


class Schema:
	def __init__(self, data : Dict[str, Any]) -> None:
		self.data = data
		self.options : Dict[str, Dict[str, Any]] = data.get('options', {})
		self.dependencies : Dict[str, Dict[str, Any]] = data.get('dependencies', {})
		self.processors : List[str] = data.get('processors', [])
		self.options = add_map_options(self.options)

	@staticmethod
	def load(path : str = SCHEMA_FILE) -> Optional['Schema']:
		try:
			with open(path, encoding = 'utf-8') as schema_file:
				return Schema(json.load(schema_file))
		except (OSError, ValueError):
			return None

	@property
	def version(self) -> str:
		return self.data.get('version') or '?'

	@property
	def facefusion_root(self) -> str:
		return self.data.get('facefusion_root') or ''

	def formats(self, media : str) -> List[str]:
		return self.data.get('formats', {}).get(media, [])

	def group_names(self) -> List[str]:
		return list(dict.fromkeys(option.get('group') for option in self.options.values()))

	def group_options(self, group : str) -> List[Dict[str, Any]]:
		return [ option for option in self.options.values() if option.get('group') == group and option.get('dest') not in MANAGED_OPTIONS ]

	def group_title(self, group : str) -> str:
		return GROUP_TITLES.get(group) or humanize(group)

	def option_label(self, option : Dict[str, Any]) -> str:
		dest = option.get('dest')
		group = option.get('group')
		if group in self.processors and dest.startswith(group + '_'):
			dest = dest[len(group) + 1:]
		return humanize(dest)

	def split_label(self, option : Dict[str, Any]) -> Tuple[str, str]:
		"""the label without its leading group words, plus those words: face_selector_gender gives ('Gender', 'Face selector')"""
		dest = option.get('dest')
		group = option.get('group')
		if dest in SHORT_LABELS:
			return SHORT_LABELS.get(dest), self.group_title(group)
		if dest.startswith(group + '_'):
			return humanize(dest[len(group) + 1:]), self.group_title(group)
		words = dest.split('_')
		cut = 1 if dest in SPLIT_AFTER_ONE or any(dest.startswith(prefix) for prefix in SPLIT_AFTER_ONE_PREFIXES) else 2
		if len(words) > 2:
			return ' '.join('FPS' if word.lower() == 'fps' else word for word in humanize('_'.join(words[cut:])).split(' ')), humanize('_'.join(words[:cut]))
		return humanize(dest), ''

	def resolve(self, dest : str, values : Dict[str, Any]) -> Dict[str, Any]:
		"""returns the option with choices / range / default adjusted to the currently selected driver model"""
		option = dict(self.options.get(dest, {}))
		dependency = self.dependencies.get(dest)
		if dependency:
			driver_value = values.get(dependency.get('driver'), self.options.get(dependency.get('driver'), {}).get('default'))
			variant = dependency.get('variants', {}).get(str(driver_value))
			if variant:
				option.update(variant)
		return option

	def drivers(self) -> List[str]:
		return list(dict.fromkeys(dependency.get('driver') for dependency in self.dependencies.values()))


def add_map_options(options : Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
	"""the options with the reference frame and position rows back in their old places, they have no flag of their own"""
	added : Dict[str, Dict[str, Any]] = {}
	for dest, option in options.items():
		added[dest] = option
		for map_dest, map_option in MAP_OPTIONS.items():
			if map_option.get('after') == dest and map_dest not in options:
				added[map_dest] = { 'dest': map_dest, 'flag': None, 'help': map_option.get('help'), 'default': 0, 'choices': None, 'range': None, 'type': 'int', 'multiple': False, 'group': 'face_selector', 'mapped': True }
	return added


def face_map_args(values : Dict[str, Any]) -> List[str]:
	"""--face-selector-map from the given maps, or from the reference frame and position, nothing for facefusion's own 'to 0:0'"""
	maps = list(values.get('face_selector_maps') or [])
	frame = int(values.get('reference_frame_index') or 0)
	position = int(values.get('reference_face_position') or 0)
	if not maps and (frame, position) != (0, 0):
		maps = [ 'to %d:%d' % (frame, position) ]
	return [ '--face-selector-map' ] + maps if maps else []


def format_value(value : Any) -> List[str]:
	if isinstance(value, list):
		return [ format_scalar(item) for item in value ]
	return [ format_scalar(value) ]


def format_scalar(value : Any) -> str:
	if isinstance(value, float):
		return ('%.4f' % value).rstrip('0').rstrip('.')
	return str(value)


def same_value(left : Any, right : Any) -> bool:
	if left is None or right is None:
		return left is None and right is None
	return format_value(left) == format_value(right)


def build_option_args(schema : Schema, values : Dict[str, Any], groups : Optional[List[str]] = None) -> List[str]:
	"""translates UI values into CLI flags, only passing values that differ from facefusion's own defaults"""
	args : List[str] = []
	active_processors = values.get('processors') or schema.options.get('processors', {}).get('default') or []

	if groups is None or 'face_selector' in groups:
		args.extend(face_map_args(values))
	for dest, option in schema.options.items():
		if dest in MANAGED_OPTIONS or dest not in values or option.get('mapped'):
			continue
		group = option.get('group')
		if groups is not None and group not in groups:
			continue
		if group in schema.processors and group not in active_processors:
			continue
		value = values.get(dest)
		resolved = schema.resolve(dest, values)
		if value is None or value == [] or value == '':
			continue
		if resolved.get('choices') and not option.get('multiple') and value not in resolved.get('choices'):
			continue
		if same_value(value, resolved.get('default')):
			continue
		if option.get('type') == 'bool':
			if value:
				args.append(option.get('flag'))
			continue
		args.append(option.get('flag'))
		args.extend(format_value(value))
	return args


def schema_is_stale(schema : Optional[Schema], facefusion_root : str) -> bool:
	if not schema or os.path.normcase(os.path.abspath(schema.facefusion_root)) != os.path.normcase(os.path.abspath(facefusion_root)):
		return True
	try:
		schema_time = os.path.getmtime(SCHEMA_FILE)
		watched = [ os.path.join(facefusion_root, 'facefusion', 'program.py'), os.path.join(facefusion_root, 'facefusion.ini'), os.path.join(facefusion_root, 'facefusion', 'choices.py') ]
		return any(os.path.getmtime(path) > schema_time for path in watched if os.path.exists(path))
	except OSError:
		return True
