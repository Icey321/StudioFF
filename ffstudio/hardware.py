import os
import re
import shutil
import subprocess
from typing import Dict, List

from PySide6.QtCore import QThread, Signal

from ffstudio.environment import CREATE_NO_WINDOW, create_process_environment
from ffstudio.platforms import IS_WINDOWS, read_cpu_name, read_cpu_speed, read_memory_total


def run_text(command : List[str], timeout : int = 10, env = None) -> str:
	try:
		return subprocess.run(command, capture_output = True, text = True, timeout = timeout, creationflags = CREATE_NO_WINDOW, env = env).stdout
	except (OSError, subprocess.SubprocessError):
		return ''


def read_nvidia_gpus() -> List[Dict[str, str]]:
	gpus = []
	output = run_text([ 'nvidia-smi', '--query-gpu=name,memory.total,driver_version', '--format=csv,noheader,nounits' ])
	for line in output.strip().splitlines():
		parts = [ part.strip() for part in line.split(',') ]
		if len(parts) < 3:
			continue
		try:
			vram = '%.0f GB' % (float(parts[1]) / 1024)
		except ValueError:
			vram = ''
		gpus.append({ 'name': parts[0], 'vram': vram, 'driver': parts[2] })
	return gpus


def read_cuda_version() -> str:
	"""the newest CUDA the NVIDIA driver supports, from nvidia-smi's header"""
	# older drivers print "CUDA Version: 12.4", newer ones "CUDA UMD Version: 13.5"
	match = re.search(r'CUDA (?:UMD )?Version:\s*([\d.]+)', run_text([ 'nvidia-smi' ]))
	return match.group(1) if match else ''


def read_windows_gpus() -> List[Dict[str, str]]:
	"""any video card on Windows, for PCs without NVIDIA. Windows can't report more than 4 GB of video memory here, so it is left out"""
	script = 'Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + "|" + $_.DriverVersion }'
	output = run_text([ 'powershell', '-NoProfile', '-Command', script ], timeout = 20)
	gpus = []
	for line in output.strip().splitlines():
		name, _, driver = line.partition('|')
		if name.strip():
			gpus.append({ 'name': name.strip(), 'vram': '', 'driver': driver.strip() })
	return gpus


def read_onnxruntime(python_path : str) -> str:
	"""version and execution providers of the onnxruntime in FaceFusion's environment"""
	if not os.path.isfile(python_path):
		return ''
	environment = create_process_environment(python_path)
	script = 'import onnxruntime; print(onnxruntime.__version__); print(", ".join(provider.replace("ExecutionProvider", "").replace("Tensorrt", "TensorRT") for provider in onnxruntime.get_available_providers()))'
	output = run_text([ python_path, '-c', script ], timeout = 60, env = { key: environment.value(key) for key in environment.keys() }).strip().splitlines()
	return '%s  ·  %s' % (output[0], output[1]) if len(output) >= 2 else ''


def read_hardware(python_path : str) -> Dict[str, str]:
	"""the Benchmark page reads CPU, Memory, GPU and ONNX Runtime; the FaceFusion page reads the rest too"""
	info : Dict[str, str] = {}
	cpu = read_cpu_name() or 'Unknown'
	threads = os.cpu_count() or 0
	info['CPU'] = '%s · %d threads' % (cpu, threads)
	speed = read_cpu_speed()
	# the FaceFusion page's System Info rows, laid out like the Operating System row
	info['System CPU'] = '  ·  '.join(part for part in [ cpu, '%d threads' % threads if threads else '', '%.1f GHz' % speed if speed else '' ] if part)
	memory = read_memory_total()
	info['Memory'] = '%.0f GB' % (memory / 1073741824) if memory else 'Unknown'
	gpus = read_nvidia_gpus() if shutil.which('nvidia-smi') else []
	if gpus:
		info['GPU'] = '\n'.join('%s · %s · driver %s' % (gpu['name'], gpu['vram'], gpu['driver']) for gpu in gpus)
		info['CUDA'] = read_cuda_version()
	else:
		info['GPU'] = 'No NVIDIA GPU found'
		if IS_WINDOWS:
			gpus = read_windows_gpus()
	info['System GPU'] = '\n'.join('  ·  '.join(part for part in [ gpu['name'], gpu['vram'] ] if part) for gpu in gpus)
	info['GPU Driver'] = '\n'.join(gpu['driver'] for gpu in gpus if gpu['driver'])
	info['ONNX Runtime'] = read_onnxruntime(python_path)
	return info


class HardwareProbe(QThread):
	"""collects gpu, cpu, memory and onnxruntime details without blocking the window"""
	probed = Signal(dict)

	def __init__(self, python_path : str) -> None:
		super().__init__()
		self.python_path = python_path

	def run(self) -> None:
		self.probed.emit(read_hardware(self.python_path))
