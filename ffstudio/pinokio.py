"""the copy of studioff that pinokio installs: it keeps its own facefusion, python environment and settings inside the app folder.
install.js runs this file to install facefusion, start.js sets FFSTUDIO_PINOKIO so the app looks there"""
import argparse
import os
import shutil
import subprocess
import sys
from typing import List

from ffstudio.platforms import IS_MAC, IS_WINDOWS, conda_env_python, script_command

IS_PINOKIO = os.environ.get('FFSTUDIO_PINOKIO') == '1'
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FACEFUSION_DIR = os.path.join(APP_ROOT, 'facefusion')
# a conda environment, like the Install card's, so the cuda runtime and ffmpeg come with it
FACEFUSION_ENV = os.path.join(FACEFUSION_DIR, '.conda')
FACEFUSION_PYTHON = conda_env_python(FACEFUSION_ENV)
DATA_DIR = os.path.join(APP_ROOT, 'data')


def pick_onnxruntime() -> str:
	if IS_MAC:
		return 'default'
	if shutil.which('nvidia-smi'):
		return 'cuda@12'
	return 'directml' if IS_WINDOWS else 'default'


def run(title : str, program : str, args : List[str], cwd : str, env : dict = None) -> None:
	print('\n== %s' % title, flush = True)
	exit_code = subprocess.call([ program ] + args, cwd = cwd, env = env)
	if exit_code != 0:
		sys.exit('%s failed (exit code %d)' % (title, exit_code))


def install(onnxruntime : str) -> None:
	from ffstudio import TESTED_FACEFUSION_COMMIT
	from ffstudio.environment import create_process_environment, environment_to_dict, find_conda, is_facefusion_root, switch_facefusion_commit
	from ffstudio.pages.setup import FACEFUSION_BRANCH, REPOSITORY_URL

	conda = shutil.which('conda') or find_conda()
	if not conda:
		sys.exit('conda was not found. Pinokio installs it, try Install again from Pinokio.')
	conda_channel = [ '-y', '-p', FACEFUSION_ENV, '--override-channels', '-c', 'conda-forge' ]

	if not is_facefusion_root(FACEFUSION_DIR):
		run('Cloning FaceFusion v4', shutil.which('git') or 'git', [ 'clone', '--branch', FACEFUSION_BRANCH, '--depth', '1', REPOSITORY_URL, FACEFUSION_DIR ], APP_ROOT)
		# only a new clone moves to the tested commit, a reinstall keeps a newer one picked on the FaceFusion page
		print('\n== Switching to the tested commit %s' % TESTED_FACEFUSION_COMMIT[:7], flush = True)
		switched = switch_facefusion_commit(FACEFUSION_DIR, FACEFUSION_BRANCH, TESTED_FACEFUSION_COMMIT)
		if switched['error']:
			sys.exit('Switching to the tested commit failed: %s' % switched['error'])
	if not os.path.isfile(FACEFUSION_PYTHON):
		run('Creating the Python 3.13 environment', *script_command(conda, [ 'create' ] + conda_channel + [ 'python=3.13', 'pip', 'ffmpeg' ]), FACEFUSION_DIR)
	if onnxruntime.startswith('cuda'):
		run('Installing CUDA runtime and cuDNN', *script_command(conda, [ 'install' ] + conda_channel + [ 'cuda-runtime=' + onnxruntime.split('@')[1], 'cudnn=9' ]), FACEFUSION_DIR)
	environment = environment_to_dict(create_process_environment(FACEFUSION_PYTHON))
	# pinokio's shell sets this to true, but facefusion's packages go into its conda environment, not a virtualenv
	environment['PIP_REQUIRE_VIRTUALENV'] = 'false'
	run('Installing FaceFusion requirements (%s)' % onnxruntime, FACEFUSION_PYTHON, [ 'install.py', onnxruntime, '--skip-conda' ], FACEFUSION_DIR, environment)
	run('Checking the FaceFusion packages', FACEFUSION_PYTHON, [ '-c', 'import cv2, numpy, onnxruntime' ], FACEFUSION_DIR, environment)
	print('\nFaceFusion is installed. Press Start in Pinokio.', flush = True)


def main() -> None:
	parser = argparse.ArgumentParser(prog = 'ffstudio.pinokio')
	parser.add_argument('command', choices = [ 'install' ])
	parser.add_argument('--onnxruntime', default = 'auto', help = 'cuda@12, cuda@13, directml, openvino or default, auto picks one for this pc')
	args = parser.parse_args()
	install(pick_onnxruntime() if args.onnxruntime == 'auto' else args.onnxruntime)


if __name__ == '__main__':
	main()
