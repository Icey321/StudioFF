"""everything that differs between windows, linux and macos. the rest of the app calls these and never checks the system itself"""
import os
import platform
import signal
import subprocess
import sys
from typing import Dict, List, Tuple

IS_WINDOWS = sys.platform == 'win32'
IS_MAC = sys.platform == 'darwin'
IS_LINUX = not IS_WINDOWS and not IS_MAC
# subprocess takes creationflags only on windows, 0 is the one value it accepts elsewhere
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
# a python environment keeps its programs in Scripts on windows and in bin elsewhere
ENV_BIN = 'Scripts' if IS_WINDOWS else 'bin'
PYTHON_NAME = 'python.exe' if IS_WINDOWS else 'python'
# linux and macos have no segoe ui, qt picks the desktop's own font for sans-serif
UI_FONT = 'Segoe UI' if IS_WINDOWS else 'sans-serif'
# the gpu choice of the Install card besides cuda: directml only exists on windows, migraphx is amd's on linux
GPU_ONNXRUNTIME = ('directml', 'DirectML — any DirectX 12 GPU') if IS_WINDOWS else ('migraphx', 'MIGraphX — AMD GPU (ROCm)') if not IS_MAC else None


def data_root() -> str:
	"""the folder that holds the StudioFF settings folder"""
	if IS_WINDOWS:
		return os.environ.get('APPDATA') or os.path.expanduser('~')
	if IS_MAC:
		return os.path.join(os.path.expanduser('~'), 'Library', 'Application Support')
	return os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')


def venv_python(env_dir : str) -> str:
	return os.path.join(env_dir, ENV_BIN, PYTHON_NAME)


def conda_env_python(env_dir : str) -> str:
	# conda puts python.exe right in the environment folder on windows
	return os.path.join(env_dir, PYTHON_NAME) if IS_WINDOWS else os.path.join(env_dir, 'bin', PYTHON_NAME)


def python_patterns() -> List[str]:
	"""where a python environment inside the FaceFusion folder keeps its python"""
	if IS_WINDOWS:
		return [ '.venv\\Scripts\\python.exe', 'venv\\Scripts\\python.exe', '.conda\\python.exe', 'env\\python.exe' ]
	return [ '.venv/bin/python', 'venv/bin/python', '.conda/bin/python', 'env/bin/python' ]


def conda_candidates() -> List[str]:
	"""usual install places of conda, for when it is not on PATH"""
	names = [ 'Miniconda3', 'miniconda3', 'Anaconda3', 'anaconda3', 'miniforge3', 'Miniforge3' ]
	if IS_WINDOWS:
		bases = [ os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs'), os.path.expanduser('~'), 'C:\\ProgramData' ]
		return [ os.path.join(base, name, 'condabin', 'conda.bat') for base in bases for name in names ]
	bases = [ os.path.expanduser('~'), '/opt' ]
	return [ os.path.join(base, name, 'condabin', 'conda') for base in bases for name in names ]


def script_command(program : str, args : List[str]) -> Tuple[str, List[str]]:
	"""program and arguments to start a program that may be a .bat file, which windows only runs through cmd.exe"""
	if IS_WINDOWS:
		return 'cmd.exe', [ '/c', program ] + args
	return program, args


def show_in_folder(path : str) -> None:
	"""opens the folder holding path, with path picked where the file manager can do that"""
	if IS_WINDOWS:
		subprocess.Popen([ 'explorer', '/select,', path ])
	elif IS_MAC:
		subprocess.Popen([ 'open', '-R', path ])
	else:
		from PySide6.QtCore import QUrl
		from PySide6.QtDBus import QDBusConnection, QDBusMessage

		# most linux file managers (dolphin, nautilus, nemo, thunar) pick a file through the FileManager1 interface. from tokenwizard's linux fork
		message = QDBusMessage.createMethodCall('org.freedesktop.FileManager1', '/org/freedesktop/FileManager1', 'org.freedesktop.FileManager1', 'ShowItems')
		message.setArguments([ [ QUrl.fromLocalFile(path).toEncoded().data().decode() ], '' ])
		reply = QDBusConnection.sessionBus().call(message, timeout = 2000)
		if reply.type() == QDBusMessage.MessageType.ErrorMessage:
			subprocess.Popen([ 'xdg-open', os.path.dirname(path) or '.' ])


def package_install_hint(keys : List[str]) -> str:
	"""linux and macos: the command that installs these programs (git, uv, ffmpeg, curl) with the package manager found here"""
	packages = [ key for key in keys if key != 'uv' ]
	commands = []
	if packages:
		import shutil
		for manager, command in [ ('brew', 'brew install'), ('apt', 'sudo apt install'), ('dnf', 'sudo dnf install'), ('pacman', 'sudo pacman -S'), ('zypper', 'sudo zypper install') ]:
			if shutil.which(manager):
				commands.append('%s %s' % (command, ' '.join(packages)))
				break
		else:
			commands.append('your package manager')
	if 'uv' in keys:
		# uv is in few distributions' packages, its own installer works everywhere
		commands.append('curl -LsSf https://astral.sh/uv/install.sh | sh')
	return ' and '.join(commands)


def facefusion_library_path(prefix : str, is_conda : bool, current : str) -> str:
	"""linux only: the LD_LIBRARY_PATH for FaceFusion. StudioFF sets ENVIRONMENT_READY, which skips FaceFusion's own setup_linux, so this adds
	what that step would: the conda lib folder and tensorrt_libs, where cuda and cudnn are. from tokenwizard's linux fork"""
	# this app's opencv adds its own folder and an empty entry (the working directory) when imported, FaceFusion gets neither
	paths = [ path for path in current.split(os.pathsep) if path and not os.path.abspath(path).startswith(sys.prefix + os.sep) ]
	if is_conda:
		import glob
		paths = [ path for path in [ os.path.join(prefix, 'lib') ] + glob.glob(os.path.join(prefix, 'lib', 'python3*', 'site-packages', 'tensorrt_libs')) if os.path.isdir(path) ] + paths
	return os.pathsep.join(paths)


def read_os_version() -> str:
	if IS_WINDOWS:
		return read_windows_version()
	if IS_MAC:
		return 'macOS %s  ·  %s' % (platform.mac_ver()[0], platform.machine())
	name = ''
	try:
		with open('/etc/os-release', encoding = 'utf-8') as release_file:
			for line in release_file:
				if line.startswith('PRETTY_NAME='):
					name = line.split('=', 1)[1].strip().strip('"')
	except OSError:
		pass
	return '%s  ·  Linux %s' % (name or 'Linux', platform.release())


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


def read_cpu_name() -> str:
	try:
		if IS_WINDOWS:
			import winreg
			with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
				return str(winreg.QueryValueEx(key, 'ProcessorNameString')[0]).strip()
		if IS_MAC:
			return subprocess.run([ 'sysctl', '-n', 'machdep.cpu.brand_string' ], capture_output = True, text = True, timeout = 5).stdout.strip()
		with open('/proc/cpuinfo', encoding = 'utf-8') as cpu_file:
			for line in cpu_file:
				if line.startswith('model name'):
					return line.split(':', 1)[1].strip()
	except (OSError, subprocess.SubprocessError):
		pass
	return platform.processor()


def read_cpu_speed() -> float:
	"""the processor's rated speed in GHz, 0 when unknown. not the boost speed, which no simple source reports"""
	try:
		if IS_WINDOWS:
			import winreg
			with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
				return int(winreg.QueryValueEx(key, '~MHz')[0]) / 1000
		if IS_MAC:
			return int(subprocess.run([ 'sysctl', '-n', 'hw.cpufrequency' ], capture_output = True, text = True, timeout = 5).stdout.strip()) / 1e9
		# linux: the highest speed the kernel allows, in kHz
		with open('/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq', encoding = 'utf-8') as speed_file:
			return int(speed_file.read().strip()) / 1e6
	except (OSError, ValueError, subprocess.SubprocessError):
		return 0


def read_memory_total() -> int:
	"""bytes of ram, 0 when unknown"""
	try:
		if IS_WINDOWS:
			import ctypes

			class MemoryStatus(ctypes.Structure):
				_fields_ = [ ('length', ctypes.c_ulong), ('memory_load', ctypes.c_ulong), ('total_physical', ctypes.c_ulonglong), ('available_physical', ctypes.c_ulonglong), ('total_page_file', ctypes.c_ulonglong), ('available_page_file', ctypes.c_ulonglong), ('total_virtual', ctypes.c_ulonglong), ('available_virtual', ctypes.c_ulonglong), ('available_extended_virtual', ctypes.c_ulonglong) ]

			status = MemoryStatus()
			status.length = ctypes.sizeof(MemoryStatus)
			ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
			return int(status.total_physical)
		return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
	except (AttributeError, OSError, ValueError):
		return 0


def process_tree(pid : int) -> List[int]:
	"""the process and every process under it"""
	children : Dict[int, List[int]] = {}
	for process_id, parent_id in list_processes():
		children.setdefault(parent_id, []).append(process_id)
	tree = [ pid ]
	for parent in tree:
		tree += [ child for child in children.get(parent, []) if child not in tree ]
	return tree


def list_processes() -> List[Tuple[int, int]]:
	"""process id and parent id of every process"""
	if not IS_WINDOWS:
		try:
			output = subprocess.run([ 'ps', '-A', '-o', 'pid=', '-o', 'ppid=' ], capture_output = True, text = True, timeout = 10).stdout
		except (OSError, subprocess.SubprocessError):
			return []
		return [ (int(parts[0]), int(parts[1])) for parts in (line.split() for line in output.splitlines()) if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() ]

	import ctypes
	from ctypes import wintypes

	class ProcessEntry(ctypes.Structure):
		_fields_ =\
		[
			('dwSize', wintypes.DWORD),
			('cntUsage', wintypes.DWORD),
			('th32ProcessID', wintypes.DWORD),
			('th32DefaultHeapID', ctypes.c_void_p),
			('th32ModuleID', wintypes.DWORD),
			('cntThreads', wintypes.DWORD),
			('th32ParentProcessID', wintypes.DWORD),
			('pcPriClassBase', ctypes.c_long),
			('dwFlags', wintypes.DWORD),
			('szExeFile', ctypes.c_wchar * 260)
		]

	kernel32 = ctypes.WinDLL('kernel32', use_last_error = True)
	kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
	snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)
	if not snapshot or snapshot == wintypes.HANDLE(-1).value:
		return []
	processes : List[Tuple[int, int]] = []
	entry = ProcessEntry()
	entry.dwSize = ctypes.sizeof(ProcessEntry)
	found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
	while found:
		processes.append((entry.th32ProcessID, entry.th32ParentProcessID))
		found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
	kernel32.CloseHandle(snapshot)
	return processes


def kill_process_tree(pid : int) -> None:
	if not pid:
		return
	if IS_WINDOWS:
		subprocess.run([ 'taskkill', '/PID', str(pid), '/T', '/F' ], capture_output = True, creationflags = CREATE_NO_WINDOW)
		return
	for process_id in process_tree(pid):
		try:
			os.kill(process_id, signal.SIGKILL)
		except OSError:
			pass


def suspend_process_tree(pid : int, suspend : bool) -> bool:
	"""freezes or unfreezes a process and the processes it started, such as ffmpeg, in place: they use no cpu or gpu while frozen and go on where they were"""
	if not pid:
		return False
	done = False
	if not IS_WINDOWS:
		for process_id in process_tree(pid):
			try:
				os.kill(process_id, signal.SIGSTOP if suspend else signal.SIGCONT)
				done = True
			except OSError:
				pass
		return done

	import ctypes
	from ctypes import wintypes

	kernel32 = ctypes.WinDLL('kernel32', use_last_error = True)
	kernel32.OpenProcess.restype = wintypes.HANDLE
	ntdll = ctypes.WinDLL('ntdll')
	call = ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess
	# PROCESS_SUSPEND_RESUME
	for process_id in process_tree(pid):
		handle = kernel32.OpenProcess(0x0800, False, process_id)
		if handle:
			done = call(wintypes.HANDLE(handle)) == 0 or done
			kernel32.CloseHandle(handle)
	return done


def refresh_path() -> None:
	"""takes the PATH Windows holds now, programs installed while the app runs would not be found otherwise. elsewhere PATH only changes with a new login, so nothing to do"""
	if not IS_WINDOWS:
		return
	import winreg

	parts : List[str] = []
	for root, sub_key in [ (winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'), (winreg.HKEY_CURRENT_USER, 'Environment') ]:
		try:
			with winreg.OpenKey(root, sub_key) as key:
				value, _ = winreg.QueryValueEx(key, 'Path')
				parts.extend(os.path.expandvars(part) for part in value.split(os.pathsep) if part)
		except OSError:
			pass
	known = { os.path.normcase(part) for part in parts }
	parts.extend(part for part in os.environ.get('PATH', '').split(os.pathsep) if part and os.path.normcase(part) not in known)
	path = os.pathsep.join(parts)
	os.environ['PATH'] = path
	# QProcess starts programs from the Windows environment block, which os.environ does not always reach
	try:
		import ctypes
		ctypes.windll.kernel32.SetEnvironmentVariableW('PATH', path)
	except (OSError, AttributeError):
		pass
