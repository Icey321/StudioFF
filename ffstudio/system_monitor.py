"""
Runs inside the FaceFusion Python environment (NOT the UI environment).

Reports how busy the graphics card, the processor and the memory are, with the same libraries
FaceFusion's own metrics use (psutil and its NVIDIA / AMD device bindings). Other cards (Intel)
are read from the Windows GPU counters Task Manager uses. It loads no models, so it is cheap to keep running.

usage: python system_monitor.py [interval_seconds]
	prints one line per interval: METRICS {json}
		{"gpus": [{"name", "used", "total", "load", "temperature", "shared"}], "cpu": <percent>, "ram_used": <bytes>, "ram_total": <bytes>}
	gpu memory is in bytes, load in percent, temperature in degrees celsius or null when unknown,
	shared when the card uses system memory (integrated). it quits when the app stops reading
"""
import json
import os
import sys
import time
from typing import Any, Optional

RESULT_PREFIX = 'METRICS '
GIGABYTE = 1024 * 1024 * 1024
# seconds, how soon a program that starts using the graphics card counts in the windows readings
QUERY_REFRESH = 10


def open_nvidia():
	"""the device handles of every nvidia card, none when there is no nvidia driver"""
	try:
		from facefusion.devices import nvidia_ml

		if nvidia_ml.init():
			return nvidia_ml, nvidia_ml.find_device_handles()
	except Exception:
		pass
	return None, []


def read_nvidia(nvidia_ml, handles) -> list:
	gpus = []
	for handle in handles:
		memory = nvidia_ml.get_device_memory(handle)
		gpus.append(
		{
			'name': nvidia_ml.get_device_name(handle).value.decode(errors = 'replace'),
			'used': int(memory.used),
			'total': int(memory.total),
			'load': int(nvidia_ml.get_device_utilization(handle).gpu),
			'temperature': int(nvidia_ml.get_device_temperature(handle).value)
		})
	return gpus


def read_amd() -> list:
	"""amd cards on windows, facefusion only reports their memory in whole gigabytes"""
	try:
		from facefusion.system import detect_amd_adlx

		return [
		{
			'name': device.get('product').get('name'),
			'used': device.get('memory').get('used').get('value') * GIGABYTE,
			'total': device.get('memory').get('total').get('value') * GIGABYTE,
			'load': int(device.get('utilization').get('gpu').get('value')),
			'temperature': int(device.get('temperature').get('gpu').get('value'))
		} for device in detect_amd_adlx() ]
	except Exception:
		return []


class WindowsGpus:
	"""
	any graphics card on windows (intel, or one the vendor libraries miss), from the windows gpu counters
	task manager reads. busy is the busiest engine type (3d, copy, video ...), all programs together;
	memory is dedicated plus shared, and an integrated card has almost only shared memory, taken from the ram.
	no temperature, windows does not count it
	"""

	def __init__(self) -> None:
		import ctypes
		from ctypes import wintypes

		self.ctypes = ctypes
		self.wintypes = wintypes
		self.pdh = ctypes.WinDLL('pdh')
		self.adapters = self.find_adapters()
		self.query, self.counters = self.open_query() if self.adapters else (None, {})
		# the next query, already sampled once, it takes over on the next read
		self.next_query, self.next_counters = None, {}
		self.opened = time.monotonic()

	def open_query(self) -> tuple:
		"""
		a wildcard counter only covers the programs running when it was added, so a query is replaced every
		QUERY_REFRESH seconds to count programs started since (facefusion itself, usually)
		"""
		ctypes, wintypes = self.ctypes, self.wintypes
		query = wintypes.HANDLE()
		counters = {}
		if self.pdh.PdhOpenQueryW(None, None, ctypes.byref(query)):
			return None, {}
		for key, path in [ ('load', '\\GPU Engine(*)\\Utilization Percentage'), ('dedicated', '\\GPU Adapter Memory(*)\\Dedicated Usage'), ('shared', '\\GPU Adapter Memory(*)\\Shared Usage') ]:
			counter = wintypes.HANDLE()
			if not self.pdh.PdhAddEnglishCounterW(query, path, None, ctypes.byref(counter)):
				counters[key] = counter
		# the first utilization reading needs one before it
		self.pdh.PdhCollectQueryData(query)
		return query, counters

	def refresh_query(self) -> None:
		if self.next_query:
			self.pdh.PdhCloseQuery(self.query)
			self.query, self.counters = self.next_query, self.next_counters
			self.next_query, self.next_counters = None, {}
		elif time.monotonic() - self.opened >= QUERY_REFRESH:
			self.next_query, self.next_counters = self.open_query()
			self.opened = time.monotonic()

	def find_adapters(self) -> list:
		"""the real graphics cards from dxgi: name, the luid the counters are named by, memory totals"""
		ctypes, wintypes = self.ctypes, self.wintypes

		class Luid(ctypes.Structure):
			_fields_ = [ ('low', wintypes.DWORD), ('high', wintypes.LONG) ]

		class AdapterDesc(ctypes.Structure):
			_fields_ = [ ('description', ctypes.c_wchar * 128), ('vendor_id', wintypes.UINT), ('device_id', wintypes.UINT), ('subsys_id', wintypes.UINT), ('revision', wintypes.UINT),
				('dedicated_video', ctypes.c_size_t), ('dedicated_system', ctypes.c_size_t), ('shared_system', ctypes.c_size_t), ('luid', Luid), ('flags', wintypes.UINT) ]

		class Guid(ctypes.Structure):
			_fields_ = [ ('data1', wintypes.DWORD), ('data2', wintypes.WORD), ('data3', wintypes.WORD), ('data4', ctypes.c_ubyte * 8) ]

		def method(instance : Any, index : int, *argtypes : Any) -> Any:
			vtable = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
			return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])

		# IDXGIFactory1, EnumAdapters1 is its 12th method, IDXGIAdapter1.GetDesc1 the 10th, Release the 2nd
		factory_id = Guid(0x770aae78, 0xf26f, 0x4dba, (ctypes.c_ubyte * 8)(0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))
		factory = ctypes.c_void_p()
		if ctypes.WinDLL('dxgi').CreateDXGIFactory1(ctypes.byref(factory_id), ctypes.byref(factory)) or not factory:
			return []
		adapters = []
		try:
			for index in range(16):
				adapter = ctypes.c_void_p()
				try:
					method(factory, 12, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(factory, index, ctypes.byref(adapter))
				except OSError:
					break
				desc = AdapterDesc()
				method(adapter, 10, ctypes.POINTER(AdapterDesc))(adapter, ctypes.byref(desc))
				method(adapter, 2)(adapter)
				# 2 is the software renderer
				if not desc.flags & 2:
					adapters.append(
					{
						'name': desc.description,
						'luid': 'luid_0x%08X_0x%08X' % (desc.luid.high & 0xffffffff, desc.luid.low),
						'total': int(desc.dedicated_video + desc.shared_system),
						'shared': desc.dedicated_video < 512 * 1024 * 1024
					})
		finally:
			method(factory, 2)(factory)
		return adapters

	def read_counter(self, key : str, large : bool) -> dict:
		"""every instance of a wildcard counter, by instance name"""
		ctypes, wintypes = self.ctypes, self.wintypes
		counter = self.counters.get(key)
		if not counter:
			return {}

		class Value(ctypes.Structure):
			_fields_ = [ ('status', wintypes.DWORD), ('value', ctypes.c_longlong if large else ctypes.c_double) ]

		class Item(ctypes.Structure):
			_fields_ = [ ('name', wintypes.LPWSTR), ('value', Value) ]

		# PDH_FMT_LARGE, or PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
		value_format = 0x400 if large else 0x8200
		size, count = wintypes.DWORD(0), wintypes.DWORD(0)
		# PDH_MORE_DATA tells the size
		if self.pdh.PdhGetFormattedCounterArrayW(counter, value_format, ctypes.byref(size), ctypes.byref(count), None) & 0xffffffff != 0x800007D2:
			return {}
		buffer = (ctypes.c_byte * size.value)()
		if self.pdh.PdhGetFormattedCounterArrayW(counter, value_format, ctypes.byref(size), ctypes.byref(count), buffer):
			return {}
		items = ctypes.cast(buffer, ctypes.POINTER(Item))
		return { items[index].name: items[index].value.value for index in range(count.value) if items[index].value.status in (0, 1) }

	def read(self) -> list:
		if self.query:
			self.refresh_query()
		if not self.counters or self.pdh.PdhCollectQueryData(self.query):
			return []
		# instances look like pid_1234_luid_0x00000000_0x00010098_phys_0_eng_0_engtype_3D
		engine_loads = {}
		for name, value in self.read_counter('load', False).items():
			luid, _, rest = name.partition('_phys_')
			luid = luid[luid.find('luid_'):]
			engine_type = rest.partition('engtype_')[2]
			engine_loads[(luid, engine_type)] = engine_loads.get((luid, engine_type), 0.0) + value
		dedicated = self.read_counter('dedicated', True)
		shared = self.read_counter('shared', True)
		gpus = []
		for adapter in self.adapters:
			luid = adapter.get('luid')
			load = max([ value for (engine_luid, _), value in engine_loads.items() if engine_luid == luid ] or [ 0.0 ])
			used = sum(value for name, value in dedicated.items() if name.startswith(luid)) + sum(value for name, value in shared.items() if name.startswith(luid))
			gpus.append({ 'name': adapter.get('name'), 'used': int(used), 'total': adapter.get('total'), 'load': int(round(min(load, 100.0))), 'temperature': None, 'shared': adapter.get('shared') })
		return gpus

	def close(self) -> None:
		for query in [ self.query, self.next_query ]:
			if query:
				self.pdh.PdhCloseQuery(query)
		self.query = self.next_query = None


def open_windows() -> Optional[WindowsGpus]:
	try:
		windows_gpus = WindowsGpus()
		if windows_gpus.read():
			return windows_gpus
		windows_gpus.close()
	except Exception:
		pass
	return None


def main() -> None:
	# started with facefusion's folder as the working directory
	sys.path.insert(0, os.getcwd())
	import psutil

	interval = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
	nvidia_ml, handles = open_nvidia()
	use_amd = not handles and sys.platform == 'win32' and bool(read_amd())
	# intel cards, laptops without nvidia or amd
	windows_gpus = open_windows() if not handles and not use_amd and sys.platform == 'win32' else None
	# the first cpu reading is always 0, it measures from the previous call
	psutil.cpu_percent()
	try:
		while True:
			time.sleep(interval)
			memory = psutil.virtual_memory()
			gpus = read_nvidia(nvidia_ml, handles) if handles else read_amd() if use_amd else windows_gpus.read() if windows_gpus else []
			result = { 'gpus': gpus, 'cpu': psutil.cpu_percent(), 'ram_used': int(memory.total - memory.available), 'ram_total': int(memory.total) }
			sys.stdout.write(RESULT_PREFIX + json.dumps(result) + '\n')
			sys.stdout.flush()
	except (BrokenPipeError, OSError, KeyboardInterrupt):
		pass
	finally:
		if handles:
			nvidia_ml.destroy()
		if windows_gpus:
			windows_gpus.close()


if __name__ == '__main__':
	main()
