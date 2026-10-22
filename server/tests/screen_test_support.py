import base64
from contextlib import ExitStack, contextmanager
from uuid import uuid4

import anyio
from starlette.testclient import TestClient, WebSocketTestSession

from phonebridge_test_support import CAPABILITIES


SCREEN_OPERATIONS = ["screen.tree", "screen.screenshot", "screen.tap", "screen.swipe", "screen.input", "screen.global"]


class WebSocketReceiveTimeout(AssertionError):
    pass


@contextmanager
def _preserve_primary_cleanup(primary, boundary):
    try:
        yield
    except BaseException as cleanup_error:
        if primary is None:
            raise
        primary.add_note(f"{boundary} cleanup also failed: {cleanup_error!r}")


class PrimaryPreservingTestClient(TestClient):
    primary_error = None

    def __exit__(self, exc_type, exc_value, traceback):
        primary = exc_value if exc_value is not None else self.primary_error
        with _preserve_primary_cleanup(primary, "TestClient"):
            super().__exit__(exc_type, exc_value, traceback)
        return False


class DeadlineWebSocketSession(WebSocketTestSession):
    def __init__(self, session, receive_timeout=5):
        super().__init__(session.app, session.scope, session.portal_factory)
        self.receive_timeout = receive_timeout

    def __enter__(self):
        stack = ExitStack()
        try:
            self.portal = portal = stack.enter_context(self.portal_factory())
            future, cancel_scope = portal.start_task(self._run)
            stack.callback(future.result)
            stack.callback(portal.call, cancel_scope.cancel)
            self.send({"type": "websocket.connect"})
            message = self.receive()
            self._raise_on_close(message)
            self.accepted_subprotocol = message.get("subprotocol", None)
            self.extra_headers = message.get("headers", None)
            stack.callback(self.close, 1000)
            self.exit_stack = stack.pop_all()
            return self
        except BaseException as primary:
            with _preserve_primary_cleanup(primary, "WebSocket"):
                stack.__exit__(type(primary), primary, primary.__traceback__)
            raise

    async def _receive_before_deadline(self):
        with anyio.fail_after(self.receive_timeout):
            return await self._send_rx.receive()

    def receive(self):
        try:
            return self.portal.call(self._receive_before_deadline)
        except TimeoutError as error:
            raise WebSocketReceiveTimeout(f"WebSocket receive deadline exceeded ({self.receive_timeout}s)") from error

    def __exit__(self, exc_type, exc_value, traceback):
        with _preserve_primary_cleanup(exc_value, "WebSocket"):
            return super().__exit__(exc_type, exc_value, traceback)
        return False


@contextmanager
def connected(client, claim, capabilities=None):
    session = client.websocket_connect("/v1/device/ws", headers={"Authorization": f"Bearer {claim['device_token']}"})
    with DeadlineWebSocketSession(session) as socket:
        socket.send_json({"type": "hello", "version": "0.1.0",
                          "capabilities": CAPABILITIES if capabilities is None else capabilities,
                          "permissions": {"notifications": True}})
        ready_id = str(uuid4())
        socket.send_json({"type": "event", "id": ready_id, "name": "connection.ready", "data": {}, "time": 0})
        assert socket.receive_json() == {"type": "ack", "id": ready_id}
        yield socket


def jpeg_bytes(size=240 * 1024):
    def segment(marker, payload):
        return b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload

    image = b"\xff\xd8"
    image += segment(0xDB, b"\x00" + b"\x01" * 64)
    image += segment(0xC0, b"\x08\x00\x01\x00\x01\x01\x01\x11\x00")
    counts = b"\x01" + b"\x00" * 15
    image += segment(0xC4, b"\x00" + counts + b"\x00")
    image += segment(0xC4, b"\x10" + counts + b"\x00")
    scan = segment(0xDA, b"\x01\x01\x00\x00\x3f\x00") + b"\x3f"
    padding = size - len(image) - len(scan) - 2
    segments = b""
    while padding:
        length = min(padding, 65537)
        if 0 < padding - length < 4:
            length -= 4
        segments += segment(0xFE, b"x" * (length - 4))
        padding -= length
    return image + segments + scan + b"\xff\xd9"


def screenshot_data(size=240 * 1024):
    return {"mime": "image/jpeg", "base64": base64.b64encode(jpeg_bytes(size)).decode("ascii"),
            "width": 1, "height": 1, "screen_bounds": [0, 0, 1080, 2400],
            "window_bounds": [0, 0, 1080, 2400],
            "package": "org.example.mock", "secure_window_guard": "platform_window_capture"}
