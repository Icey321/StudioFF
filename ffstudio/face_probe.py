"""
Runs inside the FaceFusion Python environment (NOT the UI environment).

Finds the faces FaceFusion sees in one frame with the current face detector, aligner and
selector settings, and which of them a run would actually process. It stays running and
keeps its models loaded, so a changed setting shows its effect within a moment.

usage: python face_probe.py
	reads one request per line on stdin:
		{"id": 1, "frame": <image path>, "reference": <image path or null>, "sources": [<image path>], "args": [<facefusion option flags>], "mask": <png path or null>}
	prints one line per request: FACES {json}
	with a mask path it also writes the face mask a run would blend with there, white where the new face goes
	each face has its position in the sorted and filtered list, the number reference_face_position counts in,
	and with "people_file" (the embeddings a people scan saved) the index of the person it belongs to

	a people request finds who appears in a target instead:
		{"id": 2, "people": <image or video path>, "start": <frame>, "end": <frame or null>, "samples": 48, "crops": <folder>, "args": [...]}
	it looks at evenly spread frames, groups the faces by identity with the Reference face Distance and
	replies with one entry per person: the frame and box where they are seen best, their position there,
	how many of the frames show them and a crop of the face. it prints PEOPLE <done> <total> while it works
"""
import contextlib
import io
import json
import os
import sys

RESULT_PREFIX = 'FACES '
# the people of a target, one embedding per person, written by a people scan next to the crops
PEOPLE_FILE = 'people.npy'


def reply(result : dict) -> None:
	sys.stdout.write(RESULT_PREFIX + json.dumps(result) + '\n')
	sys.stdout.flush()


def same_box(face, other) -> bool:
	import numpy

	return numpy.allclose(face.bounding_box, other.bounding_box, atol = 1)


def create_crop_mask(crop_frame, crop_landmark_68):
	"""the masks face_swapper combines for one face crop, from the face masker settings"""
	import numpy

	from facefusion import state_manager
	from facefusion.face_masker import create_area_mask, create_box_mask, create_occlusion_mask, create_region_mask

	mask_types = state_manager.get_item('face_mask_types') or [ 'box' ]
	crop_masks = []
	if 'box' in mask_types:
		crop_masks.append(create_box_mask(crop_frame, state_manager.get_item('face_mask_blur'), state_manager.get_item('face_mask_padding')))
	if 'occlusion' in mask_types:
		crop_masks.append(create_occlusion_mask(crop_frame))
	if 'area' in mask_types and state_manager.get_item('face_mask_areas'):
		crop_masks.append(create_area_mask(crop_frame, crop_landmark_68, state_manager.get_item('face_mask_areas')))
	if 'region' in mask_types and state_manager.get_item('face_mask_regions'):
		crop_masks.append(create_region_mask(crop_frame, state_manager.get_item('face_mask_regions')))
	if not crop_masks:
		return numpy.ones(crop_frame.shape[:2], numpy.float32)
	return numpy.minimum.reduce(crop_masks).clip(0, 1)


def write_mask(path : str, frame, faces) -> None:
	"""pastes each face's mask back into a frame sized image the way a run pastes the new face"""
	import cv2
	import numpy

	from facefusion import state_manager
	from facefusion.face_helper import calculate_paste_area, warp_face_by_face_landmark_5

	# the crop face_swapper uses when it is on, its mask padding and blur are relative to that crop
	template, size = 'arcface_128', (512, 512)
	if 'face_swapper' in (state_manager.get_item('processors') or []):
		try:
			from facefusion.processors.modules.face_swapper.core import get_model_options

			template = get_model_options().get('template') or template
		except Exception:
			pass
	mask = numpy.zeros(frame.shape[:2], numpy.float32)
	for face in faces:
		crop_frame, affine_matrix = warp_face_by_face_landmark_5(frame, face.landmark_set.get('5/68'), template, size)
		crop_landmark_68 = cv2.transform(face.landmark_set.get('68').reshape(1, -1, 2), affine_matrix).reshape(-1, 2)
		crop_mask = create_crop_mask(crop_frame, crop_landmark_68)
		(x1, y1, x2, y2), paste_matrix = calculate_paste_area(frame, crop_frame, affine_matrix)
		if x2 <= x1 or y2 <= y1:
			continue
		paste_mask = cv2.warpAffine(crop_mask, paste_matrix, (x2 - x1, y2 - y1)).clip(0, 1)
		mask[y1:y2, x1:x2] = numpy.maximum(mask[y1:y2, x1:x2], paste_mask)
	# imencode and tofile, imwrite cannot write to paths with non ascii characters on windows
	cv2.imencode('.png', (mask * 255).astype(numpy.uint8))[1].tofile(path)


def prepare(request : dict, model_keys : set, path : str, result : dict) -> bool:
	"""applies the request's settings and makes sure the models they need are there, a problem goes into the result"""
	from facefusion import face_aligner, face_classifier, face_detector, face_masker, face_recognizer, state_manager
	from facefusion.args_helper import apply_args
	from facefusion.program import create_program

	# argparse writes its whole usage on a bad value, only its last line says what was wrong
	errors = io.StringIO()
	try:
		with contextlib.redirect_stderr(errors):
			args = vars(create_program().parse_args([ 'run', '-t', path, '-o', path ] + request.get('args', [])))
	except SystemExit:
		result['error'] = 'FaceFusion did not accept the settings: ' + (errors.getvalue().strip().splitlines() or [ '' ])[-1].split('error: ')[-1]
		return False
	apply_args(args, state_manager.set_item)

	# the models only need checking (and maybe downloading) once per choice of model
	model_key = (state_manager.get_item('face_detector_model'), state_manager.get_item('face_aligner_model'))
	if model_key not in model_keys:
		print('Loading the face models', flush = True)
		if not all(module.pre_check() for module in [ face_detector, face_aligner, face_recognizer, face_classifier ]):
			result['error'] = 'Could not download the face models'
			return False
		model_keys.add(model_key)
	# the occluder and parser models only load when a mask needs them
	mask_types = state_manager.get_item('face_mask_types') or []
	mask_key = (state_manager.get_item('face_occluder_model'), state_manager.get_item('face_parser_model'))
	if request.get('mask') and ('occlusion' in mask_types or 'region' in mask_types) and mask_key not in model_keys:
		print('Loading the face mask models', flush = True)
		if not face_masker.pre_check():
			result['error'] = 'Could not download the face mask models'
			return False
		model_keys.add(mask_key)
	return True


def find_position(face, ordered) -> int:
	"""where a face is in the sorted and filtered list, the number reference_face_position counts in, -1 when the filters skip it"""
	return next((index for index, other in enumerate(ordered) if same_box(face, other)), -1)


def scan(request : dict, model_keys : set) -> dict:
	import numpy

	from facefusion import face_selector, face_store, state_manager
	from facefusion.face_creator import get_many_faces, get_one_face, get_static_faces
	from facefusion.vision import read_image

	result = { 'id': request.get('id'), 'faces': [], 'width': 0, 'height': 0 }
	if not prepare(request, model_keys, request.get('frame'), result):
		return result

	frame = read_image(request.get('frame'))
	if frame is None:
		result['error'] = 'Could not read the frame'
		return result
	result['height'], result['width'] = frame.shape[:2]
	reference_frame = read_image(request.get('reference')) if request.get('reference') else frame
	source_frames = [ source_frame for source_frame in (read_image(path) for path in request.get('sources') or []) if source_frame is not None ]

	# results are cached per frame, a changed setting has to detect again
	face_store.clear()
	faces = get_many_faces([ frame ])
	face_store.clear()
	selected = face_selector.select_faces(reference_frame, source_frames, [ frame ])
	mode = state_manager.get_item('face_selector_mode')
	result['mode'] = mode
	# the order reference_face_position counts in, the sources decide the auto gender and race filters
	ordered = face_selector.sort_and_filter_faces(get_static_faces(source_frames), faces)
	people_file = request.get('people_file')
	people = numpy.load(people_file) if people_file and os.path.isfile(people_file) else None

	reference_face = None
	if mode == 'reference':
		all_reference_faces = get_static_faces([ reference_frame ])
		# the gender, race and age filters apply to the reference frame too
		result['reference_faces'] = len(all_reference_faces)
		reference_faces = face_selector.sort_and_filter_faces(get_static_faces(source_frames), all_reference_faces)
		reference_face = get_one_face(reference_faces, state_manager.get_item('reference_face_position'))
		result['reference_found'] = reference_face is not None
		result['reference_distance'] = state_manager.get_item('reference_face_distance')

	for face in faces:
		item =\
		{
			'box': [ round(float(value), 1) for value in face.bounding_box ],
			'score': round(float(face.score_set.get('detector')), 3),
			'gender': face.gender,
			'age': [ int(face.age.start), int(face.age.stop) ],
			'race': face.race,
			'selected': any(same_box(face, other) for other in selected),
			'position': find_position(face, ordered)
		}
		if people is not None and len(people):
			# the person this face belongs to, the closest one within the Reference face Distance
			distances = numpy.interp(1 - people @ face.embedding_norm, [ 0, 2 ], [ 0, 1 ])
			if distances.min() < state_manager.get_item('reference_face_distance'):
				item['person'] = int(distances.argmin())
		if reference_face is not None:
			distance = face_selector.calculate_face_distance(face, reference_face)
			item['distance'] = round(float(numpy.interp(distance, [ 0, 2 ], [ 0, 1 ])), 3)
		result['faces'].append(item)

	if request.get('mask'):
		# the faces a run would process, or all of them when none would, so the mask can still be tuned
		masked = [ face for face in faces if any(same_box(face, other) for other in selected) ] or faces
		write_mask(request.get('mask'), frame, masked)
		result['mask'] = request.get('mask')
		result['mask_types'] = state_manager.get_item('face_mask_types') or []
		result['masked'] = len(masked) if faces else 0
	return result


def save_crop(path : str, frame, box) -> None:
	"""the face with some room around it, small enough for a list row"""
	import cv2

	x1, y1, x2, y2 = box
	margin = 0.3 * max(x2 - x1, y2 - y1)
	height, width = frame.shape[:2]
	left, top = int(max(0, x1 - margin)), int(max(0, y1 - margin))
	right, bottom = int(min(width, x2 + margin)), int(min(height, y2 + margin))
	crop = frame[top:bottom, left:right]
	if crop.size == 0:
		return
	scale = 128 / max(crop.shape[:2])
	if scale < 1:
		crop = cv2.resize(crop, (max(1, int(crop.shape[1] * scale)), max(1, int(crop.shape[0] * scale))), interpolation = cv2.INTER_AREA)
	cv2.imencode('.jpg', crop, [ cv2.IMWRITE_JPEG_QUALITY, 90 ])[1].tofile(path)


def find_people(request : dict, model_keys : set) -> dict:
	"""groups the faces of evenly spread frames by identity, the way reference mode matches them on every frame"""
	import cv2
	import numpy

	from facefusion import face_selector, face_store, state_manager
	from facefusion.face_creator import get_many_faces
	from facefusion.filesystem import is_video
	from facefusion.vision import read_image

	path = request.get('people')
	result = { 'id': request.get('id'), 'people': [], 'samples': 0, 'width': 0, 'height': 0 }
	if not prepare(request, model_keys, path, result):
		return result
	# facefusion's own video reader needs its video pool, which only a run sets up
	capture = cv2.VideoCapture(path) if is_video(path) else None

	def read_frame(frame_index : int):
		if capture is None:
			return read_image(path)
		capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
		success, frame = capture.read()
		return frame if success else None

	if capture is not None:
		total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
		start = max(0, int(request.get('start') or 0))
		end = min(total, int(request.get('end'))) if request.get('end') is not None else total
		count = max(1, min(int(request.get('samples') or 48), end - start))
		indices = sorted({ int(start + (end - start) * (step + 0.5) / count) for step in range(count) })
	else:
		indices = [ 0 ]
	limit = state_manager.get_item('reference_face_distance')
	groups = []
	for done, frame_index in enumerate(indices):
		print('PEOPLE %d %d' % (done, len(indices)), flush = True)
		frame = read_frame(frame_index)
		if frame is None:
			continue
		result['height'], result['width'] = frame.shape[:2]
		result['samples'] += 1
		face_store.clear()
		faces = get_many_faces([ frame ])
		ordered = face_selector.sort_and_filter_faces([], faces)
		# the closest pairs first, so two people in one frame never end up in the same group
		pairs = []
		for face_index, face in enumerate(faces):
			for group_index, group in enumerate(groups):
				distance = float(numpy.interp(face_selector.calculate_face_distance(face, group['face']), [ 0, 2 ], [ 0, 1 ]))
				if distance < limit:
					pairs.append((distance, face_index, group_index))
		matched = {}
		for _, face_index, group_index in sorted(pairs):
			if face_index not in matched and group_index not in matched.values():
				matched[face_index] = group_index
		for face_index, face in enumerate(faces):
			if face_index not in matched:
				groups.append({ 'face': face, 'count': 0, 'quality': -1.0, 'position': -1 })
				matched[face_index] = len(groups) - 1
			group = groups[matched[face_index]]
			group['count'] += 1
			box = face.bounding_box
			position = find_position(face, ordered)
			# the biggest clearest view becomes the reference, one the face selector filters skip only when there is no other
			quality = float(face.score_set.get('detector')) * float((box[2] - box[0]) * (box[3] - box[1]))
			if (position >= 0, quality) > (group['position'] >= 0, group['quality']):
				group.update({ 'face': face, 'quality': quality, 'position': position, 'frame': frame_index, 'box': [ round(float(value), 1) for value in box ], 'image': frame })
	if capture is not None:
		capture.release()
	crops = request.get('crops')
	groups.sort(key = lambda group: -group['count'])
	if crops:
		os.makedirs(crops, exist_ok = True)
		# a frame scan names the faces it finds after these, also once the probe has restarted
		if groups:
			numpy.save(os.path.join(crops, PEOPLE_FILE), numpy.stack([ group['face'].embedding_norm for group in groups ]))
		elif os.path.isfile(os.path.join(crops, PEOPLE_FILE)):
			os.remove(os.path.join(crops, PEOPLE_FILE))
	for index, group in enumerate(groups):
		crop_path = os.path.join(crops, 'person_%d.jpg' % (index + 1)) if crops else None
		if crop_path:
			save_crop(crop_path, group['image'], group['box'])
		face = group['face']
		# how close the most similar other person is, a reference distance above it would swap both of them
		distances = [ float(numpy.interp(face_selector.calculate_face_distance(face, other['face']), [ 0, 2 ], [ 0, 1 ])) for other in groups if other is not group ]
		result['people'].append(
		{
			'frame': group['frame'],
			'box': group['box'],
			'position': group['position'],
			'count': group['count'],
			'nearest': round(min(distances), 3) if distances else None,
			'crop': crop_path,
			'gender': face.gender,
			'age': [ int(face.age.start), int(face.age.stop) ]
		})
	return result


def main() -> int:
	# run from the facefusion folder, the script itself lives in the ui folder
	sys.path.insert(0, os.getcwd())

	from facefusion import face_store, inference_manager, logger, process_manager, session_context, session_manager, state_manager

	# the same setup as facefusion's own cli before it runs a command
	state_manager.init()
	logger.init('error')
	session_manager.set_cli_session(session_context.get_session_id(), session_manager.create_cli_session())
	face_store.init()
	inference_manager.init()
	process_manager.init()
	print('Face probe ready', flush = True)

	model_keys : set = set()
	for line in sys.stdin:
		if not line.strip():
			continue
		try:
			request = json.loads(line)
		except ValueError:
			continue
		try:
			reply(find_people(request, model_keys) if request.get('people') else scan(request, model_keys))
		except Exception as exception:
			reply({ 'id': request.get('id'), 'faces': [], 'people': [], 'width': 0, 'height': 0, 'error': '%s: %s' % (type(exception).__name__, exception) })
	return 0


if __name__ == '__main__':
	sys.exit(main())
