import json
import os
from typing import Any, Dict, List, Optional

import cv2
import numpy
from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from ffstudio.config import APP_DIR, PREVIEW_DIR
from ffstudio.environment import create_process_environment, kill_process_tree
from ffstudio.media import write_image
from ffstudio.session import Session

RESULT_PREFIX = 'FACES '
FACES_DIR = os.path.join(PREVIEW_DIR, 'faces')
# the groups that change which faces facefusion finds and picks
FACE_GROUPS = [ 'face_detector', 'face_aligner', 'face_selector', 'face_tracker', 'execution' ]
# the groups that change the face mask, face_swapper picks the crop the mask padding is measured on
MASK_GROUPS = [ 'face_masker', 'face_swapper' ]


class FaceProbe(QObject):
	"""keeps face_probe.py running in the facefusion environment and asks it which faces it sees in a frame

	one request runs at a time, a request made meanwhile replaces the waiting one, so scrubbing only scans where it stops.
	a people scan waits apart from those and goes first, a frame scan can not push it out
	"""
	scanned = Signal(dict)
	loading = Signal(bool)
	# the people found in a whole target, and how many of its frames were looked at so far
	people_found = Signal(dict)
	people_progress = Signal(int, int)

	def __init__(self, session : Session, parent : Optional[QObject] = None) -> None:
		super().__init__(parent)
		self.session = session
		self.process : Optional[QProcess] = None
		self.python_path = ''
		self.buffer = ''
		self.request_id = 0
		self.running : Optional[Dict[str, Any]] = None
		self.waiting : Optional[Dict[str, Any]] = None
		self.waiting_people : Optional[Dict[str, Any]] = None
		self.errors : List[str] = []
		# its models let go after the same time as the preview worker's, and when a whole run needs the memory
		self.idle_timer = QTimer(self)
		self.idle_timer.setSingleShot(True)
		self.idle_timer.timeout.connect(self.stop)
		session.busy_changed.connect(self.on_busy_changed)

	def is_idle(self) -> bool:
		return self.running is None and self.waiting is None and self.waiting_people is None

	def start_idle_timer(self) -> None:
		minutes = int(self.session.config.get('preview_keep_loaded_minutes') or 0)
		if minutes > 0 and self.is_running() and self.is_idle():
			self.idle_timer.start(minutes * 60 * 1000)

	def on_busy_changed(self, busy : bool, _ : str) -> None:
		if busy and not self.session.task_is_preview and self.is_running() and self.is_idle():
			self.stop()

	def is_running(self) -> bool:
		return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

	def scan(self, frame : numpy.ndarray, reference : Optional[numpy.ndarray], sources : List[str], args : List[str], tag : Any = None, mask : bool = False, people_file : Optional[str] = None) -> None:
		self.request_id += 1
		self.waiting = { 'id': self.request_id, 'frame': frame, 'reference': reference, 'sources': sources, 'args': args, 'tag': tag, 'mask': mask, 'people_file': people_file }
		if self.running is None:
			self.send_next()

	def find_people(self, path : str, start : Optional[int], end : Optional[int], args : List[str], crops : str, tag : Any = None, samples : int = 48) -> None:
		self.request_id += 1
		self.waiting_people = { 'id': self.request_id, 'people': path, 'start': start, 'end': end, 'samples': samples, 'crops': crops, 'args': args, 'tag': tag }
		if self.running is None:
			self.send_next()

	def is_finding_people(self) -> bool:
		return any(request and request.get('people') for request in [ self.running, self.waiting_people ])

	def emit_result(self, request : Dict[str, Any], result : Dict[str, Any]) -> None:
		result['tag'] = request.get('tag')
		if request.get('people'):
			self.people_found.emit(result)
		else:
			self.scanned.emit(result)

	def send_next(self) -> None:
		if self.waiting_people:
			request, self.waiting_people = self.waiting_people, None
		else:
			request, self.waiting = self.waiting, None
		if request is None:
			self.start_idle_timer()
			return
		self.idle_timer.stop()
		if self.python_path != self.session.python_path:
			self.stop()
		if not self.is_running() and not self.start():
			self.emit_result(request, { 'id': request.get('id'), 'faces': [], 'people': [], 'error': 'Could not start FaceFusion' })
			return
		if request.get('people'):
			self.running = request
			line = json.dumps({ key: request.get(key) for key in [ 'id', 'people', 'start', 'end', 'samples', 'crops', 'args' ] })
			self.process.write((line + '\n').encode('utf-8'))
			return
		os.makedirs(FACES_DIR, exist_ok = True)
		frame_path = os.path.join(FACES_DIR, 'frame.png')
		reference_path = os.path.join(FACES_DIR, 'reference.png')
		write_image(frame_path, request.get('frame'))
		if request.get('reference') is not None:
			write_image(reference_path, request.get('reference'))
		self.running = request
		mask_path = os.path.join(FACES_DIR, 'mask.png') if request.get('mask') else None
		line = json.dumps({ 'id': request.get('id'), 'frame': frame_path, 'reference': reference_path if request.get('reference') is not None else None, 'sources': request.get('sources'), 'args': request.get('args'), 'mask': mask_path, 'people_file': request.get('people_file') })
		self.process.write((line + '\n').encode('utf-8'))

	def start(self) -> bool:
		if not self.session.is_configured():
			return False
		self.python_path = self.session.python_path
		self.buffer = ''
		self.errors = []
		self.process = QProcess(self)
		self.process.setProgram(self.python_path)
		self.process.setArguments([ '-u', os.path.join(APP_DIR, 'face_probe.py') ])
		self.process.setWorkingDirectory(self.session.facefusion_root)
		self.process.setProcessEnvironment(create_process_environment(self.python_path))
		self.process.readyReadStandardOutput.connect(self.on_output)
		self.process.readyReadStandardError.connect(self.on_errors)
		self.process.finished.connect(self.on_finished)
		self.process.errorOccurred.connect(self.on_error)
		self.process.start()
		if self.process is None:
			return False
		self.loading.emit(True)
		return True

	def stop(self) -> None:
		self.idle_timer.stop()
		self.waiting = None
		self.waiting_people = None
		self.running = None
		if self.process:
			process, self.process = self.process, None
			process.finished.disconnect(self.on_finished)
			if process.state() != QProcess.ProcessState.NotRunning:
				kill_process_tree(int(process.processId()))
				process.kill()
				process.waitForFinished(2000)
			process.deleteLater()
		self.loading.emit(False)

	def on_output(self) -> None:
		# stop lets go of the process before waiting for it, its last output can still arrive
		if not self.process:
			return
		self.buffer += bytes(self.process.readAllStandardOutput().data()).decode('utf-8', errors = 'replace')
		while '\n' in self.buffer:
			line, self.buffer = self.buffer.split('\n', 1)
			line = line.strip()
			if line.startswith('Loading'):
				self.loading.emit(True)
			if line.startswith('PEOPLE '):
				done, total = (int(value) for value in line.split()[1:3])
				self.people_progress.emit(done, total)
			if line.startswith(RESULT_PREFIX):
				self.on_result(line[len(RESULT_PREFIX):])

	def on_errors(self) -> None:
		if not self.process:
			return
		text = bytes(self.process.readAllStandardError().data()).decode('utf-8', errors = 'replace')
		self.errors = (self.errors + [ line for line in text.splitlines() if line.strip() ])[-6:]

	def on_result(self, text : str) -> None:
		try:
			result = json.loads(text)
		except ValueError:
			return
		request, self.running = self.running, None
		self.loading.emit(False)
		if request and request.get('id') == result.get('id'):
			self.emit_result(request, result)
		self.send_next()

	def on_error(self, error : QProcess.ProcessError) -> None:
		# a process that never started never finishes either
		if error == QProcess.ProcessError.FailedToStart and self.process:
			self.process.finished.disconnect(self.on_finished)
			self.on_finished()

	def on_finished(self, *_ : Any) -> None:
		request, self.running = self.running, None
		self.process.deleteLater()
		self.process = None
		self.loading.emit(False)
		if request:
			self.emit_result(request, { 'id': request.get('id'), 'faces': [], 'people': [], 'error': 'FaceFusion stopped: ' + (self.errors[-1] if self.errors else 'no reason given') })


def describe_faces(result : Dict[str, Any], reference_index : Optional[int] = None) -> str:
	"""one line on what was found, and when nothing would be processed, which setting is likely the cause"""
	if result.get('error'):
		return 'Face detection failed: ' + result.get('error')
	faces = result.get('faces') or []
	if not faces:
		return 'No face detected in this frame. Try a lower Face detector Score, a bigger Size, another Model, or more Angles for tilted or sideways faces.'
	found = '1 face detected' if len(faces) == 1 else '%d faces detected' % len(faces)
	selected = [ face for face in faces if face.get('selected') ]
	mode = result.get('mode')
	if selected:
		if len(selected) == len(faces):
			return found + (', it will be processed' if len(faces) == 1 else ', all will be processed') + ' (face selector: %s).' % mode
		return found + ', %d will be processed (outlined), the others are skipped by the face selector (%s).' % (len(selected), mode)
	if mode == 'reference':
		frame = ' #%d' % reference_index if reference_index is not None else ''
		if not result.get('reference_found') and result.get('reference_faces'):
			return found + ', but none will be processed: the face selector Gender, Race or Age filter also skips the face in the reference frame%s.' % frame
		if not result.get('reference_found'):
			return found + ', but none will be processed: no face was found in the reference frame%s. Pick a frame with a clear face and click Use as reference frame.' % frame
		distances = [ face.get('distance') for face in faces if face.get('distance') is not None ]
		nearest = ' The closest is %.2f.' % min(distances) if distances else ''
		return found + ', but none will be processed: none is closer to the face in reference frame%s than the Reference face Distance (%.2f).%s Raise the Distance or pick another reference frame.' % (frame, result.get('reference_distance') or 0, nearest)
	return found + ', but none will be processed: the face selector Gender, Race or Age filter skips them.'


def no_face(result : Dict[str, Any]) -> bool:
	"""face detection ran fine and found nothing, the preview gets a red border and the line under it turns red"""
	return not result.get('error') and not result.get('faces')


def faces_text_color(result : Dict[str, Any], problem : bool) -> tuple:
	"""the light and dark theme color of the line under the preview: red with no face found, orange for other problems"""
	if no_face(result):
		return '#dc2626', '#f87171'
	return ('#b45309', '#f0b35a') if problem else ('#707070', '#9a9a9a')


def read_mask(path : Optional[str]) -> Optional[numpy.ndarray]:
	if not path or not os.path.isfile(path):
		return None
	return cv2.imdecode(numpy.fromfile(path, dtype = numpy.uint8), cv2.IMREAD_GRAYSCALE)


def describe_mask(result : Dict[str, Any], values : Dict[str, Any]) -> str:
	"""one line on what the shaded mask is made of"""
	if result.get('error'):
		return ''
	if not result.get('masked'):
		return 'Mask: no face to mask in this frame.'
	parts = []
	for mask_type in result.get('mask_types') or [ 'box' ]:
		if mask_type == 'box':
			padding = values.get('face_mask_padding') or [ 0, 0, 0, 0 ]
			blur = values.get('face_mask_blur')
			parts.append('box (padding %s, blur %.2f)' % ('/'.join(str(int(value)) for value in padding), blur if blur is not None else 0.3))
		elif mask_type == 'area':
			parts.append('area (%s)' % ', '.join(values.get('face_mask_areas') or [ 'all' ]))
		elif mask_type == 'region':
			parts.append('region (%d parts)' % len(values.get('face_mask_regions') or []) if values.get('face_mask_regions') else 'region (all parts)')
		else:
			parts.append(mask_type)
	faces = '1 face' if result.get('masked') == 1 else '%d faces' % result.get('masked')
	return 'Mask on %s: %s. The shaded part is replaced, the rest keeps the target.' % (faces, ' × '.join(parts))


def face_tag(index : int, face : Dict[str, Any]) -> str:
	age = face.get('age') or [ 0, 0 ]
	parts = [ '#%d' % (index + 1), '%d%%' % round((face.get('score') or 0) * 100) ]
	# the distance comes right after the score, it is what decides which faces the reference selector takes
	if face.get('distance') is not None:
		parts.append('Dist %.2f' % face.get('distance'))
	parts.append('%s %d-%d' % ((face.get('gender') or '?')[0].upper(), age[0], age[1]))
	if face.get('race'):
		parts.append(face.get('race').capitalize())
	return '  '.join(parts)


def face_landmarks(result : Dict[str, Any], mode : str = '68') -> List[tuple]:
	"""the landmarks of every detected face as PreviewView draws them, the 5 key points or all 68 by the Landmarks mode"""
	return [ ((face.get('landmarks') or []) if mode == '68' else [], (face.get('key_points') or []) if mode == '5' else [], bool(face.get('estimated'))) for face in result.get('faces') or [] ]


def describe_landmarks(result : Dict[str, Any], mode : str = '68') -> str:
	"""one line on the landmarks, and what to change when the aligner had to guess them"""
	faces = result.get('faces') or []
	if not faces:
		return ''
	if mode == '5':
		return 'Landmarks: the 5 key points the face is aligned by, the eyes, the nose tip and the mouth corners.'
	estimated = sum(1 for face in faces if face.get('estimated'))
	if not estimated:
		return 'Landmarks: the face aligner placed all 68 points.'
	which = 'this face' if len(faces) == 1 else ('%d of the faces' % estimated if estimated < len(faces) else 'all faces')
	return 'Landmarks: the face aligner was unsure on %s (orange), its points are estimated from the 5 the detector found. A lower Face aligner Score or another aligner Model may place them.' % which
