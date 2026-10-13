import asyncio
import threading
import time
from dataclasses import dataclass

from starlette.responses import JSONResponse

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
        if scope["path"] != "/healthz" and not self.limiter.take(("http", address), self.settings.http_rate):
            await respond("RATE_LIMITED", "Request rate limit exceeded", 429)
            return
        if scope["path"] == "/v1/pair/claim" and not self.limiter.take(("claim", address), self.settings.claim_rate):
            await respond("RATE_LIMITED", "Pairing attempt rate limit exceeded", 429)
            return
        lengths = [value for key, value in scope["headers"] if key == b"content-length"]
        if len(lengths) > 1:
            await respond("VALIDATION_ERROR", "Invalid Content-Length", 400)
            return
        if lengths:
            try:
                if not lengths[0].isdigit():
                    raise ValueError
                length = int(lengths[0])
                if length > self.settings.max_body_bytes:
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
            if len(body) > self.settings.max_body_bytes:
                await respond("TOO_LARGE", "Request body limit exceeded", 413)
                return
            if not message.get("more_body", False):
                break
        if lengths and int(lengths[0]) != len(body):
            await respond("VALIDATION_ERROR", "Content-Length does not match body", 400)
            return
        if body:
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

    async def close(self, code=1008):
        self.closed = True
        async with self.lock:
            try:
                await asyncio.wait_for(self.websocket.close(code=code), self.settings.send_timeout)
            except (RuntimeError, TimeoutError, OSError):
                pass

    def enqueue(self, value):
        try:
            self.queue.put_nowait(value)
        except asyncio.QueueFull as error:
            raise BridgeError("QUEUE_FULL", "Outgoing frame queue limit reached", 429) from error


class Hub:
    def __init__(self, settings):
        self.settings = settings
        self.peers = {}
        self.lock = asyncio.Lock()

    async def add(self, device_id, peer):
        async with self.lock:
            old = self.peers.get(device_id)
            if old is None and len(self.peers) >= self.settings.max_connections:
                raise BridgeError("QUEUE_FULL", "Connection limit reached", 429)
            self.peers[device_id] = peer
        if old is not None:
            await old.close(1012)

    async def remove(self, device_id, peer):
        async with self.lock:
            if self.peers.get(device_id) is peer:
                self.peers.pop(device_id)

    async def revoke(self, device_id):
        async with self.lock:
            peer = self.peers.get(device_id)
        if peer:
            await peer.close(1008)

    async def close(self):
        async with self.lock:
            peers = list(self.peers.values())
        await asyncio.gather(*(peer.close(1001) for peer in peers), return_exceptions=True)
