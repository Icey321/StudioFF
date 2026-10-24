import os
import re
from typing import Dict, List, Optional, Tuple

from ffstudio.media import media_kind

# tag, what it stands for
NAME_TAGS : List[Tuple[str, str]] =\
[
	('target', 'Target file name, without its extension'),
	('source', 'Names of the sources in use, joined by +. Click a name under a source thumbnail to rename it'),
	('processors', 'Processors that are switched on, shortened'),
	('model', 'Model of the first processor that has one'),
	('frame', 'Frame the timeline is on'),
	('date', 'Date when the run starts'),
	('time', 'Time when the run starts'),
	('n', 'Counts up to the first number that is not taken yet, so nothing is overwritten')
]
NAME_EXAMPLES = [ '{target}_{source}', '{source}_on_{target}_{date}', '{target}_{model}_{n}' ]
AUTOMATIC_TEMPLATE = '{target}_{processors}'
TAG_PATTERN = re.compile(r'\{(\w+)\}')
INVALID_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def unknown_tags(template : str) -> List[str]:
	known = { tag for tag, _ in NAME_TAGS }
	return [ match.group(0) for match in TAG_PATTERN.finditer(template) if match.group(1).lower() not in known ]


def clean_name(name : str) -> str:
	name = INVALID_CHARACTERS.sub('_', name)
	# an empty tag leaves its separators behind, "clip__swapper" or "_clip" reads better as "clip_swapper" / "clip"
	name = re.sub(r'([_\-. ])[_\-. ]*(?=[_\-. ])', '', name)
	return name.strip('_-. ')


def expand(template : str, values : Dict[str, str], counter : Optional[int] = None) -> str:
	"""replaces the tags in template, unknown tags stay as typed, {n} becomes counter (or 1)"""
	def replace(match : re.Match) -> str:
		tag = match.group(1).lower()
		if tag == 'n':
			return str(counter or 1)
		if tag in values:
			return str(values.get(tag) or '')
		return match.group(0)
	return clean_name(TAG_PATTERN.sub(replace, template))


def split_extension(name : str, fallback : str) -> Tuple[str, str]:
	# only a real media extension counts, "clip.v2" is a name and not a ".v2" file
	stem, ext = os.path.splitext(name)
	if ext and media_kind(name) in [ 'image', 'video' ]:
		return stem, ext
	return name, fallback


def build_output_path(template : str, values : Dict[str, str], output_dir : str, fallback_ext : str) -> Tuple[str, bool]:
	"""returns the output path and whether it replaces an existing file

	an empty template is the automatic name, which like {n} never overwrites: it adds _2, _3 ... instead
	"""
	automatic = not template.strip()
	template = template.strip() or AUTOMATIC_TEMPLATE
	counting = bool(re.search(r'\{n\}', template, re.IGNORECASE))
	stem, ext = split_extension(expand(template, values), fallback_ext)
	path = os.path.join(output_dir, (stem or 'output') + ext)
	if counting:
		counter = 1
		while os.path.exists(path):
			counter += 1
			stem, ext = split_extension(expand(template, values, counter), fallback_ext)
			path = os.path.join(output_dir, (stem or 'output') + ext)
	elif automatic:
		base, counter = stem or 'output', 2
		while os.path.exists(path):
			path = os.path.join(output_dir, '%s_%d%s' % (base, counter, ext))
			counter += 1
	return path, os.path.exists(path)
