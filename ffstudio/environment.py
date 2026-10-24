import glob
import json
import os
import platform
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QProcessEnvironment

CREATE_NO_WINDOW = 0x08000000


def is_facefusion_root(path : str) -> bool:
	return bool(path) and os.path.isfile(os.path.join(path, 'facefusion.py')) and os.path.isdir(os.path.join(path, 'facefusion', 'processors'))


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
	request = urllib.request.Request('https://api.github.com/' + path, headers = { 'Accept': 'application/vnd.github+json', 'User-Agent': 'FaceFusion-Studio' })
	with urllib.request.urlopen(request, timeout = 15) as response:
		return json.loads(response.read().decode('utf-8'))


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


def read_windows_version() -> str:
	try:
		import winreg
		with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, 'SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion') as key:
			def value(name : str) -> str:
				try:
					return str(winreg.QueryValueEx(key, name)[0])
				except OSError:
					return ''
			product, build, revision = value('ProductName'), value('CurrentBuild'), value('UBR')
			release = value('DisplayVersion') or value('ReleaseId')
	except (ImportError, OSError):
		return platform.platform()
	# windows 11 still calls itself windows 10 in ProductName, the build number tells them apart
	if build.isdigit() and int(build) >= 22000:
		product = product.replace('Windows 10', 'Windows 11')
	return ' '.join(part for part in [ product, release ] if part) + '  ·  version 10.0.%s%s' % (build, '.' + revision if revision else '')


def get_env_prefix(python_path : str) -> str:
	python_dir = os.path.dirname(os.path.abspath(python_path))
	if os.path.basename(python_dir).lower() == 'scripts':
		return os.path.dirname(python_dir)
	return python_dir


def is_conda_env(python_path : str) -> bool:
	return os.path.isdir(os.path.join(get_env_prefix(python_path), 'conda-meta'))


def find_conda() -> Optional[str]:
	conda = shutil.which('conda') or shutil.which('conda.bat')
	if conda:
		return conda
	for base in [ os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs'), os.path.expanduser('~'), 'C:\\ProgramData' ]:
		for name in [ 'Miniconda3', 'miniconda3', 'Anaconda3', 'anaconda3', 'miniforge3', 'Miniforge3' ]:
			candidate = os.path.join(base, name, 'condabin', 'conda.bat')
			if os.path.isfile(candidate):
				return candidate
	return None


def find_conda_base() -> Optional[str]:
	conda = find_conda()
	if conda:
		# <base>\condabin\conda.bat or <base>\Library\bin\conda.bat or <base>\Scripts\conda.exe
		path = os.path.dirname(os.path.abspath(conda))
		for _ in range(3):
			if os.path.isdir(os.path.join(path, 'envs')) or os.path.isdir(os.path.join(path, 'conda-meta')):
				return path
			path = os.path.dirname(path)
	return None


def find_python_candidates(facefusion_root : str = '') -> List[str]:
	candidates : List[str] = []

	if facefusion_root:
		for pattern in [ '.venv\\Scripts\\python.exe', 'venv\\Scripts\\python.exe', '.conda\\python.exe', 'env\\python.exe' ]:
			candidates.extend(glob.glob(os.path.join(facefusion_root, pattern)))

	conda_base = find_conda_base()
	if conda_base:
		for env_dir in sorted(glob.glob(os.path.join(conda_base, 'envs', '*'))):
			python_path = os.path.join(env_dir, 'python.exe')
			if os.path.isfile(python_path):
				candidates.append(python_path)

	# environments whose name mentions facefusion go first
	candidates.sort(key = lambda path: 'facefusion' not in path.lower())
	return list(dict.fromkeys(candidates))


def probe_python_version(python_path : str) -> Optional[str]:
	try:
		output = subprocess.run([ python_path, '-c', 'import sys; print(sys.version.split()[0])' ], capture_output = True, text = True, timeout = 20, creationflags = CREATE_NO_WINDOW)
		return output.stdout.strip() or None
	except (OSError, subprocess.SubprocessError):
		return None


def version_tuple(version : Optional[str]) -> tuple:
	try:
		return tuple(int(part) for part in (version or '').split('.')[:2])
	except ValueError:
		return ()


def create_process_environment(python_path : str) -> QProcessEnvironment:
	environment = QProcessEnvironment.systemEnvironment()
	prefix = get_env_prefix(python_path)
	path_parts = [ prefix, os.path.join(prefix, 'Scripts') ]

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
	# skip facefusion's self re-spawn so stop() can terminate a single process tree
	environment.insert('ENVIRONMENT_READY', '1')
	environment.insert('OMP_NUM_THREADS', '1')
	environment.insert('PYTHONUNBUFFERED', '1')
	environment.insert('PYTHONIOENCODING', 'utf-8')
	environment.insert('COLUMNS', '200')
	return environment


def environment_to_dict(environment : QProcessEnvironment) -> Dict[str, str]:
	return { key: environment.value(key) for key in environment.keys() }


def kill_process_tree(pid : int) -> None:
	if pid:
		subprocess.run([ 'taskkill', '/PID', str(pid), '/T', '/F' ], capture_output = True, creationflags = CREATE_NO_WINDOW)


def has_nvidia_gpu() -> bool:
	return bool(shutil.which('nvidia-smi'))


def check_dependencies() -> Dict[str, bool]:
	return { name: bool(shutil.which(name)) for name in [ 'git', 'ffmpeg', 'ffprobe', 'curl' ] }
