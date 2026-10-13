import json
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import pytest

from phonebridge.client import Client, Settings
from phonebridge.errors import BridgeError
from phonebridge.transport import MAX_ADDRESSES


@pytest.mark.parametrize("waiting", [False, True])
def test_dns_timeout_kills_and_reaps_resolver_without_late_request(wire, monkeypatch, waiting):
    processes = []
    popen = subprocess.Popen
    port = urlsplit(wire.url).port
    addresses = json.dumps([[socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ["127.0.0.1", port]]])
    script = f"import time; time.sleep(2); print({addresses!r})"

    def delayed_resolver(arguments, **options):
        assert arguments[-2:] == ["localhost", str(port)]
        process = popen([sys.executable, "-I", "-c", script], **options)
        processes.append(process)
        return process

    monkeypatch.setattr("phonebridge.transport.subprocess.Popen", delayed_resolver)
    client = Client(Settings(wire.url.replace("127.0.0.1", "localhost"), wire.client.settings.token, 0.15))
    started = time.monotonic()
    with pytest.raises(BridgeError) as caught:
        if waiting:
            client.wait("cmd", timeout=0.15)
        else:
            client.submit("dev", "sms.send", {"text": "authorized"}, idempotency_key="original-key")
    assert time.monotonic() - started < 1
    assert caught.value.code == "TRANSPORT_ERROR"
    if waiting:
        assert caught.value.details["command_id"] == "cmd"
    else:
        assert caught.value.details["idempotency_key"] == "original-key"
        assert caught.value.details["may_have_been_submitted"] is True
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed
    assert not wire.requests


def test_resolver_returning_after_deadline_cannot_open_or_post(wire, fake_time, monkeypatch):
    lifecycle = []

    class LateResolver:
        returncode = 0

        def communicate(self, timeout=None):
            lifecycle.append(("communicate", timeout))
            fake_time.elapsed += 0.2
            return json.dumps([[socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                                ["127.0.0.1", urlsplit(wire.url).port]]]).encode(), None

        def poll(self):
            return self.returncode

    monkeypatch.setattr("phonebridge.transport.subprocess.Popen", lambda *args, **kwargs: LateResolver())
    client = Client(Settings(wire.url.replace("127.0.0.1", "localhost"), wire.client.settings.token, 0.1), clock=fake_time.monotonic)
    with pytest.raises(BridgeError) as caught:
        client.submit("dev", "sms.send", {}, idempotency_key="same-key")
    assert caught.value.code == "TRANSPORT_ERROR"
    assert caught.value.details["idempotency_key"] == "same-key"
    assert lifecycle[0][1] == pytest.approx(0.1)
    assert len(lifecycle) == 2
    assert not wire.requests


def test_dns_and_address_attempts_use_one_shrinking_deadline(wire, fake_time, monkeypatch):
    attempts = []
    sockets = []

    def resolve(host, port, deadline):
        fake_time.elapsed += 0.04
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ("127.0.0.1", port))] * 10

    class FailedSocket:
        def __init__(self, *args):
            self.closed = False
            sockets.append(self)

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            attempts.append(self.timeout)
            fake_time.elapsed += min(0.04, self.timeout)
            raise OSError("controlled connection failure")

        def shutdown(self, mode):
            pass

        def close(self):
            self.closed = True

    monkeypatch.setattr("phonebridge.transport.resolve_addresses", resolve)
    monkeypatch.setattr("phonebridge.transport.create_socket", FailedSocket)
    wire.client.clock = fake_time.monotonic
    with pytest.raises(BridgeError) as caught:
        wire.client.request("POST", "/v1/devices/dev/commands", {}, timeout=0.15)
    assert caught.value.code == "TRANSPORT_ERROR"
    assert attempts == pytest.approx([0.11, 0.07, 0.03])
    assert fake_time.elapsed == pytest.approx(0.15)
    assert all(active.closed for active in sockets)
    assert not wire.requests


def test_address_attempt_count_is_bounded(wire, monkeypatch):
    attempts = []
    sockets = []

    class FailedSocket:
        def __init__(self, *args):
            self.closed = False
            sockets.append(self)

        def settimeout(self, timeout):
            pass

        def connect(self, address):
            attempts.append(address)
            raise OSError("unreachable")

        def shutdown(self, mode):
            pass

        def close(self):
            self.closed = True

    monkeypatch.setattr("phonebridge.transport.resolve_addresses", lambda host, port, deadline: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ("127.0.0.1", port))] * 1000)
    monkeypatch.setattr("phonebridge.transport.create_socket", FailedSocket)
    with pytest.raises(BridgeError) as caught:
        wire.client.submit("dev", "sms.send", {}, idempotency_key="unchanged")
    assert caught.value.code == "TRANSPORT_ERROR"
    assert caught.value.details["idempotency_key"] == "unchanged"
    assert len(attempts) == MAX_ADDRESSES
    assert all(active.closed for active in sockets)
    assert not wire.requests


def test_connect_cancel_closes_real_socket_and_never_sends_post(wire, monkeypatch):
    cancelled = threading.Event()
    sockets = []
    socket_type = socket.socket

    class PausedSocket:
        def __init__(self, *args):
            self.raw = socket_type(*args)
            sockets.append(self)

        def settimeout(self, timeout):
            self.raw.settimeout(timeout)

        def connect(self, address):
            self.raw.connect(address)
            assert cancelled.wait(1)
            raise TimeoutError("cancelled before HTTP submission")

        def shutdown(self, mode):
            cancelled.set()
            self.raw.shutdown(mode)

        def close(self):
            self.raw.close()

    monkeypatch.setattr("phonebridge.transport.create_socket", PausedSocket)
    started = time.monotonic()
    with pytest.raises(BridgeError) as caught:
        wire.client.request("POST", "/v1/devices/dev/commands", {}, timeout=0.15)
    assert time.monotonic() - started < 1
    assert caught.value.code == "TRANSPORT_ERROR"
    assert caught.value.details["may_have_been_submitted"] is True
    assert cancelled.is_set()
    assert all(active.raw.fileno() == -1 for active in sockets)
    assert not wire.requests


def test_tls_handshake_is_bounded_on_real_loopback_without_http_submission(wire):
    received = []
    accepted = threading.Event()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)

    def stall_tls():
        with listener.accept()[0] as peer:
            accepted.set()
            peer.settimeout(2)
            while chunk := peer.recv(65536):
                received.append(chunk)

    worker = threading.Thread(target=stall_tls, daemon=True)
    worker.start()
    client = Client(Settings(f"https://127.0.0.1:{listener.getsockname()[1]}", wire.client.settings.token, 0.2))
    try:
        started = time.monotonic()
        with pytest.raises(BridgeError) as caught:
            client.submit("dev", "sms.send", {}, idempotency_key="tls-key")
        assert time.monotonic() - started < 1
        assert caught.value.code == "TRANSPORT_ERROR"
        assert caught.value.details["idempotency_key"] == "tls-key"
        assert accepted.wait(1)
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert received
        assert b"POST " not in b"".join(received)
        assert wire.client.settings.token.encode() not in b"".join(received)
        assert not wire.requests
    finally:
        listener.close()
        worker.join(timeout=3)


def test_real_hostname_resolution_reaches_loopback_once(wire):
    client = Client(Settings(wire.url.replace("127.0.0.1", "localhost"), wire.client.settings.token, 2))
    wire.respond({"devices": []})
    assert client.request("GET", "/v1/devices") == {"devices": []}
    assert [request["method"] for request in wire.requests] == ["GET"]


def test_wait_absolute_deadline_cannot_extend_during_request_setup(wire, fake_time, monkeypatch):
    captured = []
    request = wire.client.request
    wire.client.clock = fake_time.monotonic

    def delayed_request(*args, **kwargs):
        fake_time.elapsed += 0.1
        captured.append(kwargs["deadline_at"])
        return request(*args, **kwargs)

    monkeypatch.setattr(wire.client, "request", delayed_request)
    with pytest.raises(BridgeError) as caught:
        wire.client.wait("cmd", timeout=0.1)
    assert captured == [0.1]
    assert caught.value.code == "TRANSPORT_ERROR"
    assert caught.value.details["command_id"] == "cmd"
    assert not wire.requests


def test_slow_streaming_body_uses_total_deadline_and_stops_watchdog(wire, monkeypatch):
    from phonebridge.transport import Deadline

    deadlines = []

    def record_deadline(*args):
        deadline = Deadline(*args)
        deadlines.append(deadline)
        return deadline

    monkeypatch.setattr("phonebridge.client.Deadline", record_deadline)
    wire.respond({"id": "cmd", "status": "succeeded", "data": {"text": "x" * 8192}}, test_chunk_delay="0.04")
    started = time.monotonic()
    with pytest.raises(BridgeError) as caught:
        wire.client.wait("cmd", timeout=0.15)
    assert time.monotonic() - started < 1
    assert caught.value.code == "TRANSPORT_ERROR"
    assert caught.value.details["command_id"] == "cmd"
    assert [request["method"] for request in wire.requests] == ["GET"]
    assert len(deadlines) == 1
    assert not deadlines[0].timer.is_alive()
    assert deadlines[0].active_socket.fileno() == -1
