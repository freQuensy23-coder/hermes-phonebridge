import asyncio
import re
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from starlette.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from .protocol import BridgeError, decode_json, encode_json


class RateLimiter:
    def __init__(self, max_keys, clock=time.monotonic):
        self.max_keys = max_keys
        self.clock = clock
        self.entries = {}
        self.lock = threading.Lock()

    def take(self, key, capacity):
        now = self.clock()
        with self.lock:
            previous = self.entries.get(key)
            if previous is None:
                if len(self.entries) >= self.max_keys:
                    self.entries = {name: value for name, value in self.entries.items() if now - value[1] < 120}
                    if len(self.entries) >= self.max_keys:
                        return False
                tokens = float(capacity)
            else:
                tokens = min(float(capacity), previous[0] + (now - previous[1]) * capacity / 60)
            allowed = tokens >= 1
            self.entries[key] = (tokens - 1 if allowed else tokens, now)
            return allowed


UPLOAD_PATH = re.compile(r"^/v1/devices/[^/]+/files$")


def public_path(path):
    return path == "/v1/pair/claim" or path.startswith("/v1/device/files/")


class RequestBoundary:
    def __init__(self, app, settings, limiter):
        self.app = app
        self.settings = settings
        self.limiter = limiter
        self.active_http = 0
        self.active_websockets = 0
        self.active_lock = threading.Lock()

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        attribute = "active_http" if kind == "http" else "active_websockets"
        limit = self.settings.max_http_requests if kind == "http" else self.settings.max_connections + 16
        with self.active_lock:
            current = getattr(self, attribute)
            allowed = current < limit
            if allowed:
                setattr(self, attribute, current + 1)
        if not allowed:
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1013})
            else:
                response = JSONResponse({"error": {"code": "QUEUE_FULL", "message": "Request concurrency limit reached"}},
                                        status_code=429, headers={"Retry-After": "1", "Cache-Control": "no-store"})
                await response(scope, receive, send)
            return
        try:
            await self.handle(scope, receive, send)
        finally:
            with self.active_lock:
                setattr(self, attribute, getattr(self, attribute) - 1)

    async def handle(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def respond(code, message, status):
            response = JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                                    headers={"Cache-Control": "no-store", "Retry-After": "60"} if status == 429
                                    else {"Cache-Control": "no-store"})
            await response(scope, receive, send)

        address = scope.get("client")
        address = address[0] if address else "unknown"
        if public_path(scope["path"]) and not self.limiter.take(("http", address), self.settings.http_rate):
            await respond("RATE_LIMITED", "Request rate limit exceeded", 429)
            return
        if scope["path"] == "/v1/pair/claim" and not self.limiter.take(("claim", address), self.settings.claim_rate):
            await respond("RATE_LIMITED", "Pairing attempt rate limit exceeded", 429)
            return
        upload = scope.get("method") == "POST" and UPLOAD_PATH.fullmatch(scope["path"]) is not None
        limit = self.settings.max_file_bytes if upload else self.settings.max_body_bytes
        lengths = [value for key, value in scope["headers"] if key == b"content-length"]
        if len(lengths) > 1:
            await respond("VALIDATION_ERROR", "Invalid Content-Length", 400)
            return
        if upload:
            content_types = [value for key, value in scope["headers"] if key == b"content-type"]
            if len(content_types) != 1 or content_types[0].strip().lower() != b"application/octet-stream":
                await respond("VALIDATION_ERROR", "Content-Type must be application/octet-stream", 415)
                return
            if not lengths:
                await respond("VALIDATION_ERROR", "Content-Length is required", 411)
                return
        if lengths:
            try:
                if not lengths[0].isdigit():
                    raise ValueError
                length = int(lengths[0])
                if length > limit:
                    await respond("TOO_LARGE", "Request body limit exceeded", 413)
                    return
            except ValueError:
                await respond("VALIDATION_ERROR", "Invalid Content-Length", 400)
                return
        body = bytearray()
        deadline = asyncio.get_running_loop().time() + self.settings.request_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                if remaining <= 0:
                    raise TimeoutError
                message = await asyncio.wait_for(receive(), remaining)
            except TimeoutError:
                await respond("TIMEOUT", "Request body timeout", 408)
                return
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                await respond("TOO_LARGE", "Request body limit exceeded", 413)
                return
            if not message.get("more_body", False):
                break
        if lengths and int(lengths[0]) != len(body):
            await respond("VALIDATION_ERROR", "Content-Length does not match body", 400)
            return
        if body and not upload:
            content_types = [value for key, value in scope["headers"] if key == b"content-type"]
            if len(content_types) != 1 or content_types[0].split(b";")[0].strip().lower() != b"application/json":
                await respond("VALIDATION_ERROR", "Content-Type must be application/json", 415)
                return
            try:
                decode_json(bytes(body))
            except (ValueError, UnicodeError, RecursionError):
                await respond("VALIDATION_ERROR", "Invalid or unbounded JSON body", 422)
                return
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")])
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, replay, secure_send)


@dataclass
class Peer:
    websocket: object
    settings: object
    queue: asyncio.Queue
    lock: asyncio.Lock
    closed: bool = False
    close_sent: bool = False
    device_id: str | None = None
    session_id: str | None = None

    @classmethod
    def create(cls, websocket, settings):
        return cls(websocket, settings, asyncio.Queue(settings.outbound_queue_size), asyncio.Lock())

    async def send(self, value):
        async with self.lock:
            if self.closed:
                raise BridgeError("AUTH_REVOKED", "Connection is closed", 401)
            encoded = encode_json(value)
            if len(encoded.encode()) > self.settings.max_frame_bytes:
                raise BridgeError("TOO_LARGE", "Outgoing frame limit exceeded", 413)
            await asyncio.wait_for(self.websocket.send_text(encoded), self.settings.send_timeout)

    async def revoke(self):
        self.closed = True
        async with self.lock:
            if self.close_sent:
                return
            self.close_sent = True
            try:
                await asyncio.wait_for(self.websocket.send_text(encode_json({"type": "revoked"})), self.settings.send_timeout)
                await asyncio.wait_for(self.websocket.close(code=4401), self.settings.send_timeout)
            except (WebSocketDisconnect, RuntimeError, TimeoutError, OSError):
                pass

    async def close(self, code=1008):
        self.closed = True
        async with self.lock:
            if self.close_sent:
                return
            self.close_sent = True
            try:
                await asyncio.wait_for(self.websocket.close(code=code), self.settings.send_timeout)
            except (RuntimeError, TimeoutError, OSError):
                pass

    def enqueue(self, value):
        try:
            self.queue.put_nowait(value)
        except asyncio.QueueFull as error:
            raise BridgeError("QUEUE_FULL", "Outgoing frame queue limit reached", 429) from error


@dataclass
class DeviceConnections:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sessions: dict = field(default_factory=dict)
    current_session: str | None = None
    users: int = 0


class Hub:
    def __init__(self, settings):
        self.settings = settings
        self.peers = {}
        self.devices = {}
        self.lock = asyncio.Lock()
        self.closing = False

    @asynccontextmanager
    async def coordinate(self, device_id):
        async with self.lock:
            state = self.devices.get(device_id)
            if state is None:
                limit = self.settings.max_connections + self.settings.max_http_requests + 16
                if len(self.devices) >= limit:
                    raise BridgeError("QUEUE_FULL", "Connection coordination limit reached", 429)
                state = DeviceConnections()
                self.devices[device_id] = state
            state.users += 1
        try:
            async with state.lock:
                yield state
        finally:
            async with self.lock:
                state.users -= 1
                if not state.users and not state.sessions:
                    self.devices.pop(device_id)

    def _current(self, state, session_id, peer):
        if (self.closing or peer.closed or peer.session_id != session_id
                or state.current_session != session_id
                or state.sessions.get(session_id) is not peer):
            raise BridgeError("AUTH_REVOKED", "Device session is no longer authorized", 401)

    async def connect(self, token, peer, execute):
        device_id = await execute("authorize", token)
        async with self.coordinate(device_id) as state:
            if self.closing:
                raise BridgeError("AUTH_REVOKED", "Connection is closed", 401)
            connecting = asyncio.create_task(execute("connect", token))
            try:
                device_id, session_id = await asyncio.shield(connecting)
            except asyncio.CancelledError:
                device_id, session_id = await connecting
                await execute("disconnect", device_id, session_id)
                raise
            peer.device_id = device_id
            peer.session_id = session_id
            state.current_session = session_id
            state.sessions[session_id] = peer
        return device_id, session_id

    async def add(self, device_id, session_id, peer, execute):
        async with self.coordinate(device_id) as state:
            self._current(state, session_id, peer)
            await execute("check_session", device_id, session_id)
            self._current(state, session_id, peer)
            old = self.peers.get(device_id)
            if old is None and len(self.peers) >= self.settings.max_connections:
                raise BridgeError("QUEUE_FULL", "Connection limit reached", 429)
            self.peers[device_id] = peer
            if old is not None and old is not peer:
                old.closed = True
        if old is not None and old is not peer:
            await old.close(1012)

    async def check_current(self, device_id, session_id, peer, execute):
        async with self.coordinate(device_id) as state:
            self._current(state, session_id, peer)
            await execute("check_session", device_id, session_id)
            self._current(state, session_id, peer)

    async def remove(self, device_id, session_id, peer):
        async with self.coordinate(device_id) as state:
            if state.sessions.get(session_id) is not peer:
                return
            state.sessions.pop(session_id)
            if self.peers.get(device_id) is peer:
                self.peers.pop(device_id)
            if state.current_session == session_id:
                state.current_session = None

    async def revoke(self, device_id, revoke):
        async with self.coordinate(device_id) as state:
            await revoke()
            state.current_session = None
            peers = list(state.sessions.values())
            self.peers.pop(device_id, None)
            for peer in peers:
                peer.closed = True
        await asyncio.gather(*(peer.revoke() for peer in peers))

    async def close(self):
        async with self.lock:
            self.closing = True
            peers = [peer for state in self.devices.values() for peer in state.sessions.values()]
            for peer in peers:
                peer.closed = True
        await asyncio.gather(*(peer.close(1001) for peer in peers), return_exceptions=True)
