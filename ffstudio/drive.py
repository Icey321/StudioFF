import os
import shutil
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

GIGABYTE = 1024 * 1024 * 1024
# less free than this shows in orange, a long video may not fit
WARN_FREE_SHARE = 0.1
WARN_FREE_BYTES = 20 * GIGABYTE
# asking a network drive often is slow, readings this young are used again
CACHE_SECONDS = 5.0
CACHE : Dict[str, Tuple[float, Optional[Tuple[int, int]]]] = {}
# drives being asked right now, an asleep or offline network drive can take many seconds to answer
READING : Set[str] = set()
LOCK = threading.Lock()


def drive_name(folder : str) -> str:
	"""C: for a local folder, \\\\server\\share for a network one, the mount point where there are no drive letters"""
	folder = os.path.abspath(folder)
	drive = os.path.splitdrive(folder)[0]
	if drive:
		return drive.rstrip('\\/')
	while not os.path.ismount(folder) and os.path.dirname(folder) != folder:
		folder = os.path.dirname(folder)
	return folder


def existing_folder(folder : str) -> str:
	"""the folder, or the nearest one above it that exists, an output folder is made only by the first run"""
	folder = os.path.abspath(folder)
	while not os.path.isdir(folder):
		parent = os.path.dirname(folder)
		if parent == folder:
			return ''
		folder = parent
	return folder


def read_usage(folder : str) -> Optional[Tuple[int, int]]:
	"""asks the drive, which can block for a long time on a network drive that is not answering"""
	found = existing_folder(folder)
	if found:
		try:
			total, _, free = shutil.disk_usage(found)
			return (free, total)
		except OSError:
			return None
	return None


def read_in_background(key : str, folder : str) -> None:
	usage = read_usage(folder)
	with LOCK:
		CACHE[key] = (time.monotonic(), usage)
		READING.discard(key)


def drive_usage(folder : str) -> Optional[Tuple[int, int]]:
	"""free and total bytes of the drive the folder is on, None when it cannot be read (yet).
	never waits for the drive: an old reading is returned while a new one is read in the background, see is_reading"""
	if not folder:
		return None
	key = os.path.normcase(drive_name(folder))
	cached = CACHE.get(key)
	if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
		return cached[1]
	with LOCK:
		if key not in READING:
			READING.add(key)
			threading.Thread(target = read_in_background, args = (key, folder), daemon = True).start()
	return cached[1] if cached else None


def is_reading(folder : str) -> bool:
	"""true while the first reading of the folder's drive is not there yet, the caller asks again soon"""
	if not folder:
		return False
	key = os.path.normcase(drive_name(folder))
	return key in READING and key not in CACHE


def format_drive_size(size : int) -> str:
	if size >= 1000 * GIGABYTE:
		return '%.1f TB' % (size / (1024 * GIGABYTE))
	return '%.0f GB' % (size / GIGABYTE) if size >= 10 * GIGABYTE else '%.1f GB' % (size / GIGABYTE)


def describe_drive(folder : str) -> Optional[Dict[str, Any]]:
	"""the drive of a folder: its name, free and total space, and whether it is getting full"""
	usage = drive_usage(folder)
	if not usage or not usage[1]:
		return None
	free, total = usage
	return {
		'drive': drive_name(folder),
		'free': free,
		'total': total,
		'share': min(max(1 - free / total, 0.0), 1.0),
		'warn': free < total * WARN_FREE_SHARE or free < WARN_FREE_BYTES
	}


def drive_text(folder : str) -> str:
	"""such as Drive D: 412 GB free of 931 GB"""
	drive = describe_drive(folder)
	if not drive:
		return ''
	return 'Drive %s  %s free of %s' % (drive.get('drive'), format_drive_size(drive.get('free')), format_drive_size(drive.get('total')))


def output_drives(folders : List[str]) -> List[Dict[str, Any]]:
	"""one reading for each drive the folders are on, in the order of the folders"""
	drives = []
	seen = set()
	for folder in folders:
		if not folder:
			continue
		key = os.path.normcase(drive_name(folder))
		if key in seen:
			continue
		seen.add(key)
		drive = describe_drive(folder)
		if drive:
			drives.append(drive)
	return drives
