"""
Runs inside the FaceFusion Python environment (NOT the UI environment).

Reports how busy the graphics card, the processor and the memory are, with the same libraries
FaceFusion's own metrics use (psutil and its NVIDIA / AMD device bindings). It loads no models,
so it is cheap to keep running.

usage: python system_monitor.py [interval_seconds]
	prints one line per interval: METRICS {json}
		{"gpus": [{"name", "used", "total", "load", "temperature"}], "cpu": <percent>, "ram_used": <bytes>, "ram_total": <bytes>}
	gpu memory is in bytes, load in percent, temperature in degrees celsius. it quits when the app stops reading
"""
import json
import os
import sys
import time

RESULT_PREFIX = 'METRICS '
GIGABYTE = 1024 * 1024 * 1024


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


def main() -> None:
	# started with facefusion's folder as the working directory
	sys.path.insert(0, os.getcwd())
	import psutil

	interval = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
	nvidia_ml, handles = open_nvidia()
	use_amd = not handles and sys.platform == 'win32' and bool(read_amd())
	# the first cpu reading is always 0, it measures from the previous call
	psutil.cpu_percent()
	try:
		while True:
			time.sleep(interval)
			memory = psutil.virtual_memory()
			gpus = read_nvidia(nvidia_ml, handles) if handles else read_amd() if use_amd else []
			result = { 'gpus': gpus, 'cpu': psutil.cpu_percent(), 'ram_used': int(memory.total - memory.available), 'ram_total': int(memory.total) }
			sys.stdout.write(RESULT_PREFIX + json.dumps(result) + '\n')
			sys.stdout.flush()
	except (BrokenPipeError, OSError, KeyboardInterrupt):
		pass
	finally:
		if handles:
			nvidia_ml.destroy()


if __name__ == '__main__':
	main()
