import asyncio
from dataclasses import replace

import pytest

from phonebridge_server.protocol import BridgeError
from phonebridge_server.transport import Peer, RateLimiter, RequestBoundary


pytestmark = pytest.mark.asyncio


class Socket:
    def __init__(self):
        self.frames = []
        self.closes = []

    async def send_text(self, value):
        self.frames.append(value)

    async def close(self, code):
        self.closes.append(code)


async def invoke(boundary, messages, headers=None, path="/v1/pairings"):
    sent = []
    queue = asyncio.Queue()
    for message in messages:
        queue.put_nowait(message)
    scope = {"type": "http", "path": path, "headers": headers or [], "client": ("127.0.0.1", 12345)}

    async def receive():
        return await queue.get()

    async def send(value):
        sent.append(value)

    await boundary(scope, receive, send)
    return sent


async def test_chunked_body_has_enforced_limit(settings):
    called = []

    async def app(scope, receive, send):
        called.append(scope)

    boundary = RequestBoundary(app, settings, RateLimiter(4))
    result = await invoke(boundary, [{"type": "http.request", "body": b"x" * 40000, "more_body": True},
                                     {"type": "http.request", "body": b"x" * 40000, "more_body": False}])
    assert result[0]["status"] == 413
    assert called == []


async def test_body_deadline(settings):
    async def app(scope, receive, send):
        raise AssertionError("Application must not receive incomplete bodies")

    boundary = RequestBoundary(app, replace(settings, request_timeout=0.01), RateLimiter(4))
    result = await invoke(boundary, [{"type": "http.request", "body": b"{", "more_body": True}])
    assert result[0]["status"] == 408


async def test_concurrency_gate(settings):
    started, release = asyncio.Event(), asyncio.Event()

    async def app(scope, receive, send):
        started.set()
        await release.wait()

    boundary = RequestBoundary(app, replace(settings, max_http_requests=1), RateLimiter(4))
    first = asyncio.create_task(invoke(boundary, [{"type": "http.request", "body": b""}]))
    await asyncio.wait_for(started.wait(), 1)
    second = await invoke(boundary, [{"type": "http.request", "body": b""}])
    assert second[0]["status"] == 429
    release.set()
    await first
    assert boundary.active_http == 0


async def test_peer_has_bounded_queue_and_frame_size(settings):
    socket = Socket()
    peer = Peer.create(socket, replace(settings, outbound_queue_size=1, max_frame_bytes=100))
    peer.enqueue({"type": "ack", "id": "a"})
    with pytest.raises(BridgeError) as full:
        peer.enqueue({"type": "ack", "id": "b"})
    assert full.value.code == "QUEUE_FULL"
    with pytest.raises(BridgeError) as large:
        await peer.send({"text": "x" * 200})
    assert large.value.code == "TOO_LARGE"
    await peer.close()
    with pytest.raises(BridgeError):
        await peer.send({"type": "ack", "id": "a"})
    assert socket.frames == []
    assert socket.closes == [1008]


async def test_slow_send_has_timeout(settings):
    class SlowSocket(Socket):
        async def send_text(self, value):
            await asyncio.Event().wait()

    peer = Peer.create(SlowSocket(), replace(settings, send_timeout=0.01))
    with pytest.raises(TimeoutError):
        await peer.send({"type": "ack", "id": "a"})


@pytest.mark.parametrize("authorization", [[], [(b"authorization", b"Bearer wrong")],
    [(b"authorization", b"Bearer wrong"), (b"authorization", b"Bearer wrong")]])
async def test_unauthorized_upload_rejected_before_reading_any_body(settings, authorization):
    async def app(scope, receive, send):
        raise AssertionError("Unauthorized upload reached the application")

    async def receive():
        raise AssertionError("Unauthorized upload body must not be read")

    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/v1/devices/example/files",
             "headers": authorization + [(b"content-type", b"application/octet-stream"),
                                           (b"content-length", str(settings.max_file_bytes).encode())],
             "client": ("127.0.0.1", 12345)}
    await RequestBoundary(app, settings, RateLimiter(4))(scope, receive, send)
    assert sent[0]["status"] == 401
    assert (b"www-authenticate", b"Bearer") in sent[0]["headers"]
