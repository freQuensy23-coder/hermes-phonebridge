import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from phonebridge_server.app import create_app

from phonebridge_test_support import ADMIN, ADMIN_TOKEN, connected, pair


MAX_FILE_BYTES = 33554432
FILE_CAPABILITIES = ["device.info", "files.receive"]


def upload(client, claim, body=b"phonebridge\x00\xff", *, name="report.bin",
           mime="application/octet-stream", kind="file", ttl: int | str | None = 300, key=None,
           headers=None):
    params = {"name": name, "mime": mime, "kind": kind}
    if ttl is not None:
        params["ttl_seconds"] = str(ttl)
    request_headers = {
        **ADMIN,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(body)),
        "X-Content-SHA256": hashlib.sha256(body).hexdigest(),
    }
    if key is not None:
        request_headers["Idempotency-Key"] = key
    if headers is not None:
        request_headers.update(headers)
    return client.post(f"/v1/devices/{claim['device_id']}/files", params=params,
                       headers=request_headers, content=body)


def queued(response):
    assert response.status_code == 200, response.text
    command = response.json()
    assert set(command) == {"id", "status", "expires_at"}
    assert command["status"] == "queued"
    assert isinstance(command["id"], str) and command["id"]
    return command


def file_frame(socket, command, body=b"phonebridge\x00\xff", *, name="report.bin",
               mime="application/octet-stream", kind="file"):
    frame = socket.receive_json()
    assert set(frame) == {"type", "id", "op", "args", "expires_at"}
    assert frame["type"] == "command"
    assert frame["id"] == command["id"]
    assert frame["op"] == "files.receive"
    assert frame["expires_at"] == command["expires_at"]
    args = frame["args"]
    assert set(args) == {"transfer_id", "name", "mime", "kind", "size", "sha256"}
    assert isinstance(args["transfer_id"], str) and args["transfer_id"]
    assert args == {"transfer_id": args["transfer_id"], "name": name, "mime": mime,
                    "kind": kind, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    return frame


def download(client, claim, transfer_id, **kwargs):
    return client.get(f"/v1/device/files/{transfer_id}",
                      headers={"Authorization": f"Bearer {claim['device_token']}"},
                      follow_redirects=False, **kwargs)


def assert_bytes(response, body):
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert "no-store" in response.headers["cache-control"]
    assert "location" not in response.headers
    assert response.content == body


def barrier(socket):
    socket.send_json({"type": "event", "id": "file-barrier", "name": "test.barrier",
                      "data": {}, "time": 0})
    assert socket.receive_json() == {"type": "ack", "id": "file-barrier"}


def finish(socket, command, ok=True):
    socket.send_json({"type": "result", "id": command["id"], "ok": ok,
                      "data": {"saved": True} if ok else None,
                      "error": None if ok else {"code": "SAVE_FAILED", "message": "Save failed"}})
    barrier(socket)


def pending_ids(service, claim):
    store = service[0].state.store
    device_id, session_id = store.connect(claim["device_token"])
    try:
        return store.pending(device_id, session_id)
    finally:
        store.disconnect(device_id, session_id)


@pytest.fixture
def phone(client):
    _, claim = pair(client)
    with connected(client, claim, capabilities=FILE_CAPABILITIES):
        device = client.get(f"/v1/devices/{claim['device_id']}", headers=ADMIN).json()
        assert device["capabilities"] == FILE_CAPABILITIES
    return claim


@pytest.mark.parametrize("body,name,mime,kind", [
    (b"", "empty.bin", "application/octet-stream", "file"),
    (b"\x00\xff\xfe\r\nnot-json", "report.bin", "application/octet-stream", "file"),
    (b"http://127.0.0.1/private", "url.txt", "text/plain", "file"),
    (b"\x89PNG\r\n\x1a\nimage-bytes", "photo.png", "image/png", "image"),
    ("Привет".encode(), "отчёт 2026.txt", "text/plain", "file"),
])
def test_offline_upload_enqueues_one_exact_file_command(client, service, phone, clock,
                                                        body, name, mime, kind):
    response = upload(client, phone, body, name=name, mime=mime, kind=kind, ttl=None)
    command = queued(response)
    assert command["expires_at"] == int(clock()) + 300
    assert pending_ids(service, phone) == [command["id"]]
    with connected(client, phone, capabilities=FILE_CAPABILITIES) as socket:
        frame = file_frame(socket, command, body, name=name, mime=mime, kind=kind)
        assert_bytes(download(client, phone, frame["args"]["transfer_id"]), body)
        assert_bytes(download(client, phone, frame["args"]["transfer_id"]), body)


def test_upload_and_blob_survive_server_restart(settings, clock):
    with TestClient(create_app(settings, clock)) as client:
        _, claim = pair(client)
        with connected(client, claim, capabilities=FILE_CAPABILITIES):
            pass
        command = queued(upload(client, claim, key="restart"))
    with TestClient(create_app(settings, clock)) as client:
        with connected(client, claim, capabilities=FILE_CAPABILITIES) as socket:
            frame = file_frame(socket, command)
            assert_bytes(download(client, claim, frame["args"]["transfer_id"]), b"phonebridge\x00\xff")
            finish(socket, command)
        assert client.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()["status"] == "succeeded"


@pytest.mark.parametrize("ttl", [1, 900])
def test_upload_ttl_boundaries(client, phone, clock, ttl):
    command = queued(upload(client, phone, ttl=ttl))
    assert command["expires_at"] == int(clock()) + ttl


@pytest.mark.parametrize("ttl", [0, -1, 901, "1.5", "true", ""])
def test_invalid_ttl_does_not_enqueue(client, service, phone, ttl):
    assert upload(client, phone, ttl=ttl).status_code in (400, 422)
    assert pending_ids(service, phone) == []


@pytest.mark.parametrize("name", [
    "", ".", "..", "../secret", "folder/file.txt", "/absolute.txt", "folder\\file.txt",
    "..\\secret", "C:\\private.txt", "bad\x00name", "bad\nname", "bad\rname", "bad\tname",
    "bad\x1fname", "bad\x7fname", "bad\ud800name", "bad\udfffname",
])
def test_invalid_basename_does_not_enqueue(client, service, phone, name):
    body = b"name-test"
    encoded = quote(name, safe="", encoding="utf-8", errors="surrogatepass")
    response = client.post(
        f"/v1/devices/{phone['device_id']}/files?name={encoded}&mime=application%2Foctet-stream&kind=file",
        headers={**ADMIN, "Content-Type": "application/octet-stream", "Content-Length": str(len(body)),
                 "X-Content-SHA256": hashlib.sha256(body).hexdigest()}, content=body,
    )
    assert response.status_code in (400, 422)
    assert pending_ids(service, phone) == []


@pytest.mark.parametrize("mime,kind", [
    ("", "file"), ("not-a-mime", "file"), ("text/plain\r\nX-Evil: yes", "file"),
    ("application/octet-stream", "download"), ("application/octet-stream", ""),
])
def test_invalid_metadata_does_not_enqueue(client, service, phone, mime, kind):
    assert upload(client, phone, mime=mime, kind=kind).status_code in (400, 422)
    assert pending_ids(service, phone) == []


@pytest.mark.parametrize("digest", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64,
                                    " " + "a" * 64, "a" * 64 + "\n"])
def test_malformed_sha256_does_not_enqueue(client, service, phone, digest):
    response = upload(client, phone, headers={"X-Content-SHA256": digest})
    assert response.status_code in (400, 422)
    assert pending_ids(service, phone) == []


def test_digest_mismatch_rolls_back_upload_and_idempotency(client, service, phone):
    response = upload(client, phone, key="digest-retry", headers={"X-Content-SHA256": "0" * 64})
    assert response.status_code in (400, 422)
    assert pending_ids(service, phone) == []
    command = queued(upload(client, phone, key="digest-retry"))
    assert pending_ids(service, phone) == [command["id"]]


@pytest.mark.parametrize("header", ["Content-Length", "X-Content-SHA256", "Content-Type"])
def test_upload_requires_single_mandatory_headers(client, service, phone, header):
    request = client.build_request(
        "POST", f"/v1/devices/{phone['device_id']}/files",
        params={"name": "x.bin", "mime": "application/octet-stream", "kind": "file"},
        headers={**ADMIN, "Content-Type": "application/octet-stream",
                 "X-Content-SHA256": hashlib.sha256(b"x").hexdigest()}, content=b"x",
    )
    del request.headers[header]
    assert client.send(request).status_code in (400, 411, 415, 422)
    assert pending_ids(service, phone) == []


@pytest.mark.parametrize("header", ["Authorization", "Content-Length", "Content-Type",
                                    "X-Content-SHA256", "Idempotency-Key"])
def test_upload_rejects_duplicate_security_headers(client, service, phone, header):
    headers = [*ADMIN.items(), ("Content-Type", "application/octet-stream"),
               ("Content-Length", "1"), ("X-Content-SHA256", hashlib.sha256(b"x").hexdigest()),
               ("Idempotency-Key", "duplicate")]
    headers.append(next(item for item in headers if item[0] == header))
    response = client.post(f"/v1/devices/{phone['device_id']}/files",
                           params={"name": "x.bin", "mime": "application/octet-stream", "kind": "file"},
                           headers=headers, content=b"x")
    assert response.status_code in (400, 401, 415, 422)
    assert pending_ids(service, phone) == []


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", "multipart/form-data"])
def test_upload_requires_octet_stream(client, service, phone, content_type):
    assert upload(client, phone, headers={"Content-Type": content_type}).status_code == 415
    assert pending_ids(service, phone) == []


@pytest.mark.parametrize("length", ["0", "1", "4", "-1", "+3", "three"])
def test_content_length_must_equal_raw_body(client, service, phone, length):
    assert upload(client, phone, b"abc", headers={"Content-Length": length}).status_code in (400, 422)
    assert pending_ids(service, phone) == []


def test_announced_overlimit_upload_is_rejected_without_body(client, service, phone):
    response = upload(client, phone, b"", headers={"Content-Length": str(MAX_FILE_BYTES + 1)})
    assert response.status_code == 413
    assert pending_ids(service, phone) == []


def test_actual_overlimit_body_is_rejected_even_with_small_declared_length(client, service, phone):
    body = b"x" * (MAX_FILE_BYTES + 1)
    response = upload(client, phone, body, headers={"Content-Length": "1"})
    assert response.status_code == 413
    assert pending_ids(service, phone) == []


@pytest.mark.parametrize("key", ["", "a" * 129, "bad/key", "bad key", "bad\\key"])
def test_invalid_idempotency_key_does_not_enqueue(client, service, phone, key):
    assert upload(client, phone, key=key).status_code in (400, 422)
    assert pending_ids(service, phone) == []


def test_identical_idempotency_retry_returns_same_command_without_extra_queue_entry(client, service, phone):
    key = "a" * 128
    first = queued(upload(client, phone, key=key))
    assert queued(upload(client, phone, key=key)) == first
    assert pending_ids(service, phone) == [first["id"]]


@pytest.mark.parametrize("changed", [
    {"body": b"different"}, {"name": "other.bin"}, {"mime": "text/plain"},
    {"kind": "image", "mime": "image/png"}, {"ttl": 301},
])
def test_idempotency_conflicts_do_not_change_original_blob(client, service, phone, changed):
    command = queued(upload(client, phone, key="conflict"))
    assert upload(client, phone, key="conflict", **changed).status_code == 409
    assert pending_ids(service, phone) == [command["id"]]
    with connected(client, phone, capabilities=FILE_CAPABILITIES) as socket:
        frame = file_frame(socket, command)
        assert_bytes(download(client, phone, frame["args"]["transfer_id"]), b"phonebridge\x00\xff")


def test_idempotency_key_is_scoped_to_device(client, phone):
    _, other = pair(client, "Other phone")
    with connected(client, other, capabilities=FILE_CAPABILITIES):
        pass
    first = queued(upload(client, phone, key="same"))
    second = queued(upload(client, other, key="same"))
    assert first["id"] != second["id"]


def test_concurrent_identical_uploads_atomically_create_one_command(client, service, phone):
    with ThreadPoolExecutor(max_workers=2) as pool:
        commands = list(pool.map(lambda _: queued(upload(client, phone, key="concurrent")), range(2)))
    assert commands[0] == commands[1]
    assert pending_ids(service, phone) == [commands[0]["id"]]


def test_upload_without_idempotency_key_creates_independent_transfers(client, service, phone):
    first = queued(upload(client, phone))
    second = queued(upload(client, phone))
    assert first["id"] != second["id"]
    assert pending_ids(service, phone) == [first["id"], second["id"]]


@pytest.mark.parametrize("credential", [None, "wrong", "device", "pairing"])
def test_unauthorized_upload_does_not_enqueue(client, service, phone, credential):
    pairing, _ = pair(client, "Credentials")
    token = {"device": phone["device_token"], "pairing": pairing["qr_payload"]["token"]}.get(credential, credential)
    request = client.build_request(
        "POST", f"/v1/devices/{phone['device_id']}/files",
        params={"name": "x.bin", "mime": "application/octet-stream", "kind": "file"},
        headers={"Content-Type": "application/octet-stream", "X-Content-SHA256": hashlib.sha256(b"x").hexdigest(),
                 **({"Authorization": f"Bearer {token}"} if token else {})}, content=b"x",
    )
    assert client.send(request).status_code == 401
    assert pending_ids(service, phone) == []
    command = queued(upload(client, phone, key="authorized"))
    assert pending_ids(service, phone) == [command["id"]]


def test_query_string_admin_token_cannot_authorize_upload(client, service, phone):
    response = client.post(
        f"/v1/devices/{phone['device_id']}/files",
        params={"name": "empty.bin", "mime": "application/octet-stream", "kind": "file", "token": ADMIN_TOKEN},
        headers={"Content-Type": "application/octet-stream", "Content-Length": "0",
                 "X-Content-SHA256": hashlib.sha256(b"").hexdigest()}, content=b"",
    )
    assert response.status_code == 401
    assert pending_ids(service, phone) == []


def test_only_owner_device_bearer_can_download_a_sent_transfer(client, phone):
    _, other = pair(client, "Other phone")
    command = queued(upload(client, phone))
    with connected(client, phone, capabilities=FILE_CAPABILITIES) as socket:
        frame = file_frame(socket, command)
        transfer_id = frame["args"]["transfer_id"]
        path = f"/v1/device/files/{transfer_id}"
        assert client.get(path).status_code == 401
        assert client.get(path, headers=ADMIN).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert download(client, other, transfer_id).status_code in (403, 404)
        assert client.get(path, params={"token": phone["device_token"]}).status_code == 401
        assert client.get(path, headers=[("Authorization", f"Bearer {phone['device_token']}"),
                                         ("Authorization", f"Bearer {phone['device_token']}")]).status_code == 401
        assert_bytes(download(client, phone, transfer_id), b"phonebridge\x00\xff")
        assert download(client, phone, "missing-transfer").status_code == 404


def test_owner_cannot_download_queued_transfer_before_dispatch(client, service, phone):
    command = queued(upload(client, phone))
    store = service[0].state.store
    with store.lock:
        row = store.connection.execute("SELECT args FROM commands WHERE id=?", (command["id"],)).fetchone()
        transfer_id = json.loads(row["args"])["transfer_id"]
    assert download(client, phone, transfer_id).status_code in (403, 404, 409)
    assert pending_ids(service, phone) == [command["id"]]
    with connected(client, phone, capabilities=FILE_CAPABILITIES) as socket:
        frame = file_frame(socket, command)
        assert frame["args"]["transfer_id"] == transfer_id
        assert_bytes(download(client, phone, transfer_id), b"phonebridge\x00\xff")


@pytest.mark.parametrize("ok", [True, False])
def test_terminal_result_removes_download_but_preserves_command_metadata(client, phone, ok):
    command = queued(upload(client, phone, key="terminal"))
    with connected(client, phone, capabilities=FILE_CAPABILITIES) as socket:
        frame = file_frame(socket, command)
        transfer_id = frame["args"]["transfer_id"]
        assert_bytes(download(client, phone, transfer_id), b"phonebridge\x00\xff")
        finish(socket, command, ok)
        assert download(client, phone, transfer_id).status_code in (403, 404, 410)
        status = client.get(f"/v1/commands/{command['id']}", headers=ADMIN)
        assert status.status_code == 200
        assert status.json()["status"] == ("succeeded" if ok else "failed")
        assert status.json()["op"] == "files.receive"
        assert status.json()["device_id"] == phone["device_id"]


def test_sent_transfer_expires_at_exact_ttl(client, phone, clock):
    command = queued(upload(client, phone, ttl=1))
    with connected(client, phone, capabilities=FILE_CAPABILITIES) as socket:
        frame = file_frame(socket, command)
        transfer_id = frame["args"]["transfer_id"]
        assert_bytes(download(client, phone, transfer_id), b"phonebridge\x00\xff")
        clock.advance(1)
        assert download(client, phone, transfer_id).status_code in (403, 404, 410)
        assert client.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()["status"] == "expired"


@pytest.mark.parametrize("target", ["device", "pairing"])
def test_revocation_denies_existing_transfer_and_retains_failed_command(client, phone, target):
    pairing, owner = pair(client, "Revoked phone")
    with connected(client, owner, capabilities=FILE_CAPABILITIES) as socket:
        command = queued(upload(client, owner))
        frame = file_frame(socket, command)
        path = (f"/v1/devices/{owner['device_id']}" if target == "device"
                else f"/v1/pairings/{pairing['id']}")
        assert client.delete(path, headers=ADMIN).status_code == 204
        with pytest.raises(WebSocketDisconnect):
            while True:
                socket.receive_json()
    assert download(client, owner, frame["args"]["transfer_id"]).status_code == 401
    assert client.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()["status"] == "failed"
    assert upload(client, owner).status_code == 404


def test_pending_transfer_limit_is_sixteen_and_expiry_reclaims_capacity(client, service, phone, clock):
    commands = [queued(upload(client, phone, b"", key=f"pending-{index}", ttl=1)) for index in range(16)]
    assert upload(client, phone, b"", key="overflow").status_code == 429
    assert queued(upload(client, phone, b"", key="pending-0", ttl=1)) == commands[0]
    assert pending_ids(service, phone) == [command["id"] for command in commands]
    _, other = pair(client, "Separate transfer quota")
    with connected(client, other, capabilities=FILE_CAPABILITIES):
        pass
    queued(upload(client, other, b""))
    clock.advance(1)
    service[0].state.store.maintain()
    assert pending_ids(service, phone) == []
    for command in commands:
        assert client.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()["status"] == "expired"
    assert queued(upload(client, phone, b"", key="overflow"))["id"] not in {command["id"] for command in commands}


def test_global_blob_quota_reclaimed_after_success_failure_expiry_and_revocation(client, service, phone, clock):
    owners = [phone]
    for index in range(4):
        _, claim = pair(client, f"Quota phone {index}")
        with connected(client, claim, capabilities=FILE_CAPABILITIES):
            pass
        owners.append(claim)
    body = b"a" * MAX_FILE_BYTES
    unauthorized = upload(client, owners[4], body,
                          headers={"Authorization": f"Bearer {owners[4]['device_token']}"})
    assert unauthorized.status_code == 401
    commands = []
    for index, owner in enumerate(owners[:4]):
        body = bytes([97 + index]) * MAX_FILE_BYTES
        commands.append(queued(upload(client, owner, body, ttl=1 if index == 2 else 300)))
    assert upload(client, owners[4], b"x", key="overflow").status_code == 429
    for index, owner in enumerate(owners[:4]):
        if index < 2:
            with connected(client, owner, capabilities=FILE_CAPABILITIES) as socket:
                frame = file_frame(socket, commands[index], bytes([97 + index]) * MAX_FILE_BYTES)
                finish(socket, commands[index], ok=index == 0)
                assert download(client, owner, frame["args"]["transfer_id"]).status_code in (403, 404, 410)
        elif index == 2:
            clock.advance(1)
            service[0].state.store.maintain()
        else:
            assert client.delete(f"/v1/devices/{owner['device_id']}", headers=ADMIN).status_code == 204
        status = client.get(f"/v1/commands/{commands[index]['id']}", headers=ADMIN)
        assert status.status_code == 200
        assert status.json()["status"] == ["succeeded", "failed", "expired", "failed"][index]
        queued(upload(client, owners[4], bytes([110 + index]) * MAX_FILE_BYTES, key=f"reclaimed-{index}"))
        assert upload(client, owners[4], b"x", key="overflow").status_code == 429


@pytest.mark.parametrize("args", [
    {},
    {"transfer_id": "fabricated", "name": "report.bin", "mime": "application/octet-stream",
     "kind": "file", "size": 0, "sha256": hashlib.sha256(b"").hexdigest()},
    {"url": "http://127.0.0.1/private", "name": "report.bin"},
    {"path": "/etc/passwd"},
    {"name": "bad\ud800name"},
])
def test_public_command_enqueue_cannot_fabricate_file_transfer(client, service, phone, args):
    response = client.post(
        f"/v1/devices/{phone['device_id']}/commands",
        headers={**ADMIN, "Content-Type": "application/json"},
        content=json.dumps({"op": "files.receive", "args": args}),
    )
    assert response.status_code == 422
    assert pending_ids(service, phone) == []


def test_public_command_enqueue_cannot_reuse_real_bound_metadata(client, service, phone):
    command = queued(upload(client, phone))
    with connected(client, phone, capabilities=FILE_CAPABILITIES) as socket:
        frame = file_frame(socket, command)
        response = client.post(f"/v1/devices/{phone['device_id']}/commands", headers=ADMIN,
                               json={"op": "files.receive", "args": frame["args"]})
        assert response.status_code == 422
        assert_bytes(download(client, phone, frame["args"]["transfer_id"]), b"phonebridge\x00\xff")
    assert pending_ids(service, phone) == [command["id"]]


@pytest.mark.parametrize("op", ["files.read", "files.write", "files.list", "files.delete", "files.download"])
def test_file_delivery_does_not_enable_remote_filesystem_commands(client, service, phone, op):
    response = client.post(f"/v1/devices/{phone['device_id']}/commands", headers=ADMIN,
                           json={"op": op, "args": {"path": "/etc/passwd", "url": "http://127.0.0.1/"}})
    assert response.status_code == 422
    assert pending_ids(service, phone) == []


@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/device/files"), ("POST", "/v1/device/files"),
    ("DELETE", "/v1/device/files/missing"), ("PUT", "/v1/device/files/missing"),
    ("GET", "/v1/files"), ("POST", "/v1/files/download"),
])
def test_no_remote_filesystem_http_surface(client, phone, method, path):
    response = client.request(method, path, headers=ADMIN,
                              params={"path": "/etc/passwd", "url": "http://127.0.0.1/"})
    assert response.status_code in (404, 405)
