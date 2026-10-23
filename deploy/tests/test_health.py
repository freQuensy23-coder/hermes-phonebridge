from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest


@pytest.fixture
def http_health():
    state = {"status": 200, "body": {"status": "ok", "version": "0.1.0", "revision": "a" * 40}, "redirect": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            if state["redirect"]:
                self.send_header("Location", state["redirect"])
            self.end_headers()
            self.wfile.write(json.dumps(state["body"]).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}/healthz"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_http_read_requires_exact_contract(modules, http_health):
    state, url = http_health
    rollout = modules["rollout"]
    assert rollout.healthy("a" * 40, url, timeout=1) == state["body"]
    state["body"]["version"] = "0.2.0"
    with pytest.raises(RuntimeError, match="health"):
        rollout.healthy("a" * 40, url, timeout=0.05)


def test_real_redirect_is_not_followed_even_to_same_host(modules, http_health):
    state, url = http_health
    state["status"] = 302
    state["redirect"] = url
    with pytest.raises(RuntimeError, match="health"):
        modules["rollout"].healthy("a" * 40, url, timeout=0.05)


@pytest.mark.parametrize("url", ["http://bridge.example", "https://user@bridge.example", "https://u:p@bridge.example", "https://bridge.example?a=1", "https://bridge.example#fragment", "https://bridge.example/path", "https://-bad.example", "https://bad..example", "https://bridge.example:444", "https://127.0.0.1", "https://localhost", "https://bridge.example:wrong"])
def test_origin_rejects_non_origin_or_invalid_dns(modules, url):
    with pytest.raises(RuntimeError, match="origin"):
        modules["rollout"].origin(url)



def test_trickling_network_operation_has_hard_wall_clock_bound(modules, monkeypatch):
    import time

    rollout = modules["rollout"]

    class SlowClient:
        def open(self, request, timeout):
            time.sleep(10)
            pytest.fail("Network alarm did not interrupt operation")

    monkeypatch.setattr(rollout, "opener", lambda: SlowClient())
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="health"):
        rollout.healthy("a" * 40, timeout=0.05)
    assert time.monotonic() - started < 1
