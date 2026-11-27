"""live face swap through facefusion's web api: StreamServer runs `facefusion.py api`, StreamClient opens a session,
hands it the sources and options and sends camera frames over the /stream websocket, one frame at a time"""
import hashlib
import json
import os
import secrets
import socket
import time
from typing import Any, Callable, Dict, List, Optional

import cv2
import numpy
from PySide6.QtCore import QByteArray, QFile, QIODevice, QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QAbstractSocket, QHttpMultiPart, QHttpPart, QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWebSockets import QWebSocket, QWebSocketHandshakeOptions

from ffstudio.environment import create_process_environment
from ffstudio.runner import ProcessRunner

API_HOST = '127.0.0.1'
# the sides of an image the api takes, see facefusion/apis/media_validator.py
API_RESOLUTION_MIN = 64
API_RESOLUTION_MAX = 4096
# facefusion logs this once its api is about to listen, see facefusion/locales.py api_started
API_STARTED_TEXT = 'started API on'
# an access token lasts 10 minutes and the session guard takes it for the first half only
TOKEN_RENEW_SECONDS = 4 * 60
# the api starts listening a moment after it logs that it started
SESSION_ATTEMPTS = 20
SESSION_RETRY_MS = 500


def free_port() -> int:
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
		probe.bind((API_HOST, 0))
		return int(probe.getsockname()[1])


def upload_ready_path(path : str, folder : str) -> str:
	"""the source itself, or a copy scaled down to fit when it is bigger than the api takes (64 to 4096 px a side)"""
	# a video or audio source does not decode and goes as it is
	image = cv2.imdecode(numpy.fromfile(path, numpy.uint8), cv2.IMREAD_COLOR) if os.path.isfile(path) else None
	if image is None or max(image.shape[:2]) <= API_RESOLUTION_MAX:
		return path
	scale = API_RESOLUTION_MAX / max(image.shape[:2])
	scaled = cv2.resize(image, (max(API_RESOLUTION_MIN, int(image.shape[1] * scale)), max(API_RESOLUTION_MIN, int(image.shape[0] * scale))), interpolation = cv2.INTER_AREA)
	os.makedirs(folder, exist_ok = True)
	copy_path = os.path.join(folder, hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:16] + '.png')
	if not os.path.isfile(copy_path) or os.path.getmtime(copy_path) < os.path.getmtime(path):
		cv2.imencode('.png', scaled)[1].tofile(copy_path)
	return copy_path


def allowed_api_keys(capabilities : Dict[str, Any]) -> List[str]:
	"""the option names /state takes, from /capabilities: { 'arguments': { group: { name: {...} } } }"""
	keys : List[str] = []
	for group in (capabilities.get('arguments') or {}).values():
		for name in group:
			if name not in keys:
				keys.append(name)
	return keys


def api_state_values(values : Dict[str, Any], allowed : List[str]) -> Dict[str, Any]:
	"""the values /state takes, json ready: names the api does not know are left out"""
	return { key: list(value) if isinstance(value, tuple) else value for key, value in values.items() if key in allowed }


class FrameGate:
	"""keeps one frame in flight: a frame offered while the last one is still out waits, a newer one replaces it"""

	def __init__(self) -> None:
		self.in_flight = False
		self.waiting : Optional[bytes] = None
		self.sent_at = 0.0

	def offer(self, frame : bytes) -> Optional[bytes]:
		"""the frame to send now, None when it waits for the result of the one in flight"""
		if self.in_flight:
			self.waiting = frame
			return None
		self.in_flight = True
		self.sent_at = time.monotonic()
		return frame

	def answered(self) -> Optional[bytes]:
		"""the result of the frame in flight came back, returns the waiting frame to send next"""
		self.in_flight = False
		frame, self.waiting = self.waiting, None
		return self.offer(frame) if frame is not None else None

	def reset(self) -> None:
		self.in_flight = False
		self.waiting = None


class StreamServer(QObject):
	"""facefusion's api as a process of its own, apart from the session's runner so other pages can still run"""
	ready = Signal(str)
	failed = Signal(str)
	log = Signal(str)

	def __init__(self, parent : Optional[QObject] = None) -> None:
		super().__init__(parent)
		self.runner = ProcessRunner(self)
		self.runner.log.connect(self.on_log)
		self.runner.finished.connect(self.on_finished)
		self.api_key = ''
		self.base_url = ''
		self.is_ready = False
		self.lines : List[str] = []

	def is_running(self) -> bool:
		return self.runner.is_running()

	def start(self, python_path : str, facefusion_root : str) -> bool:
		if self.runner.is_running():
			return True
		self.api_key = secrets.token_urlsafe(24)
		port = free_port()
		self.base_url = 'http://%s:%d' % (API_HOST, port)
		self.is_ready = False
		self.lines = []
		environment = create_process_environment(python_path)
		# only a client that knows the key can open a session
		environment.insert('FACEFUSION_API_KEY', self.api_key)
		args = [ 'facefusion.py', 'api', '--api-host', API_HOST, '--api-port', str(port) ]
		return self.runner.start(python_path, args, facefusion_root, environment)

	def stop(self) -> None:
		self.is_ready = False
		self.runner.stop()

	def on_log(self, line : str) -> None:
		self.lines.append(line)
		del self.lines[:-50]
		self.log.emit(line)
		if not self.is_ready and API_STARTED_TEXT in line:
			self.is_ready = True
			self.ready.emit(self.base_url)

	def on_finished(self, exit_code : int, _ : float) -> None:
		was_ready, self.is_ready = self.is_ready, False
		if exit_code != -1:
			tail = '\n'.join(self.lines[-6:])
			self.failed.emit(('FaceFusion stopped (exit code %d).' % exit_code if was_ready else 'FaceFusion could not start its API (exit code %d).' % exit_code) + ('\n' + tail if tail else ''))


class StreamClient(QObject):
	"""one api session: sources, options and the /stream websocket"""
	# the stream is open, frames can be sent
	opened = Signal()
	# the swapped frame as encoded image bytes, and how long it took since it was sent
	frame_received = Signal(bytes, float)
	failed = Signal(str)
	# option names the api refused
	refused = Signal(list)
	closed = Signal()

	def __init__(self, parent : Optional[QObject] = None) -> None:
		super().__init__(parent)
		self.network = QNetworkAccessManager(self)
		self.socket : Optional[QWebSocket] = None
		self.base_url = ''
		self.api_key = ''
		self.access_token = ''
		self.refresh_token = ''
		self.allowed : List[str] = []
		self.gate = FrameGate()
		self.renew_timer = QTimer(self)
		self.renew_timer.setInterval(TOKEN_RENEW_SECONDS * 1000)
		self.renew_timer.timeout.connect(self.renew_session)
		self.replies : List[QNetworkReply] = []

	# requests

	def request(self, path : str) -> QNetworkRequest:
		request = QNetworkRequest(QUrl(self.base_url + path))
		if self.access_token:
			request.setRawHeader(b'Authorization', ('Bearer ' + self.access_token).encode())
		return request

	def send(self, method : str, path : str, body : Any, then : Callable[[int, Any], None]) -> None:
		"""sends json (or a QHttpMultiPart), then gets the status code and the decoded json answer (None when there is none)"""
		request = self.request(path)
		if isinstance(body, QHttpMultiPart):
			data : Any = body
		else:
			request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, 'application/json')
			data = QByteArray(json.dumps(body).encode()) if body is not None else QByteArray()
		if method == 'GET':
			reply = self.network.get(request)
		elif method == 'POST':
			reply = self.network.post(request, data)
		elif method == 'PUT':
			reply = self.network.put(request, data)
		else:
			reply = self.network.sendCustomRequest(request, method.encode(), data)
		if isinstance(body, QHttpMultiPart):
			body.setParent(reply)
		self.replies.append(reply)
		reply.finished.connect(lambda: self.on_reply(reply, then))

	def on_reply(self, reply : QNetworkReply, then : Callable[[int, Any], None]) -> None:
		if reply in self.replies:
			self.replies.remove(reply)
		status = int(reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0)
		raw = bytes(reply.readAll().data())
		reply.deleteLater()
		try:
			answer = json.loads(raw) if raw else None
		except ValueError:
			answer = None
		then(status, answer)

	# session

	def open(self, base_url : str, api_key : str, source_paths : List[str], values : Dict[str, Any]) -> None:
		self.base_url = base_url
		self.api_key = api_key
		self.access_token = ''
		self.gate.reset()
		self.create_session(source_paths, values, SESSION_ATTEMPTS)

	def create_session(self, source_paths : List[str], values : Dict[str, Any], attempts : int) -> None:
		def on_session(status : int, answer : Any) -> None:
			if status == 201 and answer:
				self.access_token = answer.get('access_token') or ''
				self.refresh_token = answer.get('refresh_token') or ''
				self.renew_timer.start()
				self.send('GET', '/capabilities', None, lambda status, answer: on_capabilities(answer or {}))
			elif status in [ 0, 429 ] and attempts > 1:
				# not listening yet, or the last session is still being ended
				QTimer.singleShot(SESSION_RETRY_MS, lambda: self.create_session(source_paths, values, attempts - 1))
			else:
				self.failed.emit('FaceFusion refused the session (%s).' % (answer or {}).get('message', status or 'no answer'))

		def on_capabilities(capabilities : Dict[str, Any]) -> None:
			self.allowed = allowed_api_keys(capabilities)
			self.set_sources(source_paths, lambda: self.set_state(values, self.open_stream))

		self.send('POST', '/session', { 'api_key': self.api_key }, on_session)

	def renew_session(self) -> None:
		def on_renewed(status : int, answer : Any) -> None:
			if status == 200 and answer:
				self.access_token = answer.get('access_token') or self.access_token
				self.refresh_token = answer.get('refresh_token') or self.refresh_token
		if self.refresh_token:
			self.send('PUT', '/session', { 'refresh_token': self.refresh_token }, on_renewed)

	def set_sources(self, source_paths : List[str], then : Callable[[], None]) -> None:
		parts = QHttpMultiPart(QHttpMultiPart.ContentType.FormDataType)
		for path in source_paths:
			source_file = QFile(path, parts)
			if not source_file.open(QIODevice.OpenModeFlag.ReadOnly):
				continue
			part = QHttpPart()
			part.setHeader(QNetworkRequest.KnownHeaders.ContentDispositionHeader, 'form-data; name="file"; filename="%s"' % os.path.basename(path).replace('"', ''))
			part.setBodyDevice(source_file)
			parts.append(part)

		def on_uploaded(status : int, answer : Any) -> None:
			asset_ids = (answer or {}).get('asset_ids') or []
			if status != 201 or not asset_ids:
				self.failed.emit('FaceFusion could not read the source faces (%s).' % (answer or {}).get('message', status))
				return
			self.send('PUT', '/state?action=select&type=source', { 'asset_ids': asset_ids }, lambda status, answer: then() if status == 200 else self.failed.emit('FaceFusion could not use the source faces.'))

		self.send('POST', '/assets?type=source', parts, on_uploaded)

	def set_state(self, values : Dict[str, Any], then : Optional[Callable[[], None]] = None) -> None:
		"""all values at once, and one at a time when the api refuses the lot, so the refused ones can be named"""
		state = api_state_values(values, self.allowed)

		def on_state(status : int, _ : Any) -> None:
			if status == 400 and len(state) > 1:
				self.set_one_by_one(list(state.items()), [], then)
				return
			if status == 400:
				self.refused.emit(list(state))
			if then:
				then()

		if state:
			self.send('PUT', '/state', state, on_state)
		elif then:
			then()

	def set_one_by_one(self, items : List[Any], refused : List[str], then : Optional[Callable[[], None]]) -> None:
		if not items:
			if refused:
				self.refused.emit(refused)
			if then:
				then()
			return
		key, value = items[0]

		def on_state(status : int, _ : Any) -> None:
			self.set_one_by_one(items[1:], refused + ([ key ] if status == 400 else []), then)

		self.send('PUT', '/state', { key: value }, on_state)

	# stream

	def open_stream(self) -> None:
		self.socket = QWebSocket()
		self.socket.setParent(self)
		self.socket.connected.connect(self.opened.emit)
		self.socket.disconnected.connect(self.on_disconnected)
		self.socket.binaryMessageReceived.connect(self.on_frame)
		options = QWebSocketHandshakeOptions()
		# the session guard reads the token from the subprotocol
		options.setSubprotocols([ 'access_token.' + self.access_token ])
		self.socket.open(QUrl(self.base_url.replace('http://', 'ws://') + '/stream'), options)

	def is_open(self) -> bool:
		return self.socket is not None and self.socket.state() == QAbstractSocket.SocketState.ConnectedState

	def wants_frame(self) -> bool:
		"""whether a frame sent now goes out at once, so the page encodes only the frames that are sent"""
		return self.is_open() and not self.gate.in_flight

	def send_frame(self, frame : bytes) -> None:
		if not self.is_open():
			return
		frame = self.gate.offer(frame)
		if frame is not None:
			self.socket.sendBinaryMessage(QByteArray(frame))

	def on_frame(self, message : QByteArray) -> None:
		elapsed = time.monotonic() - self.gate.sent_at
		self.frame_received.emit(bytes(message.data()), elapsed)
		frame = self.gate.answered()
		if frame is not None and self.is_open():
			self.socket.sendBinaryMessage(QByteArray(frame))

	def on_disconnected(self) -> None:
		self.gate.reset()
		self.closed.emit()

	def close(self) -> None:
		"""closes the stream and ends the session, facefusion removes the session's uploads with it"""
		self.renew_timer.stop()
		self.gate.reset()
		if self.socket is not None:
			socket_, self.socket = self.socket, None
			socket_.disconnected.disconnect(self.on_disconnected)
			socket_.close()
			socket_.deleteLater()
		if self.access_token:
			self.end_session(self.request('/session'), 10)
			self.access_token = ''

	def end_session(self, request : QNetworkRequest, attempts : int) -> None:
		# 409 while the stream that just closed still finishes its frame, the api takes one session at a time
		reply = self.network.sendCustomRequest(request, b'DELETE')

		def on_done() -> None:
			status = int(reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0)
			reply.deleteLater()
			if status == 409 and attempts > 1:
				QTimer.singleShot(SESSION_RETRY_MS, lambda: self.end_session(request, attempts - 1))

		reply.finished.connect(on_done)
