"""hides who and where you are in text you share: the user name, the computer name, and the folders you picked"""
import os
import re
import socket
import sys
import tempfile
from typing import Iterable, List, Optional

USER_NAME = os.path.basename(os.path.expanduser('~').rstrip('\\/'))
USER_NAME_STAND_IN = '%USERNAME%'
COMPUTER_NAME = os.environ.get('COMPUTERNAME') or socket.gethostname().split('.')[0]
COMPUTER_NAME_STAND_IN = '%COMPUTERNAME%'
# the folders between the drive and the file name of a path that is not one of StudioFF's or FaceFusion's own
FOLDERS_STAND_IN = '…'


def name_pattern(name : str, path_part : bool) -> Optional[re.Pattern]:
	# a name shorter than 3 letters would be found inside too many other words
	if len(name) < 3:
		return None
	# windows' 8.3 short form of a long name, C:\Users\JOHNSM~1
	short = name.replace(' ', '')[:6]
	names = re.escape(name) + ('|' + re.escape(short) + r'~\d+' if len(name) > 8 else '')
	if path_part:
		# only as a whole path part, so a name like "ann" is not taken out of "Scan"
		return re.compile(r'(?<=[\\/])(?:' + names + r')(?=[\\/\s"\',;]|$)', re.IGNORECASE)
	return re.compile(r'(?<![\w\-])(?:' + names + r')(?![\w\-])', re.IGNORECASE)


USER_NAME_PATTERN = name_pattern(USER_NAME, True)
COMPUTER_NAME_PATTERN = name_pattern(COMPUTER_NAME, False)
# a path in quotes may have spaces, one without ends at the first space. not the slashes of 12.3it/s or a url
PATH_PATTERN = re.compile(
	r'(?<=")[A-Za-z]:[\\/][^"\r\n]*(?=")'
	r'|(?<=\')[A-Za-z]:[\\/][^\'\r\n]*(?=\')'
	r'|(?<![\w"\'])[A-Za-z]:[\\/][^\s"\'<>|,;]*'
	r'|(?<![\w"\'/:.])/[\w.\-][^\s"\'<>|,;]*')
SEPARATOR_PATTERN = re.compile(r'[\\/]+')


def comparable(path : str) -> str:
	path = SEPARATOR_PATTERN.sub(lambda _: os.sep, path.strip()).rstrip(os.sep)
	return os.path.normcase(path)


def program_folders(facefusion_root : str = '', python_path : str = '') -> List[str]:
	"""the folders whose paths stay readable when folders are hidden: they say nothing about you, and help find a problem"""
	folders = [ facefusion_root, os.path.dirname(os.path.dirname(os.path.abspath(__file__))), tempfile.gettempdir(), sys.prefix ]
	if python_path:
		environment = os.path.dirname(python_path)
		# a venv keeps python in Scripts or bin, a conda environment beside its own folder
		folders.append(os.path.dirname(environment) if os.path.basename(environment).lower() in [ 'scripts', 'bin' ] else environment)
	from ffstudio.config import DATA_DIR
	folders.append(DATA_DIR)
	if sys.platform == 'win32':
		folders.extend(os.environ.get(name, '') for name in [ 'SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData' ])
	else:
		folders.extend([ '/usr', '/opt', '/bin', '/lib', '/System', '/Applications' ])
	return [ comparable(folder) for folder in folders if folder ]


def hide_path_folders(path : str, keep : Iterable[str]) -> str:
	"""M:\\Clients\\Smith\\photo.png reads M:\\…\\photo.png, the file name stays to tell what it was"""
	compared = comparable(path)
	if any(compared == folder or compared.startswith(folder + os.sep) for folder in keep):
		return path
	start = re.match(r'[A-Za-z]:[\\/]+|/+', path)
	head, rest = path[:start.end()], path[start.end():]
	parts = SEPARATOR_PATTERN.split(rest)
	# C:\photo.png has no folder to hide
	if len(parts) < 2:
		return path
	separator = SEPARATOR_PATTERN.findall(rest)[-1]
	return head + FOLDERS_STAND_IN + separator + parts[-1]


def hide_private(text : str, names : bool = True, folders : bool = False, keep : Iterable[str] = ()) -> str:
	if folders:
		keep = list(keep)
		text = PATH_PATTERN.sub(lambda match: hide_path_folders(match.group(), keep), text)
	if names:
		if USER_NAME_PATTERN:
			text = USER_NAME_PATTERN.sub(USER_NAME_STAND_IN, text)
		if COMPUTER_NAME_PATTERN:
			text = COMPUTER_NAME_PATTERN.sub(COMPUTER_NAME_STAND_IN, text)
	return text
