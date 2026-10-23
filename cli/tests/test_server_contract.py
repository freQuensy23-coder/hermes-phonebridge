import json
import socket
import threading
import time

import pytest
import uvicorn

from phonebridge.cli import main
from phonebridge.client import Client, MAX_RESPONSE, Settings
from phonebridge_server.app import create_app
from phonebridge_server.config import Settings as ServerSettings
from phonebridge_server.protocol import (
    OPERATIONS,
    ClaimRequest,
    CommandRequest,
    EventFrame,
    HelloFrame,
    PairingRequest,
    ResultFrame,
    encode_json,
    validate_json,
)


ADMIN_TOKEN = "cli-ci-admin-credential-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@pytest.fixture
def bridge_server(tmp_path):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    settings = ServerSettings(admin_token=ADMIN_TOKEN, public_url=url, allow_insecure=True,
                              db_path=tmp_path / "state" / "bridge.sqlite3", http_rate=10000, claim_rate=2000,
                              max_connections=1000, online_timeout=3600)
    application = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(application, access_log=False, log_config=None, ws="websockets-sansio"))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield Client(Settings(url, ADMIN_TOKEN, 5)), application.state.store
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        listener.close()
        assert not worker.is_alive()


def enroll(store, name="CI phone", capabilities=None, permissions=None, version="0.1.0"):
    pairing = store.create_pairing(PairingRequest(name=name))
    claim = store.claim(ClaimRequest(token=pairing["qr_payload"]["token"], name=name, platform="android"))
    device_id, session = store.connect(claim["device_token"])
    store.hello(device_id, session, HelloFrame(type="hello", version=version,
                                             capabilities=capabilities or ["device.info", "screen.tree"],
                                             permissions=permissions or {}))
    return device_id, session


@pytest.mark.parametrize("waiting", [False, True])
def test_real_server_revocation_result_retains_cli_code(bridge_server, environment, monkeypatch, capsys, waiting):
    client, store = bridge_server
    device_id, session = enroll(store)
    store.disconnect(device_id, session)
    submitted = client.submit(device_id, "device.info", {}, idempotency_key="revoked-command")
    command_id = submitted["id"]
    assert client.request("DELETE", f"/v1/devices/{device_id}") == {}
    expected = store.command(command_id)
    assert expected["error"]["code"] == "DEVICE_REVOKED"
    assert expected["status"] == "failed"
    monkeypatch.setenv("PHONEBRIDGE_URL", client.settings.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", ADMIN_TOKEN)
    assert main(["command-status", command_id] + (["--wait"] if waiting else [])) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {**expected, "error": {
        "code": "DEVICE_REVOKED", "message": "Device authorization revoked"}}
    assert not output.err
    assert ADMIN_TOKEN not in output.out


def test_real_server_full_legal_event_page_has_no_drops(bridge_server, environment, monkeypatch, capsys):
    client, store = bridge_server
    device_id, session = enroll(store)
    overhead = len(encode_json({"text": ""}).encode("utf-8"))
    data = {"text": "Ж" * ((16384 - overhead) // len("Ж".encode("utf-8")))}
    assert 16380 <= len(encode_json(data).encode("utf-8")) <= 16384
    for index in range(100):
        frame = EventFrame(type="event", id=f"{index:03d}" + "e" * 125, name="n" * 128,
                           data=data, time=2**63 - 1)
        assert store.event(device_id, session, frame) == {"type": "ack", "id": frame.id}
    expected = store.events(device_id, 0, 100)
    assert len(encode_json(expected).encode("utf-8")) > MAX_RESPONSE
    monkeypatch.setenv("PHONEBRIDGE_URL", client.settings.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", ADMIN_TOKEN)
    assert main(["events", device_id, "--limit", "100"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == expected
    assert len(expected["events"]) == 100
    assert expected["next_cursor"] == expected["events"][-1]["cursor"]
    assert not output.err


def test_real_server_maximum_device_collection_has_no_drops(bridge_server, environment, monkeypatch, capsys):
    client, store = bridge_server
    permissions = {f"p{index:03d}" + "x" * 124: bool(index % 2) for index in range(64)}
    capabilities = sorted(OPERATIONS)
    with store.lock:
        store.connection.execute("PRAGMA synchronous=OFF")
    try:
        for _ in range(store.settings.max_devices):
            enroll(store, name="😀" * 128, capabilities=capabilities,
                   permissions=permissions, version="1.1.1+" + "x" * 26)
    finally:
        with store.lock:
            store.connection.execute("PRAGMA synchronous=FULL")
    assert store.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    expected = store.devices()
    assert len(expected["devices"]) == store.settings.max_devices == 1000
    assert len(encode_json(expected).encode("utf-8")) > MAX_RESPONSE
    monkeypatch.setenv("PHONEBRIDGE_URL", client.settings.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", ADMIN_TOKEN)
    assert main(["devices", "list"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == expected
    assert not output.err


@pytest.mark.parametrize("target", ["status", "id", "device_id", "op", "data", "succeeded"])
def test_server_valid_opaque_phone_result_preserves_cli_metadata(bridge_server, environment, monkeypatch, capsys, target):
    client, store = bridge_server
    device_id, session = enroll(store)
    submitted = client.submit(device_id, "device.info", {}, idempotency_key="opaque-result")
    command_id = submitted["id"]
    assert store.dispatch(device_id, session, command_id)["id"] == command_id
    data = {"token": target, "password": command_id, "device_token": device_id,
            "clipboard": "Bearer ordinary-phone-clipboard", "nested": {"cookies": "personal-cookie"}}
    store.result(device_id, session, ResultFrame(type="result", id=command_id, ok=True, data=data))
    expected = store.command(command_id)
    monkeypatch.setenv("PHONEBRIDGE_URL", client.settings.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", ADMIN_TOKEN)
    assert main(["command-status", command_id]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == expected
    assert not output.err


@pytest.mark.parametrize("character", ["Ж", "😀"])
def test_server_accepts_near_max_utf8_args_from_real_cli_transport(bridge_server, character):
    client, store = bridge_server
    device_id, session = enroll(store)
    overhead = len(encode_json({"text": ""}).encode("utf-8"))
    args = {"text": character * ((32768 - overhead) // len(character.encode("utf-8")))}
    assert validate_json(args) == args
    assert CommandRequest(op="device.info", args=args).args == args
    assert len(json.dumps(args, ensure_ascii=True).encode("ascii")) > 65536
    result = client.submit(device_id, "device.info", args, idempotency_key="unicode-server")
    assert result["status"] == "queued"
    assert store.dispatch(device_id, session, result["id"])["args"] == args
