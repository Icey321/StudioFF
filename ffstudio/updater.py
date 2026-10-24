import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from typing import Callable, Optional, Tuple

from ffstudio.config import DATA_DIR

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATE_DIR = os.path.join(tempfile.gettempdir(), 'FaceFusionStudio-update')
RESULT_FILE = os.path.join(DATA_DIR, 'update-result.txt')
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# runs after the app has quit: swaps in the new files, keeps a backup of ffstudio until the copy worked, refreshes the
# packages when requirements.txt changed and opens the app again. it writes one line to the result file for the next start
HELPER_SCRIPT = r'''
param([int]$AppId, [string]$Source, [string]$Target, [string]$Tag, [string]$Result)
$ErrorActionPreference = 'Stop'
Start-Transcript -Path (Join-Path (Split-Path $Result) 'update-log.txt') -Force | Out-Null
try { Wait-Process -Id $AppId -Timeout 120 -ErrorAction SilentlyContinue } catch {}
Start-Sleep -Milliseconds 500
$backup = Join-Path $Target 'ffstudio.update-backup'
$python = Join-Path $Target '.venv\Scripts\python.exe'
try {
	$oldRequirements = Get-Content (Join-Path $Target 'requirements.txt') -Raw -ErrorAction SilentlyContinue
	if (Test-Path $backup) { Remove-Item $backup -Recurse -Force }
	Move-Item (Join-Path $Target 'ffstudio') $backup
	robocopy $Source $Target /E /XD .venv .git /R:3 /W:1 /NP /NFL /NDL /NJH /NJS | Out-Null
	if ($LASTEXITCODE -ge 8) { throw "copying the new files failed (robocopy $LASTEXITCODE)" }
	if (-not (Test-Path (Join-Path $Target 'ffstudio\__init__.py'))) { throw 'the new files are incomplete' }
	$newRequirements = Get-Content (Join-Path $Target 'requirements.txt') -Raw -ErrorAction SilentlyContinue
	if ($newRequirements -ne $oldRequirements) {
		if (Get-Command uv -ErrorAction SilentlyContinue) {
			uv pip install --python $python -r (Join-Path $Target 'requirements.txt') | Out-Null
		} else {
			& $python -m pip install -r (Join-Path $Target 'requirements.txt') | Out-Null
		}
		if ($LASTEXITCODE -ne 0) { throw 'installing the new packages failed' }
	}
	Remove-Item $backup -Recurse -Force -ErrorAction SilentlyContinue
	Set-Content $Result "ok $Tag" -Encoding utf8
} catch {
	if (Test-Path $backup) {
		Remove-Item (Join-Path $Target 'ffstudio') -Recurse -Force -ErrorAction SilentlyContinue
		Move-Item $backup (Join-Path $Target 'ffstudio')
	}
	# put the old requirements back too, else the next update sees them as equal and skips the install
	if ($null -ne $oldRequirements) { Set-Content (Join-Path $Target 'requirements.txt') $oldRequirements -NoNewline -Encoding utf8 }
	Set-Content $Result ("failed " + $_.Exception.Message) -Encoding utf8
}
Remove-Item (Split-Path $Source) -Recurse -Force -ErrorAction SilentlyContinue
Start-Process (Join-Path $Target '.venv\Scripts\pythonw.exe') -ArgumentList '-m', 'ffstudio' -WorkingDirectory $Target
Stop-Transcript | Out-Null
'''


def is_git_checkout() -> bool:
	return os.path.isdir(os.path.join(APP_ROOT, '.git'))


def download_release(zip_url : str, progress : Callable[[int, int], None]) -> str:
	"""downloads and unzips a release, returns the folder holding its files. progress gets bytes done and total, total is 0 when github does not say. blocks, so run it off the ui thread"""
	shutil.rmtree(UPDATE_DIR, ignore_errors = True)
	os.makedirs(UPDATE_DIR)
	zip_path = os.path.join(UPDATE_DIR, 'release.zip')
	request = urllib.request.Request(zip_url, headers = { 'User-Agent': 'FaceFusion-Studio' })
	with urllib.request.urlopen(request, timeout = 30) as response, open(zip_path, 'wb') as zip_file:
		total = int(response.headers.get('Content-Length') or 0)
		done = 0
		while True:
			chunk = response.read(256 * 1024)
			if not chunk:
				break
			zip_file.write(chunk)
			done += len(chunk)
			progress(done, total)
	# github has no checksum for its source zips, so this checks that the whole file came and every file in it is intact
	if total and done != total:
		raise RuntimeError('the download stopped early (%d of %d bytes)' % (done, total))
	extract_dir = os.path.join(UPDATE_DIR, 'files')
	try:
		with zipfile.ZipFile(zip_path) as archive:
			broken = archive.testzip()
			if broken:
				raise RuntimeError('the download is damaged (%s)' % broken)
			archive.extractall(extract_dir)
	except zipfile.BadZipFile:
		raise RuntimeError('the download is not a valid zip file')
	os.remove(zip_path)
	# github wraps the source in one folder named after the repository and commit
	folders = os.listdir(extract_dir)
	source = os.path.join(extract_dir, folders[0]) if len(folders) == 1 else extract_dir
	if not os.path.isfile(os.path.join(source, 'ffstudio', '__init__.py')):
		raise RuntimeError('the download does not contain FaceFusion Studio')
	return source


def start_helper(source : str, tag : str) -> None:
	"""starts the helper that waits for this app to quit, then installs the downloaded files and reopens the app"""
	script_path = os.path.join(UPDATE_DIR, 'update.ps1')
	with open(script_path, 'w', encoding = 'utf-8-sig') as script_file:
		script_file.write(HELPER_SCRIPT)
	os.makedirs(DATA_DIR, exist_ok = True)
	command = [ 'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', script_path, '-AppId', str(os.getpid()), '-Source', source, '-Target', APP_ROOT, '-Tag', tag, '-Result', RESULT_FILE ]
	if sys.platform != 'win32':
		subprocess.Popen(command, close_fds = True)
		return
	# a hidden console, powershell started with none at all (DETACHED_PROCESS) quits without running the script
	flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
	# the helper has to outlive this app, also when the app was started inside a job that closes with it
	try:
		subprocess.Popen(command, creationflags = flags | CREATE_BREAKAWAY_FROM_JOB, close_fds = True)
	except OSError:
		subprocess.Popen(command, creationflags = flags, close_fds = True)


def read_update_result() -> Optional[Tuple[bool, str]]:
	"""what the last update did, once: ok with the tag, or failed with the reason. none when no update ran"""
	try:
		with open(RESULT_FILE, encoding = 'utf-8-sig') as result_file:
			line = result_file.read().strip()
		os.remove(RESULT_FILE)
	except OSError:
		return None
	state, _, text = line.partition(' ')
	return state == 'ok', text
