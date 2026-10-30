import os
import re
from typing import Dict, List, Optional, Set, Tuple

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
# stands in for a tag that came out empty, until the separators around it are tidied
EMPTY_TAG = ''
# an empty tag with the separators on either side of it, several empty tags in a row count as one
EMPTY_GAP = re.compile(r'([_\-. ]*)%s(?:[_\-. ]*%s)*([_\-. ]*)' % (EMPTY_TAG, EMPTY_TAG))


def unknown_tags(template : str) -> List[str]:
	known = { tag for tag, _ in NAME_TAGS }
	return [ match.group(0) for match in TAG_PATTERN.finditer(template) if match.group(1).lower() not in known ]


def clean_name(name : str) -> str:
	# windows drops spaces and dots at the end of a name
	return INVALID_CHARACTERS.sub('_', name).strip(' .')


def close_empty_tags(template : str) -> str:
	"""an empty tag leaves its separators behind, "clip__swapper" or "_clip" reads better as "clip_swapper" / "clip"

	only the template is tidied, separators typed between two filled tags (like " - ") and the names put in stay as they are
	"""
	def close_gap(match : re.Match) -> str:
		if match.start() == 0 or match.end() == len(template):
			return ''
		return match.group(1) or match.group(2)
	return EMPTY_GAP.sub(close_gap, template)


def expand(template : str, values : Dict[str, str], counter : Optional[int] = None) -> str:
	"""replaces the tags in template, unknown tags stay as typed, {n} becomes counter (or 1)"""
	def mark_empty(match : re.Match) -> str:
		tag = match.group(1).lower()
		return EMPTY_TAG if tag in values and tag != 'n' and not str(values.get(tag) or '') else match.group(0)

	def replace(match : re.Match) -> str:
		tag = match.group(1).lower()
		if tag == 'n':
			return str(counter or 1)
		if tag in values:
			return str(values.get(tag) or '')
		return match.group(0)
	return clean_name(TAG_PATTERN.sub(replace, close_empty_tags(TAG_PATTERN.sub(mark_empty, template))))


def split_extension(name : str, fallback : str) -> Tuple[str, str]:
	# only a real media extension counts, "clip.v2" is a name and not a ".v2" file
	stem, ext = os.path.splitext(name)
	if ext and media_kind(name) in [ 'image', 'video' ]:
		return stem, ext
	return name, fallback


def build_output_path(template : str, values : Dict[str, str], output_dir : str, fallback_ext : str, taken : Optional[Set[str]] = None) -> Tuple[str, bool]:
	"""returns the output path and whether it replaces an existing file

	an empty template is the automatic name, which like {n} never overwrites: it adds _2, _3 ... instead
	taken holds paths planned for other outputs of the same batch, they count as existing files
	"""
	taken = taken or set()

	def exists(path : str) -> bool:
		return os.path.exists(path) or os.path.normcase(path) in taken

	automatic = not template.strip()
	template = template.strip() or AUTOMATIC_TEMPLATE
	counting = bool(re.search(r'\{n\}', template, re.IGNORECASE))
	stem, ext = split_extension(expand(template, values), fallback_ext)
	path = os.path.join(output_dir, (stem or 'output') + ext)
	if counting:
		counter = 1
		while exists(path):
			counter += 1
			stem, ext = split_extension(expand(template, values, counter), fallback_ext)
			path = os.path.join(output_dir, (stem or 'output') + ext)
	elif automatic:
		base, counter = stem or 'output', 2
		while exists(path):
			path = os.path.join(output_dir, '%s_%d%s' % (base, counter, ext))
			counter += 1
	return path, exists(path)


def build_output(template : str, values : Dict[str, str], output_dir : str, fallback_ext : str, subfolders : bool, taken : Optional[Set[str]] = None, animated : bool = False) -> Tuple[str, bool]:
	"""the output path and whether it replaces a file, inside <source name>\\Images, Videos or Animated GIFs when subfolders is on.
	animated is for a gif or animated webp target, whose result is an animation even when its name ends in .webp"""
	path, replaces = build_output_path(template, values, output_dir, fallback_ext, taken)
	if subfolders:
		# a typed extension can turn an image target into a video, so the kind comes from the built name
		if animated or path.lower().endswith('.gif'):
			kind = 'Animated GIFs'
		else:
			kind = 'Images' if media_kind(path) == 'image' else 'Videos'
		folder = os.path.join(output_dir, clean_name(values.get('source') or '') or 'No source', kind)
		path, replaces = build_output_path(template, values, folder, fallback_ext, taken)
	return path, replaces
