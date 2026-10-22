import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

from phonebridge.cli import main
from phonebridge.client import MAX_RESPONSE


MAX_FILE_SIZE = 32 * 1024 * 1024
ADMIN_TOKEN = "file-test-admin-credential-00000000000000000000"


@pytest.fixture
def file_wire(environment, monkeypatch):
    responses = deque()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            requests.append({"method": self.command, "path": self.path,
                             "headers": {key.lower(): value for key, value in self.headers.items()},
                             "raw_body": body})
            if not responses:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status, content, headers = responses.popleft()
            if status == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if "test_delay" in headers:
                time.sleep(float(headers["test_delay"]))
            encoded = content if isinstance(content, bytes) else json.dumps(
                content, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
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
                        self.wfile.flush()
                else:
                    self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = respond
        do_POST = respond
        do_DELETE = respond

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    class FileWire:
        def __init__(self):
            self.url = f"http://127.0.0.1:{server.server_port}"
            self.requests = requests
            self.token = ADMIN_TOKEN

        def respond(self, content, status=200, **headers):
            responses.append((status, content, headers))

    wire = FileWire()
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.token)
    try:
        yield wire
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def command_resource(status="queued", **extra):
    return {"id": "file-command", "device_id": "device", "status": status,
            "expires_at": time.time() + 300, "data": None, "error": None, **extra}


def local_file(tmp_path, name="payload.bin", content=b"\x00\xff\r\n{not-json}\x80"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def image_bytes(format):
    stream = io.BytesIO()
    Image.new("RGB", (2, 3), "white").save(stream, format=format)
    return stream.getvalue()


def read_success(capsys, expected):
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out) == expected
    assert ADMIN_TOKEN not in output.out
    return output


def read_error(capsys, codes):
    output = capsys.readouterr()
    assert not output.out
    error = json.loads(output.err)["error"]
    assert error["code"] in codes
    assert ADMIN_TOKEN not in output.err
    assert "Traceback" not in output.err
    return error


def upload_query(request):
    return parse_qs(urlsplit(request["path"]).query, keep_blank_values=True)


def test_files_send_uploads_raw_bytes_and_returns_only_queued_command(file_wire, tmp_path, capsys):
    parent = tmp_path / "host-private-directory"
    parent.mkdir()
    path = local_file(parent, "report.txt", b"\x00\xffprivate bytes\r\n")
    queued = command_resource()
    file_wire.respond(queued, status=202)
    assert main(["files", "send", "device/exact?x", str(path)]) == 0
    read_success(capsys, queued)
    assert len(file_wire.requests) == 1
    request = file_wire.requests[0]
    assert request["method"] == "POST"
    assert urlsplit(request["path"]).path == "/v1/devices/device%2Fexact%3Fx/files"
    assert upload_query(request) == {"name": ["report.txt"], "mime": ["text/plain"],
                                     "kind": ["file"], "ttl_seconds": ["300"]}
    assert request["raw_body"] == path.read_bytes()
    headers = request["headers"]
    assert headers["authorization"] == f"Bearer {file_wire.token}"
    assert headers["content-type"] == "application/octet-stream"
    assert headers["content-length"] == str(path.stat().st_size)
    assert headers["x-content-sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "transfer-encoding" not in headers
    key = headers["idempotency-key"]
    assert str(uuid.UUID(key)) == key
    assert file_wire.token not in request["path"]
    assert str(parent) not in request["path"]
    assert "host-private-directory" not in json.dumps(headers)
    assert queued["status"] == "queued"
    assert queued["data"] is None


def test_files_send_preserves_explicit_name_mime_ttl_and_key(file_wire, tmp_path, capsys):
    path = local_file(tmp_path)
    resource = command_resource()
    file_wire.respond(resource, status=202)
    name = "Отчёт & צילום + 2026.bin"
    key = "Exact-Key-001"
    assert main(["files", "send", "device", str(path), "--name", name,
                 "--mime", "application/x-phonebridge-test", "--ttl", "900",
                 "--idempotency-key", key]) == 0
    read_success(capsys, resource)
    assert len(file_wire.requests) == 1
    request = file_wire.requests[0]
    assert upload_query(request) == {"name": [name], "mime": ["application/x-phonebridge-test"],
                                     "kind": ["file"], "ttl_seconds": ["900"]}
    assert request["headers"]["idempotency-key"] == key
    assert request["raw_body"] == path.read_bytes()


def test_upload_preserves_configured_base_path_and_literal_device_identifier(file_wire, tmp_path, capsys):
    path = local_file(tmp_path)
    resource = command_resource()
    file_wire.respond(resource, status=202)
    assert main(["--url", file_wire.url + "/bridge", "files", "send", "literal%2Fdevice", str(path)]) == 0
    read_success(capsys, resource)
    assert len(file_wire.requests) == 1
    assert urlsplit(file_wire.requests[0]["path"]).path == "/bridge/v1/devices/literal%252Fdevice/files"


@pytest.mark.parametrize("name,mime", [("notes.txt", "text/plain"), ("data.json", "application/json"),
                                      ("photo.png", "image/png"), ("unknown.phonebridgeunknown", "application/octet-stream")])
def test_generic_file_infers_mime_without_promoting_to_image(file_wire, tmp_path, capsys, name, mime):
    path = local_file(tmp_path, name)
    resource = command_resource()
    file_wire.respond(resource, status=202)
    assert main(["files", "send", "device", str(path)]) == 0
    read_success(capsys, resource)
    assert len(file_wire.requests) == 1
    assert upload_query(file_wire.requests[0]) == {"name": [name], "mime": [mime],
                                                "kind": ["file"], "ttl_seconds": ["300"]}


def test_empty_generic_file_uploads_zero_length_and_empty_digest(file_wire, tmp_path, capsys):
    path = local_file(tmp_path, content=b"")
    resource = command_resource()
    file_wire.respond(resource, status=202)
    assert main(["files", "send", "device", str(path)]) == 0
    read_success(capsys, resource)
    assert len(file_wire.requests) == 1
    request = file_wire.requests[0]
    assert request["raw_body"] == b""
    assert request["headers"]["content-length"] == "0"
    assert request["headers"]["x-content-sha256"] == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("name", ["a" * 180, "Ж" * 90, "😀" * 45])
def test_safe_basename_accepts_exact_180_utf8_bytes(file_wire, tmp_path, capsys, name):
    path = local_file(tmp_path)
    resource = command_resource()
    file_wire.respond(resource, status=202)
    assert len(name.encode("utf-8")) == 180
    assert main(["files", "send", "device", str(path), "--name", name]) == 0
    read_success(capsys, resource)
    assert len(file_wire.requests) == 1
    assert upload_query(file_wire.requests[0])["name"] == [name]


@pytest.mark.parametrize("name", ["", ".", "..", "../escape", "a/../escape", "/absolute", "a/b",
                                  "a\\b", "C:\\escape", "nul\x00name", "line\nname", "return\rname",
                                  "tab\tname", "delete\x7fname", "a" * 181, "Ж" * 91, "😀" * 46])
def test_unsafe_remote_basename_is_rejected_without_upload(file_wire, tmp_path, capsys, name):
    path = local_file(tmp_path)
    assert main(["files", "send", "device", str(path), "--name", name]) == 1
    read_error(capsys, {"VALIDATION_ERROR"})
    assert not file_wire.requests


@pytest.mark.parametrize("name", ["a\\b.bin", "line\nname.bin", "Ж" * 91])
def test_unsafe_default_local_basename_is_not_silently_rewritten(file_wire, tmp_path, capsys, name):
    path = local_file(tmp_path, name)
    assert main(["files", "send", "device", str(path)]) == 1
    read_error(capsys, {"VALIDATION_ERROR"})
    assert not file_wire.requests


@pytest.mark.parametrize("source", ["missing", "directory", "symlink", "dangling-symlink"])
def test_nonregular_local_source_is_rejected_without_upload(file_wire, tmp_path, capsys, source):
    path = tmp_path / "source"
    if source == "directory":
        path.mkdir()
    elif source == "symlink":
        path.symlink_to(local_file(tmp_path))
    elif source == "dangling-symlink":
        path.symlink_to(tmp_path / "missing-target")
    assert main(["files", "send", "device", str(path)]) == 1
    read_error(capsys, {"VALIDATION_ERROR"})
    assert not file_wire.requests


def test_fifo_is_rejected_promptly_without_waiting_for_writer(file_wire, tmp_path):
    path = tmp_path / "source.fifo"
    os.mkfifo(path)
    env = dict(os.environ)
    source = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [source, env.get("PYTHONPATH")]))
    result = subprocess.run([sys.executable, "-m", "phonebridge", "files", "send", "device", str(path)],
                            env=env, capture_output=True, text=True, timeout=3, check=False)
    assert result.returncode == 1
    assert not result.stdout
    assert json.loads(result.stderr)["error"]["code"] == "VALIDATION_ERROR"
    assert file_wire.token not in result.stderr
    assert not file_wire.requests


def test_source_replaced_by_symlink_at_open_is_rejected(file_wire, tmp_path, capsys, monkeypatch):
    path = local_file(tmp_path, "source.bin")
    target = local_file(tmp_path, "must-not-upload.bin", b"private symlink target")
    real_open = os.open
    replacements = []

    def replace_then_open(candidate, flags, *args, **kwargs):
        if os.fsdecode(candidate) == str(path) and not replacements:
            path.unlink()
            path.symlink_to(target)
            replacements.append(True)
        return real_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_then_open)
    assert main(["files", "send", "device", str(path)]) == 1
    read_error(capsys, {"VALIDATION_ERROR"})
    assert replacements == [True]
    assert not file_wire.requests


@pytest.mark.parametrize("extra", [0, 1])
def test_local_file_size_limit_is_exactly_32_mib(file_wire, tmp_path, capsys, extra):
    path = tmp_path / "large.bin"
    with path.open("wb") as stream:
        stream.truncate(MAX_FILE_SIZE + extra)
    if extra:
        assert main(["files", "send", "device", str(path)]) == 1
        read_error(capsys, {"VALIDATION_ERROR"})
        assert not file_wire.requests
    else:
        resource = command_resource()
        file_wire.respond(resource, status=202)
        assert main(["files", "send", "device", str(path)]) == 0
        read_success(capsys, resource)
        assert len(file_wire.requests) == 1
        request = file_wire.requests[0]
        assert request["raw_body"] == path.read_bytes()
        assert request["headers"]["content-length"] == str(MAX_FILE_SIZE)
        assert request["headers"]["x-content-sha256"] == hashlib.sha256(request["raw_body"]).hexdigest()


@pytest.mark.parametrize("arguments", [["files", "send"], ["images", "send"]])
@pytest.mark.parametrize("format,extension,mime", [("PNG", "png", "image/png"), ("JPEG", "jpg", "image/jpeg"),
                                                  ("WEBP", "webp", "image/webp"), ("GIF", "gif", "image/gif")])
def test_image_mode_and_alias_send_supported_image_bytes(file_wire, tmp_path, capsys, arguments, format, extension, mime):
    content = image_bytes(format)
    path = local_file(tmp_path, "photo." + extension, content)
    resource = command_resource()
    file_wire.respond(resource, status=202)
    flags = ["--image"] if arguments[0] == "files" else []
    assert main([*arguments, "device", str(path), *flags]) == 0
    read_success(capsys, resource)
    assert len(file_wire.requests) == 1
    request = file_wire.requests[0]
    assert urlsplit(request["path"]).path == "/v1/devices/device/files"
    assert upload_query(request) == {"name": [path.name], "mime": [mime], "kind": ["image"], "ttl_seconds": ["300"]}
    assert request["raw_body"] == content
    assert request["headers"]["content-type"] == "application/octet-stream"
    assert request["headers"]["content-length"] == str(len(content))
    assert request["headers"]["x-content-sha256"] == hashlib.sha256(content).hexdigest()


def test_image_mode_accepts_explicit_mime_with_extensionless_local_source(file_wire, tmp_path, capsys):
    path = local_file(tmp_path, "source", image_bytes("PNG"))
    resource = command_resource()
    file_wire.respond(resource, status=202)
    assert main(["images", "send", "device", str(path), "--mime", "image/png", "--name", "photo.png"]) == 0
    read_success(capsys, resource)
    assert len(file_wire.requests) == 1
    assert upload_query(file_wire.requests[0])["mime"] == ["image/png"]
    assert upload_query(file_wire.requests[0])["kind"] == ["image"]


@pytest.mark.parametrize("group", ["files", "images"])
@pytest.mark.parametrize("name,content,mime", [
    ("photo.png", b"", None),
    ("photo.png", b"not a PNG", None),
    ("photo.png", b"GIF89a" + b"\x00" * 32, "image/png"),
    ("photo.gif", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/gif"),
    ("photo.jpg", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/jpeg"),
    ("photo.webp", b"RIFF\x20\x00\x00\x00WAVE" + b"\x00" * 32, "image/webp"),
    ("vector.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
    ("photo.bmp", b"BM" + b"\x00" * 32, "image/bmp"),
    ("photo.bin", b"arbitrary bytes", "application/octet-stream"),
])
def test_image_mode_rejects_unsupported_or_mismatched_magic_before_upload(file_wire, tmp_path, capsys, group, name, content, mime):
    path = local_file(tmp_path, name, content)
    flags = ["--image"] if group == "files" else []
    if mime is not None:
        flags += ["--mime", mime]
    assert main([group, "send", "device", str(path), *flags]) == 1
    read_error(capsys, {"VALIDATION_ERROR"})
    assert not file_wire.requests


@pytest.mark.parametrize("flags", [["--ttl", "0"], ["--ttl", "901"], ["--ttl", "-1"], ["--ttl", "1.5"],
                                   ["--timeout", "0"], ["--timeout", "nan"], ["--timeout", "inf"],
                                   ["--idempotency-key", ""], ["--idempotency-key", "bad\r\nInjected: yes"],
                                   ["--mime", "text/plain\r\nInjected: yes"]])
def test_invalid_delivery_flags_are_rejected_without_upload(file_wire, tmp_path, capsys, flags):
    path = local_file(tmp_path)
    assert main(["files", "send", "device", str(path), *flags]) == 1
    read_error(capsys, {"USAGE_ERROR", "VALIDATION_ERROR"})
    assert not file_wire.requests


@pytest.mark.parametrize("group", ["files", "images"])
def test_wait_uploads_once_then_only_polls_original_command_for_confirmed_result(file_wire, tmp_path, capsys, group):
    path = local_file(tmp_path, "photo.png", image_bytes("PNG"))
    queued = command_resource()
    confirmed = {"uri": "content://downloads/my_downloads/42", "name": "phone-confirmed.bin",
                 "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    succeeded = command_resource("succeeded", data=confirmed)
    file_wire.respond(queued, status=202)
    file_wire.respond(command_resource("sent"))
    file_wire.respond(succeeded)
    assert main([group, "send", "device", str(path), "--wait", "--timeout", "3"]) == 0
    read_success(capsys, succeeded)
    assert [(request["method"], urlsplit(request["path"]).path) for request in file_wire.requests] == [
        ("POST", "/v1/devices/device/files"), ("GET", "/v1/commands/file-command"),
        ("GET", "/v1/commands/file-command")]
    assert file_wire.requests[0]["raw_body"] == path.read_bytes()
    assert all(request["raw_body"] == b"" for request in file_wire.requests[1:])


@pytest.mark.parametrize("status", ["failed", "expired"])
def test_wait_terminal_delivery_failure_is_nonzero_without_reupload(file_wire, tmp_path, capsys, status):
    path = local_file(tmp_path)
    file_wire.respond(command_resource(), status=202)
    failed = command_resource(status, error={"code": "PERMISSION_REQUIRED", "message": file_wire.token})
    file_wire.respond(failed)
    assert main(["files", "send", "device", str(path), "--wait", "--timeout", "2"]) == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert not output.err
    assert result["id"] == "file-command"
    assert result["status"] == status
    assert result["data"] is None
    if status == "failed":
        assert result["error"]["code"] == "PERMISSION_REQUIRED"
    assert file_wire.token not in output.out
    assert [request["method"] for request in file_wire.requests] == ["POST", "GET"]


def test_wait_timeout_keeps_command_identity_without_reupload(file_wire, tmp_path, capsys):
    path = local_file(tmp_path)
    file_wire.respond(command_resource(), status=202)
    file_wire.respond(command_resource("sent"))
    assert main(["files", "send", "device", str(path), "--wait", "--timeout", "0.1"]) == 1
    error = read_error(capsys, {"WAIT_TIMEOUT"})
    assert error["command_id"] == "file-command"
    assert [request["method"] for request in file_wire.requests] == ["POST", "GET"]


def test_poll_failure_keeps_original_command_identity_without_reupload(file_wire, tmp_path, capsys):
    path = local_file(tmp_path)
    file_wire.respond(command_resource(), status=202)
    file_wire.respond({"error": {"message": file_wire.token}}, status=503)
    assert main(["files", "send", "device", str(path), "--wait", "--timeout", "2"]) == 1
    error = read_error(capsys, {"SERVER_ERROR"})
    assert error["command_id"] == "file-command"
    assert [request["method"] for request in file_wire.requests] == ["POST", "GET"]


@pytest.mark.parametrize("status,code", [(401, "UNAUTHORIZED"), (403, "FORBIDDEN"), (404, "NOT_FOUND"),
                                        (409, "CONFLICT"), (422, "VALIDATION_ERROR"), (429, "RATE_LIMITED"),
                                        (500, "SERVER_ERROR"), (503, "SERVER_ERROR")])
def test_upload_http_errors_reuse_safe_policy_and_preserve_ambiguity(file_wire, tmp_path, capsys, status, code):
    path = local_file(tmp_path)
    file_wire.respond({"error": {"message": file_wire.token}}, status=status)
    assert main(["files", "send", "device", str(path), "--idempotency-key", "upload-key"]) == 1
    error = read_error(capsys, {code})
    assert error["idempotency_key"] == "upload-key"
    assert error["may_have_been_submitted"] is (status >= 500)
    assert len(file_wire.requests) == 1


@pytest.mark.parametrize("status,code", [(409, "IDEMPOTENCY_CONFLICT"), (429, "QUEUE_FULL"), (503, "UNAVAILABLE")])
def test_upload_preserves_allowlisted_server_error_codes_without_server_message(file_wire, tmp_path, capsys, status, code):
    path = local_file(tmp_path)
    file_wire.respond({"error": {"code": code, "message": file_wire.token}}, status=status)
    assert main(["files", "send", "device", str(path), "--idempotency-key", "upload-key"]) == 1
    error = read_error(capsys, {code})
    assert error["idempotency_key"] == "upload-key"
    assert len(file_wire.requests) == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_upload_redirect_is_refused_without_forwarding_credentials(file_wire, tmp_path, capsys, status):
    path = local_file(tmp_path)
    file_wire.respond({}, status=status, Location=file_wire.url + "/credential-trap")
    file_wire.respond(command_resource(), status=202)
    assert main(["files", "send", "device", str(path), "--idempotency-key", "redirect-key"]) == 1
    error = read_error(capsys, {"REDIRECT_REFUSED"})
    assert error["idempotency_key"] == "redirect-key"
    assert error["may_have_been_submitted"] is True
    assert len(file_wire.requests) == 1
    assert urlsplit(file_wire.requests[0]["path"]).path == "/v1/devices/device/files"


@pytest.mark.parametrize("failure", ["disconnect", "timeout"])
def test_ambiguous_upload_transport_failure_is_never_retried(file_wire, tmp_path, capsys, failure):
    path = local_file(tmp_path)
    if failure == "disconnect":
        file_wire.respond({}, status="disconnect")
    else:
        file_wire.respond(command_resource(), status=202, test_delay="0.4")
    file_wire.respond(command_resource(), status=202)
    started = time.monotonic()
    assert main(["--request-timeout", "0.1", "files", "send", "device", str(path),
                 "--idempotency-key", "original-upload-key"]) == 1
    assert time.monotonic() - started < 2
    error = read_error(capsys, {"TRANSPORT_ERROR"})
    assert error["idempotency_key"] == "original-upload-key"
    assert error["may_have_been_submitted"] is True
    assert len(file_wire.requests) == 1


def test_ambiguous_upload_failure_exposes_auto_generated_key_for_recovery(file_wire, tmp_path, capsys):
    path = local_file(tmp_path)
    file_wire.respond({}, status="disconnect")
    assert main(["files", "send", "device", str(path)]) == 1
    error = read_error(capsys, {"TRANSPORT_ERROR"})
    assert len(file_wire.requests) == 1
    key = file_wire.requests[0]["headers"]["idempotency-key"]
    assert str(uuid.UUID(key)) == key
    assert error["idempotency_key"] == key
    assert error["may_have_been_submitted"] is True


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b'{"id":"one","id":"two"}', b'{"value":NaN}',
                                  {"id": "cmd", "status": "queued", "expires_at": "tomorrow"},
                                  {"status": "queued", "expires_at": 9999999999},
                                  {"id": "cmd", "status": "unknown", "expires_at": 9999999999}])
def test_invalid_upload_response_remains_ambiguous_without_retry(file_wire, tmp_path, capsys, body):
    path = local_file(tmp_path)
    file_wire.respond(body, status=202)
    assert main(["files", "send", "device", str(path), "--idempotency-key", "invalid-response-key"]) == 1
    error = read_error(capsys, {"INVALID_RESPONSE"})
    assert error["may_have_been_submitted"] is True
    assert error["idempotency_key"] == "invalid-response-key"
    assert len(file_wire.requests) == 1


@pytest.mark.parametrize("response_kind", ["oversized", "truncated", "non-json"])
def test_upload_response_budget_and_content_type_are_enforced(file_wire, tmp_path, capsys, response_kind):
    path = local_file(tmp_path)
    if response_kind == "oversized":
        file_wire.respond(b"x" * (MAX_RESPONSE + 1), status=202)
    elif response_kind == "truncated":
        file_wire.respond(b'{"id":"cmd"}', status=202, **{"Content-Length": "999"})
    else:
        file_wire.respond(b"private server body", status=202, **{"Content-Type": "text/plain"})
    assert main(["files", "send", "device", str(path), "--idempotency-key", "bounded-key"]) == 1
    error = read_error(capsys, {"INVALID_RESPONSE"})
    assert error["may_have_been_submitted"] is True
    assert error["idempotency_key"] == "bounded-key"
    assert len(file_wire.requests) == 1


def test_slow_streaming_upload_response_is_bounded_by_total_request_timeout(file_wire, tmp_path, capsys):
    path = local_file(tmp_path)
    file_wire.respond(command_resource(data={"padding": "x" * 8192}), status=202, test_chunk_delay="0.04")
    started = time.monotonic()
    assert main(["--request-timeout", "0.15", "files", "send", "device", str(path),
                 "--idempotency-key", "stream-key"]) == 1
    assert time.monotonic() - started < 2
    error = read_error(capsys, {"TRANSPORT_ERROR"})
    assert error["idempotency_key"] == "stream-key"
    assert error["may_have_been_submitted"] is True
    assert len(file_wire.requests) == 1


def test_upload_command_output_redacts_actual_admin_credential(file_wire, tmp_path, capsys):
    path = local_file(tmp_path)
    resource = command_resource(data={"echo": file_wire.token, "token": "ordinary-phone-token"})
    file_wire.respond(resource, status=202)
    assert main(["files", "send", "device", str(path)]) == 0
    output = capsys.readouterr()
    assert not output.err
    result = json.loads(output.out)
    assert result["status"] == "queued"
    assert result["data"] == {"echo": "[redacted]", "token": "ordinary-phone-token"}
    assert file_wire.token not in output.out
    assert len(file_wire.requests) == 1


def test_upload_requires_admin_configuration_before_any_http(file_wire, tmp_path, capsys, monkeypatch):
    path = local_file(tmp_path)
    monkeypatch.delenv("PHONEBRIDGE_TOKEN")
    assert main(["files", "send", "device", str(path)]) == 1
    read_error(capsys, {"CONFIG_ERROR"})
    assert not file_wire.requests


def test_upload_refuses_remote_plain_http_before_any_http(file_wire, tmp_path, capsys):
    path = local_file(tmp_path)
    assert main(["--url", "http://example.invalid", "files", "send", "device", str(path)]) == 1
    read_error(capsys, {"CONFIG_ERROR"})
    assert not file_wire.requests
