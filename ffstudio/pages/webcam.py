"""the Webcam page: swaps the faces of the people on the webcam live, through facefusion's api (see stream_client.py),
and can hand the swapped picture to a virtual camera for other apps (see virtual_camera.py)"""
import os
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

import cv2
import numpy
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtMultimedia import QCamera, QMediaCaptureSession, QMediaDevices, QVideoFrame, QVideoSink
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, CheckBox, ComboBox, FluentIcon, MessageBox, PrimaryPushButton, SimpleCardWidget, SubtitleLabel, TogglePushButton, ToolTipFilter, TransparentPushButton, isDarkTheme

from ffstudio.config import DATA_DIR
from ffstudio.media import browse_dir, format_seconds, media_kind
from ffstudio.pages.setup import SYSTEM_GROUPS
from ffstudio.pages.studio import LOCK_DRIVERS, SettingsColumn, SourceCard, SourceSelection, elastic
from ffstudio.session import OptionStore, Session
from ffstudio.stream_client import StreamClient, StreamServer, upload_ready_path
from ffstudio.virtual_camera import INSTALL_TEXT, INSTALL_TITLE, PackageInstaller, VirtualCamera, is_installed
from ffstudio.webcam_options import NO_REFERENCE_REASON, WEBCAM_DEFAULTS, WEBCAM_SELECTOR_MODES, webcam_locks

# the largest side of the frames sent to facefusion, a bigger frame takes longer to send and to check
FRAME_SIZES = [ 480, 640, 960, 1280 ]
FRAME_SIZE_DEFAULT = 640
FRAME_JPEG_QUALITY = 85
# copies of sources too big for the api
UPLOAD_DIR = os.path.join(DATA_DIR, 'webcam_sources')
# facefusion's api keeps its models loaded this long after Stop, so Start again is quick
SERVER_IDLE_MINUTES = 5
# the frames the fps is counted over
FPS_WINDOW = 10
# the border around the picture while the faces are swapped
SWAP_BORDER_COLOR = QColor('#2ecc71')
SWAP_BORDER_WIDTH = 4
CAMERA_OFF_TEXT = 'The camera is off. Press Enable Camera to see it.'
READY_TEXT = 'Ready · the models are loaded, Swap Face starts at once'


def encode_frame(image : QImage, size : int) -> bytes:
	image = scaled_frame(image, size)
	data = QByteArray()
	buffer = QBuffer(data)
	buffer.open(QIODevice.OpenModeFlag.WriteOnly)
	image.save(buffer, 'JPG', FRAME_JPEG_QUALITY)
	return bytes(data.data())


def scaled_frame(image : QImage, size : int) -> QImage:
	if max(image.width(), image.height()) > size:
		return image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
	return image


def bgr_array(image : QImage) -> numpy.ndarray:
	image = image.convertToFormat(QImage.Format.Format_BGR888)
	rows = numpy.frombuffer(image.constBits(), numpy.uint8, image.sizeInBytes()).reshape(image.height(), image.bytesPerLine())
	return rows[:, :image.width() * 3].reshape(image.height(), image.width(), 3).copy()


class FrameView(QWidget):
	"""a picture fitted into the widget, with a line of text while there is none, and a green border while the faces are swapped"""

	def __init__(self, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.pixmap = QPixmap()
		self.text = CAMERA_OFF_TEXT
		self.border = False
		self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
		self.setMinimumSize(320, 240)

	def set_image(self, image : QImage) -> None:
		self.pixmap = QPixmap.fromImage(image)
		self.update()

	def set_text(self, text : str) -> None:
		self.pixmap = QPixmap()
		self.text = text
		self.update()

	def set_border(self, border : bool) -> None:
		self.border = border
		self.update()

	def paintEvent(self, _) -> None:
		painter = QPainter(self)
		painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
		painter.fillRect(self.rect(), Qt.GlobalColor.black if isDarkTheme() else Qt.GlobalColor.darkGray)
		inner = self.rect().adjusted(SWAP_BORDER_WIDTH, SWAP_BORDER_WIDTH, -SWAP_BORDER_WIDTH, -SWAP_BORDER_WIDTH)
		if self.pixmap.isNull():
			painter.setPen(Qt.GlobalColor.white)
			painter.drawText(inner, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.text)
		else:
			scaled = self.pixmap.size().scaled(inner.size(), Qt.AspectRatioMode.KeepAspectRatio)
			target = QRectF(inner.x() + (inner.width() - scaled.width()) / 2, inner.y() + (inner.height() - scaled.height()) / 2, scaled.width(), scaled.height())
			painter.drawPixmap(target, self.pixmap, QRectF(self.pixmap.rect()))
		if self.border:
			pen = QPen(SWAP_BORDER_COLOR, SWAP_BORDER_WIDTH)
			pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
			painter.setPen(pen)
			half = SWAP_BORDER_WIDTH / 2
			painter.drawRect(QRectF(self.rect()).adjusted(half, half, -half, -half))


class WebcamPage(SourceSelection, QWidget):
	SOURCE_KEYS = ('webcam_source_paths', 'webcam_unused_source_paths', 'webcam_source_names', 'webcam_source_groups', 'webcam_removed_sources')

	def __init__(self, session : Session, parent : Optional[QWidget] = None) -> None:
		super().__init__(parent)
		self.setObjectName('webcam')
		self.session = session
		self.load_sources()
		# its own processors and options, the system settings from Setup are shared
		self.store = OptionStore(session, 'webcam_options', 'webcam_processor_order', WEBCAM_DEFAULTS)
		stored = session.config.get('webcam') or {}
		self.server = StreamServer(self)
		self.client = StreamClient(self)
		self.camera : Optional[QCamera] = None
		self.capture = QMediaCaptureSession(self)
		self.sink = QVideoSink(self)
		self.capture.setVideoSink(self.sink)
		self.sink.videoFrameChanged.connect(self.on_camera_frame)
		self.latest_image : Optional[QImage] = None
		# the camera frame waiting to be sent is newer than the one out
		self.latest_is_new = False
		self.streaming = False
		self.starting = False
		# Enable Camera opens the stream ahead of Swap Face and sends one frame, so facefusion loads its models.
		# its answers are not shown while preloading; the stream stays open, Swap Face only starts showing them
		self.preloading = False
		# one frame goes out to load the models, again after a model or processor changed
		self.warm_needed = False
		self.models_ready = False
		self.swapped_count = 0
		self.frame_times : Deque[float] = deque(maxlen = FPS_WINDOW)
		self.last_answer = 0.0
		self.virtual_camera = VirtualCamera()
		self.installer = PackageInstaller(self)
		self.installer.finished.connect(self.on_installed)
		self.idle_timer = QTimer(self)
		self.idle_timer.setSingleShot(True)
		self.idle_timer.setInterval(SERVER_IDLE_MINUTES * 60 * 1000)
		self.idle_timer.timeout.connect(self.server.stop)
		# options changed while streaming go to facefusion together, a slider sends many changes
		self.state_timer = QTimer(self)
		self.state_timer.setSingleShot(True)
		self.state_timer.setInterval(300)
		self.state_timer.timeout.connect(self.send_changed_values)
		self.changed_values : Dict[str, Any] = {}
		self.sources_changed = False

		layout = QVBoxLayout(self)
		layout.setContentsMargins(24, 16, 24, 16)
		layout.setSpacing(12)
		title = QHBoxLayout()
		title.addWidget(SubtitleLabel('Webcam', self))
		hint = CaptionLabel('Swaps the faces on your webcam live, with the selected source face and the processors and options on the right. FaceFusion runs on its own here, Studio and Batch can still run.', self)
		hint.setTextColor('#707070', '#9a9a9a')
		title.addWidget(elastic(hint), 1)
		# like Batch: always in the accent color, its words say what a click does
		self.settings_button = PrimaryPushButton(FluentIcon.SETTING, 'Show Options', self)
		self.settings_button.setToolTip('Show or hide the processors and options of the webcam on the right')
		self.settings_button.installEventFilter(ToolTipFilter(self.settings_button))
		# as wide as its longer label, so the words changing never moves it
		self.settings_button.setFixedWidth(max(self.settings_button.sizeHint().width(), self.settings_button.fontMetrics().horizontalAdvance('Hide Options') + 56))
		title.addWidget(self.settings_button)
		layout.addLayout(title)

		body = QHBoxLayout()
		body.setSpacing(16)
		layout.addLayout(body, 1)
		left = QVBoxLayout()
		left.setSpacing(12)
		body.addLayout(left, 11)
		self.source_card = SourceCard(self, 'Source faces', 'Drop face images here or click to browse\nThe selected faces go on every face on the webcam')
		copy_sources = TransparentPushButton(FluentIcon.COPY, 'Copy Faces from Studio', self.source_card)
		copy_sources.setToolTip('Add the sources from the Studio page, with their names and which ones are used')
		copy_sources.installEventFilter(ToolTipFilter(copy_sources))
		copy_sources.clicked.connect(self.copy_studio_sources)
		self.source_card.add_tool(copy_sources)
		left.addWidget(self.source_card)

		view_card = SimpleCardWidget(self)
		view_layout = QVBoxLayout(view_card)
		view_layout.setContentsMargins(16, 12, 16, 14)
		view_layout.setSpacing(10)
		controls = QHBoxLayout()
		controls.addWidget(BodyLabel('Camera', view_card))
		self.camera_box = ComboBox(view_card)
		self.camera_box.setMinimumWidth(220)
		controls.addWidget(self.camera_box, 1)
		controls.addSpacing(8)
		controls.addWidget(BodyLabel('Size', view_card))
		self.size_box = ComboBox(view_card)
		for size in FRAME_SIZES:
			self.size_box.addItem('%d px' % size, userData = size)
		self.size_box.setCurrentIndex(FRAME_SIZES.index(stored.get('size')) if stored.get('size') in FRAME_SIZES else FRAME_SIZES.index(FRAME_SIZE_DEFAULT))
		self.size_box.setToolTip('The longest side of the frames FaceFusion gets. Smaller frames come back sooner.')
		self.size_box.installEventFilter(ToolTipFilter(self.size_box))
		self.size_box.currentIndexChanged.connect(lambda _: self.store_page())
		controls.addWidget(self.size_box)
		controls.addSpacing(8)
		self.virtual_box = CheckBox('Send to virtual camera', view_card)
		self.virtual_box.setToolTip('Also send the swapped picture to a virtual camera, so Zoom, Discord, Teams or OBS can use it as a camera.\nNeeds pyvirtualcam and the OBS Studio virtual camera.')
		self.virtual_box.installEventFilter(ToolTipFilter(self.virtual_box))
		self.virtual_box.setChecked(bool(stored.get('virtual_camera')) and is_installed())
		self.virtual_box.toggled.connect(self.on_virtual_toggled)
		controls.addWidget(self.virtual_box)
		view_layout.addLayout(controls)
		self.view = FrameView(view_card)
		view_layout.addWidget(self.view, 1)
		left.addWidget(view_card, 1)

		run_card = SimpleCardWidget(self)
		run_layout = QHBoxLayout(run_card)
		run_layout.setContentsMargins(16, 12, 16, 12)
		# off each time StudioFF starts, so the camera never turns on before the user asks for it
		self.camera_button = TogglePushButton(FluentIcon.CAMERA, 'Enable Camera', run_card)
		self.camera_button.setToolTip('Turn the webcam on to see it here. FaceFusion starts and loads its models too, so Swap Face starts at once.\nTurning it off also ends the swap and lets FaceFusion go.')
		self.camera_button.installEventFilter(ToolTipFilter(self.camera_button))
		self.camera_button.toggled.connect(self.on_camera_toggled)
		run_layout.addWidget(self.camera_button)
		# one button turns the swap on and off, the camera's own picture shows while it is off
		self.swap_button = TogglePushButton(FluentIcon.PEOPLE, 'Swap Face', run_card)
		self.swap_button.setToolTip('Put the source face on the webcam. Press again to see the camera as it is.')
		self.swap_button.installEventFilter(ToolTipFilter(self.swap_button))
		self.swap_button.toggled.connect(self.on_swap_toggled)
		run_layout.addWidget(self.swap_button)
		run_layout.addSpacing(10)
		self.status_label = elastic(BodyLabel('Swap off', run_card))
		run_layout.addWidget(self.status_label, 1)
		left.addWidget(run_card)

		self.settings_column = SettingsColumn(self.store, self, (), expansion_key = 'webcam_groups_expanded', preset_key = 'webcam_preset', order_key = 'webcam_group_order')
		body.addWidget(self.settings_column, 9)
		self.show_settings(session.config.get('webcam_settings_open') is not False)
		self.settings_button.clicked.connect(lambda: self.toggle_settings(not self.settings_column.isVisibleTo(self)))

		self.server.ready.connect(self.on_server_ready)
		self.server.failed.connect(self.on_failed)
		self.client.opened.connect(self.on_stream_opened)
		self.client.frame_received.connect(self.on_swapped_frame)
		self.client.failed.connect(self.on_failed)
		self.client.refused.connect(self.on_refused)
		self.client.closed.connect(self.on_stream_closed)
		self.store.value_changed.connect(self.on_value_changed)
		self.store.schema_changed.connect(self.refresh_locks)
		# listing the cameras makes windows open each one for a moment to read its sizes, and say Python is using the
		# camera, so the list waits for the first Enable Camera. until then the box shows the camera picked last time
		self.media_devices : Optional[QMediaDevices] = None
		self.camera_box.addItem(stored.get('camera_name') or 'Default camera', userData = stored.get('camera') or '')
		self.camera_box.setToolTip('The camera list is read when you press Enable Camera')
		self.camera_box.installEventFilter(ToolTipFilter(self.camera_box))
		self.camera_box.setEnabled(False)
		self.camera_box.currentIndexChanged.connect(lambda _: self.on_camera_changed())
		self.refresh_buttons()
		self.source_card.refresh(self.source_paths)
		self.refresh_locks()

	# settings

	def store_page(self) -> None:
		stored = self.session.config.get('webcam') or {}
		# with no camera found (unplugged for now) the one picked before stays saved
		camera = { 'camera': self.camera_id(), 'camera_name': self.camera_box.currentText() } if self.camera_id() else { 'camera': stored.get('camera') or '', 'camera_name': stored.get('camera_name') or '' }
		self.session.config.set('webcam', { **camera, 'size': self.size_box.currentData(), 'virtual_camera': self.virtual_box.isChecked() })

	def show_settings(self, shown : bool) -> None:
		self.settings_column.setVisible(shown)
		self.settings_button.setText('Hide Options' if shown else 'Show Options')

	def toggle_settings(self, shown : bool) -> None:
		self.show_settings(shown)
		self.session.config.set('webcam_settings_open', shown)

	def refresh_locks(self) -> None:
		if self.store.schema:
			self.settings_column.set_locks(webcam_locks(self.store.schema, self.store.values))

	def run_values(self) -> Dict[str, Any]:
		"""the page's own options, with the system settings from Setup"""
		schema = self.session.schema
		values = dict(self.session.values)
		values.update({ dest: value for dest, value in self.store.values.items() if not schema or schema.options.get(dest, {}).get('group') not in SYSTEM_GROUPS })
		return values

	def on_value_changed(self, dest : str, value : Any) -> None:
		if dest == 'face_selector_mode' and value not in WEBCAM_SELECTOR_MODES:
			# reference mode needs a target file to take the face from
			self.store.set_value('face_selector_mode', WEBCAM_SELECTOR_MODES[0])
			self.session.notify.emit('info', 'Many mode', NO_REFERENCE_REASON + ', so the webcam uses Many or One.')
			return
		if dest in LOCK_DRIVERS or dest.endswith('_model'):
			self.refresh_locks()
		if self.streaming or self.starting:
			self.changed_values[dest] = value
			if dest == 'processors' or dest.endswith('_model'):
				# a model brings its own choices, the values that depend on it go along
				self.changed_values.update(self.session.option_values(self.run_values()))
			self.state_timer.start()

	def send_changed_values(self) -> None:
		if not self.client.access_token:
			return
		values = { dest: value for dest, value in self.changed_values.items() if value is not None and value != '' }
		self.changed_values = {}
		if self.preloading and any(dest == 'processors' or dest.endswith('_model') for dest in values):
			# the new model loads with the next frame, one goes out for it
			self.warm_needed = True
			self.models_ready = False
			if self.streaming:
				self.set_status('Loading the models…')
		if self.sources_changed:
			self.sources_changed = False
			self.client.set_sources(self.upload_sources(), lambda: self.client.set_state(values))
		elif values:
			self.client.set_state(values)

	def on_sources_changed(self, change : str) -> None:
		if change not in [ 'list', 'selection' ]:
			return
		if self.preloading and not self.upload_sources():
			# nothing to swap with any more, facefusion waits for the next face
			self.end_stream()
			self.set_status('Swap off')
		elif self.streaming or self.starting:
			self.sources_changed = True
			self.state_timer.start()
		else:
			# the first face added while the camera is on
			self.preload()

	def find_source_faces(self, paths : List[str]) -> None:
		# a photo of several people stays one source, facefusion takes its largest face
		pass

	def last_dir(self) -> str:
		return next((browse_dir(path) for path in self.source_paths if browse_dir(path)), '')

	def upload_sources(self) -> List[str]:
		return [ upload_ready_path(path, UPLOAD_DIR) for path in self.active_sources() if media_kind(path) == 'image' ]

	# camera

	def list_cameras(self) -> None:
		"""reads the cameras the first time they are needed, and from then on follows cameras being plugged in or out"""
		if self.media_devices is not None:
			return
		self.camera_box.setToolTip('')
		self.media_devices = QMediaDevices(self)
		self.media_devices.videoInputsChanged.connect(self.refresh_cameras)
		self.refresh_cameras()
		# the camera's name is kept for the box at the next start, an unplugged camera stays the saved one
		if self.camera_id():
			self.store_page()

	def refresh_cameras(self, wanted : str = '') -> None:
		wanted = wanted or self.camera_id()
		self.camera_box.blockSignals(True)
		self.camera_box.clear()
		for device in QMediaDevices.videoInputs():
			self.camera_box.addItem(device.description(), userData = bytes(device.id().data()).decode(errors = 'replace'))
		ids = [ self.camera_box.itemData(index) for index in range(self.camera_box.count()) ]
		if wanted in ids:
			self.camera_box.setCurrentIndex(ids.index(wanted))
		self.camera_box.blockSignals(False)
		self.camera_box.setEnabled(bool(ids))
		if not ids:
			self.camera_box.addItem('No camera found')
			self.close_camera()
			self.view.set_text('No camera found')
		elif self.camera is None:
			self.sync_camera()
		self.refresh_buttons()

	def camera_id(self) -> str:
		return self.camera_box.currentData() or ''

	def camera_device(self) -> Any:
		return next((device for device in QMediaDevices.videoInputs() if bytes(device.id().data()).decode(errors = 'replace') == self.camera_id()), QMediaDevices.defaultVideoInput())

	def on_camera_changed(self) -> None:
		self.store_page()
		if self.camera is not None:
			self.open_camera()

	def on_camera_toggled(self, checked : bool) -> None:
		if checked:
			self.list_cameras()
			if not self.camera_id():
				# no camera after all, the button goes back off
				self.camera_button.setChecked(False)
				return
		if not checked and (self.streaming or self.starting):
			self.end_stream()
			self.set_status('Swap off')
		self.refresh_buttons()
		self.sync_camera()
		if checked:
			self.preload()
		else:
			self.view.set_text(CAMERA_OFF_TEXT if self.camera_id() else 'No camera found')

	def wants_camera(self) -> bool:
		"""the camera runs while Enable Camera is on and the page shows, or its picture goes somewhere else: the swap or the virtual camera"""
		return self.camera_button.isChecked() and bool(self.camera_id()) and (self.isVisible() or self.is_swapping() or self.virtual_camera.running)

	def is_swapping(self) -> bool:
		"""Swap Face is on, a preloading stream does not count"""
		return (self.streaming or self.starting) and not self.preloading

	def is_live(self) -> bool:
		return self.streaming and not self.preloading

	def sync_camera(self) -> None:
		if self.wants_camera():
			if self.camera is None:
				self.open_camera()
		else:
			self.close_camera()

	def showEvent(self, event) -> None:
		super().showEvent(event)
		if self.virtual_box.isChecked() and not self.virtual_camera.running and is_installed():
			# checked last time, the virtual camera comes back the first time the page shows
			self.virtual_camera.start()
		self.sync_camera()

	def hideEvent(self, event) -> None:
		super().hideEvent(event)
		self.sync_camera()

	def open_camera(self) -> None:
		self.close_camera()
		self.view.set_text('Starting the camera…')
		self.camera = QCamera(self.camera_device(), self)
		self.camera.errorOccurred.connect(self.on_camera_error)
		self.capture.setCamera(self.camera)
		self.camera.start()

	def on_camera_error(self, _ : Any, text : str) -> None:
		# the swap ends while the failed camera is still set, so ending it does not open the camera again
		if self.is_swapping():
			self.on_failed('The camera did not start: ' + text)
		self.close_camera()
		self.view.set_text('The camera did not start: ' + text)

	def close_camera(self) -> None:
		if self.camera is not None:
			self.camera.stop()
			self.capture.setCamera(None)
			self.camera.deleteLater()
			self.camera = None
		self.latest_image = None

	def on_camera_frame(self, frame : QVideoFrame) -> None:
		if not frame.isValid():
			return
		image = frame.toImage()
		if image.isNull():
			return
		self.latest_image = image
		self.latest_is_new = True
		if not self.is_live() or not self.swapped_count:
			# the camera's own picture while the swap is off, and until the first swapped frame comes back
			self.view.set_image(image)
			if not self.is_live() and self.virtual_camera.running:
				self.virtual_camera.send(bgr_array(scaled_frame(image, self.frame_size())))
			self.check_virtual_camera()
		self.send_latest()

	def frame_size(self) -> int:
		return int(self.size_box.currentData() or FRAME_SIZE_DEFAULT)

	def send_latest(self) -> None:
		if self.preloading and not self.warm_needed:
			return
		if self.latest_image is not None and self.latest_is_new and self.client.wants_frame():
			self.latest_is_new = False
			self.warm_needed = False
			self.client.send_frame(encode_frame(self.latest_image, self.frame_size()))

	# running

	def on_swap_toggled(self, checked : bool) -> None:
		if checked:
			self.start()
		else:
			self.stop()

	def set_swap_button(self, checked : bool) -> None:
		self.swap_button.blockSignals(True)
		self.swap_button.setChecked(checked)
		self.swap_button.blockSignals(False)
		self.view.set_border(checked)

	def start(self) -> None:
		if self.preloading:
			self.show_swap()
			return
		if self.streaming or self.starting:
			return
		if not self.session.is_configured():
			self.set_swap_button(False)
			self.session.notify.emit('error', 'FaceFusion not set up', 'Choose the FaceFusion folder and Python environment on the FaceFusion page.')
			return
		if not self.upload_sources():
			self.set_swap_button(False)
			self.session.notify.emit('warning', 'No source face', 'Add a face image to put on the webcam, and select it.')
			return
		self.set_swap_button(True)
		self.begin()

	def preload(self) -> None:
		"""Enable Camera gets facefusion going and its models loaded, quietly: without facefusion or a face it waits for Swap Face"""
		if self.streaming or self.starting or not self.camera_button.isChecked() or not self.session.is_configured() or not self.upload_sources():
			return
		self.preloading = True
		self.begin()

	def show_swap(self) -> None:
		"""Swap Face on a preloaded stream: the frames sent from now on come back swapped and show"""
		self.preloading = False
		self.warm_needed = False
		self.set_swap_button(True)
		self.swapped_count = 0
		self.frame_times.clear()
		self.last_answer = 0.0
		self.refresh_buttons()
		self.sync_camera()
		if self.streaming:
			self.set_status('Swapping…' if self.models_ready else 'Loading the models, the first frame takes a while…')
			self.latest_is_new = self.latest_image is not None
			self.send_latest()

	def begin(self) -> None:
		"""opens the session and the stream, for the swap or to preload"""
		self.session.save()
		self.starting = True
		self.warm_needed = True
		self.models_ready = False
		self.swapped_count = 0
		self.frame_times.clear()
		self.changed_values = {}
		self.sources_changed = False
		self.idle_timer.stop()
		self.refresh_buttons()
		self.sync_camera()
		if self.server.is_ready:
			self.on_server_ready(self.server.base_url)
		else:
			self.set_status('Starting FaceFusion…')
			if not self.server.is_running():
				self.server.start(self.session.python_path, self.session.facefusion_root)

	def on_server_ready(self, base_url : str) -> None:
		if not self.starting:
			return
		self.set_status('Handing FaceFusion the source faces and options…')
		self.client.open(base_url, self.server.api_key, self.upload_sources(), self.session.option_values(self.run_values()))

	def on_stream_opened(self) -> None:
		self.starting = False
		self.streaming = True
		self.refresh_buttons()
		self.set_status('Loading the models…' if self.preloading else 'Loading the models, the first frame takes a while…')
		self.latest_is_new = self.latest_image is not None
		self.send_latest()

	def on_swapped_frame(self, data : bytes, elapsed : float) -> None:
		image = QImage.fromData(data)
		if image.isNull() or not self.streaming:
			return
		if self.preloading:
			# the models are loaded, the frame itself is not shown
			self.models_ready = True
			if not self.warm_needed:
				self.set_status(READY_TEXT)
			self.send_latest()
			return
		self.models_ready = True
		self.swapped_count += 1
		now = time.monotonic()
		if self.last_answer and self.swapped_count > 1:
			self.frame_times.append(now - self.last_answer)
		self.last_answer = now
		self.view.set_image(image)
		if self.virtual_camera.running:
			frame = cv2.imdecode(numpy.frombuffer(data, numpy.uint8), cv2.IMREAD_COLOR)
			if frame is not None:
				self.virtual_camera.send(frame)
		self.check_virtual_camera()
		self.show_rate(elapsed)
		self.send_latest()

	def show_rate(self, elapsed : float) -> None:
		if self.frame_times:
			fps = len(self.frame_times) / sum(self.frame_times)
			self.set_status('Live · %.1f fps · %s a frame' % (fps, format_seconds(elapsed)) + (' · virtual camera on' if self.virtual_camera.running else ''))
		else:
			self.set_status('Live · first frame took %s' % format_seconds(elapsed))

	def stop(self) -> None:
		"""Swap Face off: while the camera is on the stream stays open with the models loaded, so the next swap starts at once"""
		if self.camera_button.isChecked() and (self.streaming or self.starting):
			self.preloading = True
			self.warm_needed = not self.models_ready
			self.set_swap_button(False)
			self.swapped_count = 0
			self.refresh_buttons()
			self.sync_camera()
			if self.streaming:
				self.set_status(READY_TEXT if self.models_ready else 'Loading the models…')
			return
		self.end_stream()
		self.set_status('Swap off')

	def end_stream(self) -> None:
		was_live = self.streaming or self.starting
		self.starting = False
		self.streaming = False
		self.preloading = False
		self.warm_needed = False
		self.models_ready = False
		self.state_timer.stop()
		self.client.close()
		self.set_swap_button(False)
		# the camera's own picture again, its next frame replaces the last swapped one
		self.swapped_count = 0
		self.refresh_buttons()
		self.sync_camera()
		if was_live and self.server.is_running():
			self.idle_timer.start()

	def on_stream_closed(self) -> None:
		if self.streaming:
			self.end_stream()
			self.set_status('FaceFusion closed the stream')

	def on_failed(self, text : str) -> None:
		# a failed preload stays quiet, the next Swap Face starts over and shows its own failure
		quiet = self.preloading
		self.end_stream()
		self.set_status('Failed: ' + text.splitlines()[0])
		if not quiet:
			self.session.notify.emit('error', 'Webcam stopped', text)

	def on_refused(self, keys : List[str]) -> None:
		schema = self.store.schema
		names = [ schema.option_label(schema.options.get(key)) if schema and key in schema.options else key for key in keys ]
		self.session.notify.emit('warning', 'Some options were not used', "FaceFusion's API did not take %s, it keeps its own value." % ', '.join(names))

	def refresh_buttons(self) -> None:
		# before the list is read the box holds only the last camera, Enable Camera reads the list
		self.camera_button.setEnabled(bool(self.camera_id()) or self.media_devices is None)
		self.swap_button.setEnabled(bool(self.camera_id()) and self.camera_button.isChecked())

	def set_status(self, text : str) -> None:
		self.status_label.setText(text)

	# virtual camera

	def on_virtual_toggled(self, checked : bool) -> None:
		self.store_page()
		if not checked:
			self.virtual_camera.stop()
			self.sync_camera()
			return
		if not is_installed():
			box = MessageBox(INSTALL_TITLE, INSTALL_TEXT, self.window())
			box.yesButton.setText('Install')
			box.cancelButton.setText('Cancel')
			if not box.exec():
				self.virtual_box.setChecked(False)
				return
			self.virtual_box.setEnabled(False)
			self.set_status('Installing pyvirtualcam…')
			self.installer.start()
			return
		self.start_virtual_camera()

	def start_virtual_camera(self) -> None:
		"""the virtual camera gets the swapped picture while the swap is on, and the camera's own picture while it is off"""
		self.virtual_camera.start()
		self.sync_camera()

	def check_virtual_camera(self) -> None:
		if self.virtual_box.isChecked() and not self.virtual_camera.running and self.virtual_camera.error:
			error, self.virtual_camera.error = self.virtual_camera.error, ''
			self.virtual_box.setChecked(False)
			self.session.notify.emit('error', 'Virtual camera', error)

	def on_installed(self, installed : bool, error : str) -> None:
		self.virtual_box.setEnabled(True)
		if installed:
			self.session.notify.emit('success', 'pyvirtualcam installed', 'Start the OBS virtual camera once if you have not, then pick "OBS Virtual Camera" in the other app.')
			self.set_status('Live' if self.is_live() else READY_TEXT if self.preloading and self.models_ready else 'Swap off')
			self.start_virtual_camera()
		else:
			self.virtual_box.setChecked(False)
			self.set_status('Live' if self.is_live() else READY_TEXT if self.preloading and self.models_ready else 'Swap off')
			self.session.notify.emit('error', 'pyvirtualcam did not install', error)

	def shutdown(self) -> None:
		"""closing StudioFF: the stream, the camera and facefusion's api go"""
		self.end_stream()
		self.virtual_camera.stop()
		self.close_camera()
		self.idle_timer.stop()
		self.server.stop()
