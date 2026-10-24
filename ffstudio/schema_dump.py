"""
Runs inside the FaceFusion Python environment (NOT the UI environment).

Builds FaceFusion's own argparse program and dumps every option of the
`run` command as JSON, so the UI can render controls that always match the
installed FaceFusion version.

usage: python schema_dump.py <facefusion_root> <output_json> [config_path]
"""
import json
import os
import sys
from argparse import _HelpAction, _StoreConstAction, _SubParsersAction, _VersionAction
from functools import lru_cache, partial

NUMERIC_RANGE_THRESHOLD = 12


def to_json_value(value):
	if isinstance(value, (str, int, float, bool)) or value is None:
		return value
	if isinstance(value, (list, tuple, range)):
		return [ to_json_value(item) for item in value ]
	return str(value)


def is_numeric(values):
	return bool(values) and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values)


def describe_range(values):
	values = list(values)
	step = values[1] - values[0] if len(values) > 1 else 1
	if isinstance(step, float):
		step = round(step, 4)
	return { 'min': values[0], 'max': values[-1], 'step': step }


def describe_action(action, group_name):
	value_type = 'str'
	choices = list(action.choices) if action.choices is not None else None
	value_range = None

	if isinstance(action, _StoreConstAction):
		value_type = 'bool'
	elif action.type is int:
		value_type = 'int'
	elif action.type is float:
		value_type = 'float'
	elif isinstance(action.type, partial):
		int_range = action.type.keywords.get('int_range')
		if int_range is not None:
			value_type = 'int'
			value_range = describe_range(int_range)

	if choices and is_numeric(choices):
		if value_type == 'str':
			value_type = 'float' if any(isinstance(choice, float) for choice in choices) else 'int'
		if len(choices) > NUMERIC_RANGE_THRESHOLD:
			value_range = describe_range(choices)
			choices = None

	return\
	{
		'dest': action.dest,
		'flag': max(action.option_strings, key = len),
		'help': action.help or '',
		'default': to_json_value(action.default),
		'choices': to_json_value(choices),
		'range': value_range,
		'type': value_type,
		'multiple': action.nargs in [ '+', '*' ] or isinstance(action.nargs, int) and action.nargs > 1,
		'group': group_name
	}


def find_sub_program(program, command):
	for action in program._actions:
		if isinstance(action, _SubParsersAction):
			return action.choices.get(command)
	return None


def build_options(program_module, capability_store, extra_args):
	sys.argv = [ 'facefusion.py', 'run' ] + extra_args
	program = program_module.create_program()
	run_program = find_sub_program(program, 'run')
	cli_set = capability_store.get_cli_capability_set()
	options = {}

	for group in run_program._action_groups:
		for action in group._group_actions:
			if isinstance(action, (_HelpAction, _VersionAction)) or not action.option_strings:
				continue
			capability = cli_set.get(action.dest) or capability_store.get_sys_capability_set().get(action.dest) or {}
			group_name = (capability.get('groups') or [ group.title ])[0]
			options[action.dest] = describe_action(action, group_name)

	return options


def main():
	facefusion_root = os.path.abspath(sys.argv[1])
	output_path = os.path.abspath(sys.argv[2])
	config_path = sys.argv[3] if len(sys.argv) > 3 else 'facefusion.ini'

	os.chdir(facefusion_root)
	sys.path.insert(0, facefusion_root)

	import facefusion.choices
	import facefusion.program as program_module
	from facefusion import capability_store, metadata, state_manager

	state_manager.init()
	state_manager.init_item('config_path', config_path)
	# ffmpeg is probed for encoders on every program build, probe it once
	program_module.get_available_encoder_set = lru_cache(program_module.get_available_encoder_set)

	config_args = [ '--config-path', config_path ]
	options = build_options(program_module, capability_store, config_args)

	# options whose choices depend on a selected model (e.g. detector size, pixel boost)
	dependencies = {}
	for driver in list(options.values()):
		if not driver.get('dest').endswith('_model') or not driver.get('choices'):
			continue
		for choice in driver.get('choices'):
			variant = build_options(program_module, capability_store, config_args + [ driver.get('flag'), str(choice) ])
			for dest, option in variant.items():
				if dest == driver.get('dest') or dest not in options:
					continue
				base = options.get(dest)
				if option.get('choices') != base.get('choices') or option.get('range') != base.get('range'):
					dependencies.setdefault(dest, { 'driver': driver.get('dest'), 'variants': {} })
					dependencies[dest]['variants'][str(choice)] =\
					{
						'choices': option.get('choices'),
						'range': option.get('range'),
						'default': option.get('default')
					}
	for dest, dependency in dependencies.items():
		driver_default = options.get(dependency.get('driver')).get('default')
		dependency['variants'].setdefault(str(driver_default),
		{
			'choices': options.get(dest).get('choices'),
			'range': options.get(dest).get('range'),
			'default': options.get(dest).get('default')
		})

	schema =\
	{
		'name': metadata.get('name'),
		'version': metadata.get('version'),
		'python': sys.version.split()[0],
		'facefusion_root': facefusion_root,
		'formats':
		{
			'audio': to_json_value(facefusion.choices.audio_formats),
			'image': to_json_value(facefusion.choices.image_formats),
			'video': to_json_value(facefusion.choices.video_formats)
		},
		'processors': options.get('processors', {}).get('choices') or [],
		'download_scopes': to_json_value(getattr(facefusion.choices, 'download_scopes', [ 'lite', 'full' ])),
		'options': options,
		'dependencies': dependencies
	}

	with open(output_path, 'w', encoding = 'utf-8') as output_file:
		json.dump(schema, output_file, indent = 1)


if __name__ == '__main__':
	main()
