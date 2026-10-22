import json
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from phonebridge.client import Client, Settings


@pytest.fixture
def wire():
    responses = deque()
    requests = []
    recorded = threading.Condition()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            with recorded:
                requests.append({"method": self.command, "path": self.path,
                                 "headers": dict(self.headers), "body": json.loads(body) if body else None, "raw_body": body})
                recorded.notify_all()
            if not responses:
                self.send_response(500)
                self.end_headers()
                return
            status, content, headers = responses.popleft()
            if status == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if "test_delay" in headers:
                time.sleep(float(headers["test_delay"]))
            encoded = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", headers.get("Content-Length", str(len(encoded))))
            for key, value in headers.items():
                if key not in {"Content-Type", "Content-Length", "test_delay", "test_chunk_delay"}:
                    self.send_header(key, value)
            self.end_headers()
            try:
                if "test_chunk_delay" in headers:
                    for offset in range(0, len(encoded), 1024):
                        time.sleep(float(headers["test_chunk_delay"]))
                        self.wfile.write(encoded[offset:offset + 1024])
                else:
                    self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = respond
        do_POST = respond
        do_DELETE = respond

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # serve_forever only notices shutdown() between polls; the 0.5 s default made every
    # teardown sleep half a second.
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    client = Client(Settings(url, "test-admin-credential-00000000000000000000", 2))

    class Wire:
        def __init__(self):
            self.client = client
            self.requests = requests
            self.url = url

        def respond(self, content, status=200, **headers):
            responses.append((status, content, headers))

        def wait_for_requests(self, count, timeout=5):
            # The server thread records a request on its own schedule; a client that gives up
            # before any response arrives can return before that happens on a loaded machine.
            with recorded:
                return recorded.wait_for(lambda: len(requests) >= count, timeout)

    try:
        yield Wire()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.delenv("PHONEBRIDGE_URL", raising=False)
    monkeypatch.delenv("PHONEBRIDGE_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)


class Time:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.elapsed

    def wall(self):
        return 1000.0 + self.elapsed

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds


@pytest.fixture
def fake_time():
    return Time()
