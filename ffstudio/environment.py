import glob
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QProcessEnvironment

from ffstudio.platforms import CREATE_NO_WINDOW, ENV_BIN, IS_LINUX, IS_WINDOWS,conda_candidates, conda_env_python, facefusion_library_path, python_patterns


def is_facefusion_root(path : str) -> bool:
	return bool(path) and os.path.isfile(os.path.join(path, 'facefusion.py')) and os.path.isdir(os.path.join(path, 'facefusion', 'processors'))


def search_bases(studio_root : str) -> List[str]:
	"""folders whose subfolders may hold a FaceFusion checkout, the closest to StudioFF first"""
	home = os.path.expanduser('~')
	bases = [ os.path.dirname(studio_root), home ] + [ os.path.join(home, name) for name in [ 'Documents', 'Desktop', 'Downloads', os.path.join('pinokio', 'api') ] ]
	if IS_WINDOWS:
		import ctypes
		import string
		# fixed local drives only, a network drive can take long to answer
		drives = [ letter + ':\\' for letter in string.ascii_uppercase if ctypes.windll.kernel32.GetDriveTypeW(letter + ':\\') == 3 ]
		bases += drives + [ os.path.join(drive, 'pinokio', 'api') for drive in drives ]
	else:
		bases += [ '/opt', '/srv' ]
	# compared lowercase on windows, the folder keeps its own spelling for the box
	unique : Dict[str, str] = {}
	for base in bases:
		if os.path.isdir(base):
			unique.setdefault(os.path.normcase(os.path.abspath(base)), os.path.abspath(base))
	return list(unique.values())


def find_facefusion_folders(studio_root : str) -> List[str]:
	"""FaceFusion checkouts in the usual places, v4 ones first, it only looks two folders deep so it stays quick"""
	found : List[str] = []
	for base in search_bases(studio_root):
		try:
			children = sorted(entry.path for entry in os.scandir(base) if entry.is_dir() and not entry.name.startswith(('.', '$')))
		except OSError:
			continue
		for child in children:
			# a pinokio app keeps facefusion one folder down
			for candidate in [ child, os.path.join(child, 'facefusion'), os.path.join(child, 'app') ]:
				if is_facefusion_root(candidate) and candidate not in found:
					found.append(candidate)
	# the major number only, the v4 branch's metadata says just "v4"
	found.sort(key = lambda root: version_tuple((read_facefusion_version(root) or '').lstrip('v'))[:1] < (4,))
	return found


def read_facefusion_version(root : str) -> Optional[str]:
	metadata_path = os.path.join(root, 'facefusion', 'metadata.py')
	try:
		with open(metadata_path, encoding = 'utf-8') as metadata_file:
			for line in metadata_file:
				if "'version'" in line:
					return line.split(':', 1)[1].strip().strip(',').strip("'\"")
	except OSError:
		pass
	return None


def run_git(root : str, args : List[str]) -> Optional[str]:
	try:
		output = subprocess.run([ 'git', '-C', root ] + args, capture_output = True, text = True, timeout = 10, creationflags = CREATE_NO_WINDOW)
	except (OSError, subprocess.SubprocessError):
		return None
	return output.stdout.strip() if output.returncode == 0 and output.stdout.strip() else None


def read_facefusion_build(root : str) -> Dict[str, str]:
	"""version from metadata.py plus the branch, commit and commit date of a git checkout, empty strings when unknown"""
	build = { 'version': read_facefusion_version(root) or '', 'branch': '', 'commit': '', 'date': '', 'repository': '' }
	if not os.path.isdir(os.path.join(root, '.git')):
		return build
	build['branch'] = run_git(root, [ 'rev-parse', '--abbrev-ref', 'HEAD' ]) or ''
	build['commit'] = run_git(root, [ 'rev-parse', 'HEAD' ]) or ''
	build['date'] = run_git(root, [ 'log', '-1', '--format=%cI' ]) or ''
	remote = run_git(root, [ 'remote', 'get-url', 'origin' ]) or ''
	if 'github.com' in remote:
		build['repository'] = remote.split('github.com', 1)[1].strip(':/').removesuffix('.git')
	return build


def fetch_github_json(path : str) -> Any:
	request = urllib.request.Request('https://api.github.com/' + path, headers = { 'Accept': 'application/vnd.github+json', 'User-Agent': 'StudioFF' })
	with urllib.request.urlopen(request, timeout = 15) as response:
		return json.loads(response.read().decode('utf-8'))


def switch_facefusion_commit(root : str, branch : str, commit : str) -> Dict[str, Any]:
	"""moves a facefusion git checkout to one commit, or to the newest one of its branch when commit is empty. the branch keeps its
	name, so update checks still work, and git refuses when local changes would be overwritten. blocks, so run it off the ui thread"""
	result : Dict[str, Any] = { 'error': '', 'commit': '', 'previous': '', 'requirements_changed': False }

	def git(args : List[str], timeout : int = 300) -> subprocess.CompletedProcess:
		return subprocess.run([ 'git', '-C', root ] + args, capture_output = True, text = True, timeout = timeout, creationflags = CREATE_NO_WINDOW)

	def read_requirements() -> str:
		try:
			with open(os.path.join(root, 'requirements.txt'), encoding = 'utf-8') as file:
				return file.read()
		except OSError:
			return ''

	try:
		result['previous'] = git([ 'rev-parse', 'HEAD' ]).stdout.strip()
		old_requirements = read_requirements()
		# the installer makes a shallow clone, so the commit is fetched first. github serves any commit of the repository
		fetched = git([ 'fetch', '--depth', '1', 'origin', commit or branch ])
		if fetched.returncode != 0:
			result['error'] = (fetched.stderr.strip().splitlines() or [ 'git fetch failed' ])[-1]
			return result
		target = commit or git([ 'rev-parse', 'FETCH_HEAD' ]).stdout.strip()
		switched = git([ 'checkout', '-B', branch, target ] if branch and branch != 'HEAD' else [ 'checkout', '--detach', target ])
		if switched.returncode != 0:
			result['error'] = (switched.stderr.strip().splitlines() or [ 'git checkout failed' ])[-1]
			return result
		result['commit'] = git([ 'rev-parse', 'HEAD' ]).stdout.strip()
		result['requirements_changed'] = read_requirements() != old_requirements
	except (OSError, subprocess.SubprocessError) as exception:
		result['error'] = str(exception)
	return result


def compare_github_commits(repository : str, base : str, head : str) -> Dict[str, Any]:
	"""where head stands against base: identical, ahead (newer), behind (older) or diverged, with how many commits apart"""
	if base == head:
		return { 'status': 'identical', 'ahead': 0, 'behind': 0 }
	compare = fetch_github_json('repos/%s/compare/%s...%s' % (repository, base, head))
	return { 'status': compare.get('status', ''), 'ahead': compare.get('ahead_by', 0), 'behind': compare.get('behind_by', 0) }


def fetch_github_build(repository : str, branch : str, commit : str) -> Dict[str, Any]:
	"""newest release and branch head on github, and how far the local commit is behind it. blocks, so run it off the ui thread"""
	result : Dict[str, Any] = { 'release': '', 'branch': branch, 'commit': '', 'date': '', 'status': '', 'behind': 0 }
	try:
		result['release'] = fetch_github_json('repos/%s/releases/latest' % repository).get('tag_name', '')
	except urllib.error.HTTPError as exception:
		# a repository without releases answers 404, that is not a failure
		if exception.code != 404:
			raise
	if branch and branch != 'HEAD':
		head = fetch_github_json('repos/%s/branches/%s' % (repository, urllib.parse.quote(branch)))['commit']
		result['commit'] = head.get('sha', '')
		result['date'] = head.get('commit', {}).get('committer', {}).get('date', '')
		if commit and result['commit'] and commit != result['commit']:
			compare = fetch_github_json('repos/%s/compare/%s...%s' % (repository, commit, result['commit']))
			# ahead means github has commits the local checkout lacks
			result['status'] = compare.get('status', '')
			result['behind'] = compare.get('ahead_by', 0)
		elif commit:
			result['status'] = 'identical'
	return result


def fetch_latest_release(repository : str) -> Dict[str, str]:
	"""tag, web page, date and source zip of the newest github release, empty when there is none. blocks, so run it off the ui thread"""
	try:
		release = fetch_github_json('repos/%s/releases/latest' % repository)
	except urllib.error.HTTPError as exception:
		if exception.code != 404:
			raise
		return { 'tag': '', 'url': '', 'date': '', 'zip': '' }
	return { 'tag': release.get('tag_name', ''), 'url': release.get('html_url', ''), 'date': release.get('published_at', '') or '', 'zip': release.get('zipball_url', '') or '' }


def is_newer_version(tag : str, current : str) -> bool:
	"""compares every number of two versions like v0.10.1 and 0.2, a tag without numbers is never newer"""
	def numbers(version : str) -> tuple:
		parts = []
		for part in version.strip().lstrip('vV').split('.'):
			digits = ''.join(character for character in part if character.isdigit())
			if not digits:
				break
			parts.append(int(digits))
		return tuple(parts)
	newer, older = numbers(tag), numbers(current)
	# pad so v0.3.0 and 0.3 count as the same version
	size = max(len(newer), len(older))
	return bool(newer) and newer + (0,) * (size - len(newer)) > older + (0,) * (size - len(older))


def get_env_prefix(python_path : str) -> str:
	python_dir = os.path.dirname(os.path.abspath(python_path))
	if os.path.basename(python_dir).lower() in [ 'scripts', 'bin' ]:
		return os.path.dirname(python_dir)
	return python_dir


def is_conda_env(python_path : str) -> bool:
	return os.path.isdir(os.path.join(get_env_prefix(python_path), 'conda-meta'))


def find_conda() -> Optional[str]:
	# CONDA_EXE is set by conda's shell hook, where conda itself is only a shell function
	conda = shutil.which('conda') or shutil.which('conda.bat') or os.environ.get('CONDA_EXE')
	if conda and os.path.isfile(conda):
		return conda
	for candidate in conda_candidates():
		if os.path.isfile(candidate):
			return candidate
	return None


def find_conda_base() -> Optional[str]:
	conda = find_conda()
	if conda:
		# <base>\condabin\conda.bat or <base>\Library\bin\conda.bat or <base>\Scripts\conda.exe, <base>/condabin/conda or <base>/bin/conda elsewhere
		path = os.path.dirname(os.path.abspath(conda))
		for _ in range(3):
			if os.path.isdir(os.path.join(path, 'envs')) or os.path.isdir(os.path.join(path, 'conda-meta')):
				return path
			path = os.path.dirname(path)
	return None


def find_python_candidates(facefusion_root : str = '') -> List[str]:
	candidates : List[str] = []

	if facefusion_root:
		for pattern in python_patterns():
			candidates.extend(glob.glob(os.path.join(facefusion_root, pattern)))

	conda_base = find_conda_base()
	if conda_base:
		for env_dir in sorted(glob.glob(os.path.join(conda_base, 'envs', '*'))):
			python_path = conda_env_python(env_dir)
			if os.path.isfile(python_path):
				candidates.append(python_path)

	# environments whose name mentions facefusion go first
	candidates.sort(key = lambda path: 'facefusion' not in path.lower())
	return list(dict.fromkeys(candidates))


# python path and modified time to version, starting python takes up to seconds
PYTHON_VERSIONS : Dict[Tuple[str, float], str] = {}


def probe_python_version(python_path : str) -> Optional[str]:
	try:
		key = (os.path.normcase(os.path.abspath(python_path)), os.path.getmtime(python_path))
		if key in PYTHON_VERSIONS:
			return PYTHON_VERSIONS[key]
		output = subprocess.run([ python_path, '-c', 'import sys; print(sys.version.split()[0])' ], capture_output = True, text = True, timeout = 20, creationflags = CREATE_NO_WINDOW)
	except (OSError, subprocess.SubprocessError):
		return None
	version = output.stdout.strip() or None
	if version:
		PYTHON_VERSIONS[key] = version
	return version


def version_tuple(version : Optional[str]) -> tuple:
	try:
		return tuple(int(part) for part in (version or '').split('.')[:2])
	except ValueError:
		return ()


def create_process_environment(python_path : str) -> QProcessEnvironment:
	environment = QProcessEnvironment.systemEnvironment()
	prefix = get_env_prefix(python_path)
	path_parts = [ prefix, os.path.join(prefix, ENV_BIN) ]

	if is_conda_env(python_path):
		environment.insert('CONDA_PREFIX', prefix)
		path_parts += \
		[
			os.path.join(prefix, 'Library', 'mingw-w64', 'bin'),
			os.path.join(prefix, 'Library', 'usr', 'bin'),
			os.path.join(prefix, 'Library', 'bin'),
			os.path.join(prefix, 'Lib'),
			os.path.join(prefix, 'Lib', 'site-packages', 'tensorrt_libs')
		]
	else:
		environment.insert('VIRTUAL_ENV', prefix)

	path_parts = [ path for path in path_parts if os.path.isdir(path) ]
	environment.insert('PATH', os.pathsep.join(path_parts + [ environment.value('PATH') ]))
	if IS_LINUX:
		library_path = facefusion_library_path(prefix, is_conda_env(python_path), environment.value('LD_LIBRARY_PATH'))
		if library_path:
			environment.insert('LD_LIBRARY_PATH', library_path)
		else:
			environment.remove('LD_LIBRARY_PATH')
		# also from facefusion's setup_linux, it keeps glibc from holding on to memory
		environment.insert('MALLOC_ARENA_MAX', '4')
	# skip facefusion's self re-spawn so stop() can terminate a single process tree
	environment.insert('ENVIRONMENT_READY', '1')
	environment.insert('OMP_NUM_THREADS', '1')
	environment.insert('PYTHONUNBUFFERED', '1')
	environment.insert('PYTHONIOENCODING', 'utf-8')
	environment.insert('COLUMNS', '200')
	return environment


def environment_to_dict(environment : QProcessEnvironment) -> Dict[str, str]:
	return { key: environment.value(key) for key in environment.keys() }


def has_nvidia_gpu() -> bool:
	return bool(shutil.which('nvidia-smi'))


def check_dependencies() -> Dict[str, bool]:
	return { name: bool(shutil.which(name)) for name in [ 'git', 'ffmpeg', 'ffprobe', 'curl' ] }


# what the install needs, winget package id and the plain reason shown before anything is installed
# ffprobe comes with the ffmpeg package
PREREQUISITES : Dict[str, Tuple[str, str, str]] =\
{
	'git': ('Git.Git', 'Git', 'downloads FaceFusion and updates it later'),
	'uv': ('astral-sh.uv', 'uv', 'creates the Python 3.13 environment and downloads Python 3.13 itself'),
	'ffmpeg': ('Gyan.FFmpeg', 'FFmpeg', 'reads and writes video and audio (includes ffprobe)'),
	'curl': ('cURL.cURL', 'curl', 'downloads the models FaceFusion needs')
}


def find_missing_prerequisites(need_uv : bool, need_git : bool) -> List[str]:
	"""keys of PREREQUISITES that are not on PATH yet and that this install needs"""
	found = check_dependencies()
	missing = []
	if need_git and not found['git']:
		missing.append('git')
	if need_uv and not shutil.which('uv'):
		missing.append('uv')
	if not found['ffmpeg'] or not found['ffprobe']:
		missing.append('ffmpeg')
	if not found['curl']:
		missing.append('curl')
	return missing


def winget_install_args(key : str) -> List[str]:
	return [ 'install', '--id', PREREQUISITES[key][0], '--exact', '--silent', '--accept-package-agreements', '--accept-source-agreements' ]
