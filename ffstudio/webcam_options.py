"""what the Webcam page starts with and grays out: a live picture needs options that keep up with the camera"""
from typing import Any, Dict, Tuple

from ffstudio.option_widgets import KEEP_VALUE, REFERENCE_OPTIONS, setting_locks, unused_locks

# the page's first values in place of a copy of Studio's: the swapper at its own size and a plain box mask,
# Studio's sharper settings take seconds a frame
WEBCAM_DEFAULTS : Dict[str, Any] =\
{
	'processors': [ 'face_swapper' ],
	'face_selector_mode': 'many',
	'face_mask_types': [ 'box' ],
	# the api checks the pixel boost against the sizes of facefusion's default model, hyperswap at its own size is the
	# quickest swap it takes (inswapper_128 at 128x128 is refused)
	'face_swapper_model': 'hyperswap_1a_256',
	'face_swapper_pixel_boost': '256x256'
}
# a live picture has no target file to take a reference face from
WEBCAM_SELECTOR_MODES = [ 'many', 'one' ]
NO_REFERENCE_REASON = 'No reference face on a live webcam'
NOT_USED_REASON = 'Not used for the webcam'
# groups and options that only make or read a file
NOT_USED_GROUPS = [ 'paths', 'frame_extraction', 'output_creation', 'voice_extractor', 'jobs', 'workflow' ]
NOT_USED_OPTIONS = [ 'trim_frame_start', 'trim_frame_end', 'temp_frame_format', 'keep_temp' ]
# processors a camera picture without sound cannot use
NO_AUDIO_PROCESSORS = [ 'lip_syncer' ]


def webcam_locks(schema : Any, values : Dict[str, Any]) -> Dict[str, Tuple[Any, str]]:
	"""(value, reason) for the rows the Webcam page grays out: the usual ones for a video, the face selector rows the mode
	leaves unused, the reference rows and the rows that only make or read a file"""
	locks = unused_locks(values.get('face_selector_mode') or 'many')
	locks.update(setting_locks(schema, values, 'video'))
	for dest, option in schema.options.items():
		if option.get('group') in NOT_USED_GROUPS or dest in NOT_USED_OPTIONS:
			locks[dest] = (KEEP_VALUE, NOT_USED_REASON)
		if dest in REFERENCE_OPTIONS:
			locks[dest] = (KEEP_VALUE, NO_REFERENCE_REASON)
		if option.get('group') in NO_AUDIO_PROCESSORS:
			locks[dest] = (KEEP_VALUE, 'Needs sound, the webcam sends pictures only')
	return locks
