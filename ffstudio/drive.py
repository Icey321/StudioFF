import os
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

GIGABYTE = 1024 * 1024 * 1024
# less free than this shows in orange, a long video may not fit
WARN_FREE_SHARE = 0.1
WARN_FREE_BYTES = 20 * GIGABYTE
# asking a network drive often is slow, readings this young are used again
CACHE_SECONDS = 5.0
CACHE : Dict[str, Tuple[float, Optional[Tuple[int, int]]]] = {}


def drive_name(folder : str) -> str:
	"""C: for a local folder, \\\\server\\share for a network one"""
	drive = os.path.splitdrive(os.path.abspath(folder))[0]
	return drive.rstrip('\\/') or folder


def existing_folder(folder : str) -> str:
	"""the folder, or the nearest one above it that exists, an output folder is made only by the first run"""
	folder = os.path.abspath(folder)
	while not os.path.isdir(folder):
		parent = os.path.dirname(folder)
		if parent == folder:
			return ''
		folder = parent
	return folder


def drive_usage(folder : str) -> Optional[Tuple[int, int]]:
	"""free and total bytes of the drive the folder is on, None when it cannot be read"""
	if not folder:
		return None
	key = os.path.normcase(drive_name(folder))
	now = time.monotonic()
	cached = CACHE.get(key)
	if cached and now - cached[0] < CACHE_SECONDS:
		return cached[1]
	usage = None
	found = existing_folder(folder)
	if found:
		try:
			total, _, free = shutil.disk_usage(found)
			usage = (free, total)
		except OSError:
			usage = None
	CACHE[key] = (now, usage)
	return usage


def format_size(size : int) -> str:
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
	return 'Drive %s  %s free of %s' % (drive.get('drive'), format_size(drive.get('free')), format_size(drive.get('total')))


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
