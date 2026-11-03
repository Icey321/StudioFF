import os
import subprocess
import sys
import time
from typing import List

from ffstudio import APP_NAME
from ffstudio.config import APP_DIR

# windows groups the taskbar button, its pin and its right-click menu by this id
APP_ID = 'FaceFusionStudio'
ROOT_DIR = os.path.dirname(APP_DIR)
ICON_PATH = os.path.join(APP_DIR, 'assets', 'icon.ico')


def start_command(*extra : str) -> List[str]:
	return [ sys.executable, '-m', 'ffstudio', *extra ]


def relaunch_command() -> str:
	# the taskbar starts it from any folder, so -m ffstudio cannot find the package, this puts the app folder on the path first
	code = 'import sys; sys.path.insert(0, %r); from ffstudio.__main__ import main; main()' % ROOT_DIR
	return subprocess.list2cmdline([ sys.executable, '-c', code ])


def set_window_relaunch(window) -> None:
	"""names the taskbar's right-click entry StudioFF with its icon, without it windows shows python's, which starts a bare python"""
	if sys.platform != 'win32':
		return
	try:
		import pythoncom
		from win32com.propsys import propsys, pscon
		store = propsys.SHGetPropertyStoreForWindow(int(window.winId()), propsys.IID_IPropertyStore)
		for key, value in [
			(pscon.PKEY_AppUserModel_ID, APP_ID),
			(pscon.PKEY_AppUserModel_RelaunchCommand, relaunch_command()),
			(pscon.PKEY_AppUserModel_RelaunchDisplayNameResource, APP_NAME),
			(pscon.PKEY_AppUserModel_RelaunchIconResource, ICON_PATH + ',0')
		]:
			store.SetValue(key, propsys.PROPVARIANTType(value, pythoncom.VT_LPWSTR))
		store.Commit()
	except Exception:
		pass


def begin_destination_list():
	"""the taskbar menu's list for this app, begun. pywin32's BeginList crashes the app, so these two calls go to windows directly"""
	import ctypes
	import uuid
	from ctypes import wintypes

	import pythoncom
	from win32com.shell import shell

	def guid(text):
		return (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(str(text)).bytes_le)

	pointer = ctypes.c_void_p()
	ctypes.oledll.ole32.CoCreateInstance(ctypes.byref(guid(shell.CLSID_DestinationList)), None, pythoncom.CLSCTX_INPROC_SERVER, ctypes.byref(guid(shell.IID_ICustomDestinationList)), ctypes.byref(pointer))
	methods = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
	release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
	try:
		ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, wintypes.LPCWSTR)(methods[3])(pointer, APP_ID)
		slots = ctypes.c_uint()
		removed = ctypes.c_void_p()
		ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(methods[4])(pointer, ctypes.byref(slots), ctypes.byref(guid(shell.IID_IObjectArray)), ctypes.byref(removed))
		if removed.value:
			removed_methods = ctypes.cast(removed, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
			release(removed_methods[2])(removed)
		# pywin32 keeps its own reference, for the rest of the calls
		return pythoncom.ObjectFromAddress(pointer.value, shell.IID_ICustomDestinationList)
	finally:
		release(methods[2])(pointer)


def set_jump_list() -> None:
	"""adds Restart StudioFF to the taskbar's right-click menu, it asks the open app to restart"""
	if sys.platform != 'win32':
		return
	try:
		import pythoncom
		from win32com.propsys import propsys, pscon
		from win32com.shell import shell
		destinations = begin_destination_list()
		tasks = pythoncom.CoCreateInstance(shell.CLSID_EnumerableObjectCollection, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IObjectCollection)
		link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
		command = start_command('--restart')
		link.SetPath(command[0])
		link.SetArguments(subprocess.list2cmdline(command[1:]))
		link.SetWorkingDirectory(ROOT_DIR)
		link.SetIconLocation(ICON_PATH, 0)
		link.SetDescription('Close ' + APP_NAME + ' and open it again')
		properties = link.QueryInterface(propsys.IID_IPropertyStore)
		properties.SetValue(pscon.PKEY_Title, propsys.PROPVARIANTType('Restart ' + APP_NAME, pythoncom.VT_LPWSTR))
		properties.Commit()
		tasks.AddObject(link)
		destinations.AddUserTasks(tasks)
		destinations.CommitList()
	except Exception:
		pass


def start_again() -> None:
	"""starts a new StudioFF that waits for this one to end before it opens"""
	kwargs = {}
	if sys.platform == 'win32':
		kwargs['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
	else:
		kwargs['start_new_session'] = True
	subprocess.Popen(start_command('--after', str(os.getpid())), cwd = ROOT_DIR, close_fds = True, **kwargs)


def wait_for_exit(pid : int, timeout : float = 30) -> None:
	if sys.platform == 'win32':
		import ctypes
		# SYNCHRONIZE, enough to wait on it
		handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
		if handle:
			ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout * 1000))
			ctypes.windll.kernel32.CloseHandle(handle)
		return
	end = time.monotonic() + timeout
	while time.monotonic() < end:
		try:
			os.kill(pid, 0)
		except OSError:
			return
		time.sleep(0.1)
