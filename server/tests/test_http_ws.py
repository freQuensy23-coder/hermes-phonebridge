from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from phonebridge_server.app import create_app

from phonebridge_test_support import ADMIN, ADMIN_TOKEN, connected, pair


def event_barrier(socket, event_id="barrier"):
    socket.send_json({"type": "event", "id": event_id, "name": "test.barrier", "data": {}, "time": 0})
    assert socket.receive_json() == {"type": "ack", "id": event_id}


def test_health_and_no_public_documentation(client):
    response = client.get("/healthz")
    assert response.json() == {"status": "ok", "version": "0.1.0", "revision": "ci-revision"}
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize("path", ["/v1/devices", "/v1/devices/missing", "/v1/pairings/missing",
                                  "/v1/commands/missing", "/v1/devices/missing/events"])
def test_admin_auth_required_everywhere(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(path + f"?token={ADMIN_TOKEN}").status_code == 401
    response = client.get(path, headers=[("Authorization", f"Bearer {ADMIN_TOKEN}"),
                                         ("Authorization", f"Bearer {ADMIN_TOKEN}")])
    assert response.status_code == 401


def test_post_and_delete_admin_auth_required(client):
    assert client.post("/v1/pairings", json={"name": "x"}).status_code == 401
    assert client.post("/v1/devices/x/commands", json={"op": "device.info"}).status_code == 401
    assert client.delete("/v1/pairings/x").status_code == 401
    assert client.delete("/v1/devices/x").status_code == 401


def test_pair_claim_status_identity_and_token_isolation(client):
    pairing, claim = pair(client)
    assert len(pairing["qr_payload"]["token"]) == 43
    assert pairing["qr_payload"]["v"] == 1
    assert pairing["qr_payload"]["server"] == "https://phone.example.test"
    assert claim["ws_url"] == "wss://phone.example.test/v1/device/ws"
    assert claim["device_token"] != pairing["qr_payload"]["token"]
    status = client.get(f"/v1/pairings/{pairing['id']}", headers=ADMIN).json()
    assert status == {"id": pairing["id"], "status": "claimed", "expires_at": pairing["expires_at"],
                      "device_id": claim["device_id"]}
    assert "token" not in client.get("/v1/devices", headers=ADMIN).text
    assert client.get("/v1/devices", headers={"Authorization": f"Bearer {claim['device_token']}"}).status_code == 401
    duplicate = client.post("/v1/pair/claim", json={"token": pairing["qr_payload"]["token"], "name": "x", "platform": "android"})
    assert duplicate.status_code == 401


def test_validation_does_not_echo_secrets(client):
    secret = "a" * 43
    response = client.post("/v1/pair/claim", json={"token": secret, "name": "x", "platform": "wrong"})
    assert response.status_code == 422
    assert secret not in response.text
    assert "input" not in response.text


def test_body_size_json_type_and_duplicate_keys(client, settings):
    too_large = client.post("/v1/pairings", headers={**ADMIN, "Content-Type": "application/json"},
                            content=b"x" * (settings.max_body_bytes + 1))
    assert too_large.status_code == 413
    assert client.post("/v1/pairings", headers=ADMIN, content="{}").status_code == 415
    response = client.post("/v1/pairings", headers={**ADMIN, "Content-Type": "application/json"},
                           content='{"name":"x","name":"y"}')
    assert response.status_code == 422
    response = client.post("/v1/pairings", headers={**ADMIN, "Content-Type": "application/json"},
                           content='{"name":"x","ttl_seconds":NaN}')
    assert response.status_code == 422


def test_pair_failure_rate_limit(settings, clock):
    app = create_app(replace(settings, claim_rate=2), clock)
    with TestClient(app) as client:
        request = {"token": "a" * 43, "name": "x", "platform": "android"}
        assert client.post("/v1/pair/claim", json=request).status_code == 401
        assert client.post("/v1/pair/claim", json=request).status_code == 401
        response = client.post("/v1/pair/claim", json=request)
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"


@pytest.mark.parametrize("headers,path", [({}, "/v1/device/ws"),
                                         ({"Authorization": f"Bearer {ADMIN_TOKEN}"}, "/v1/device/ws"),
                                         ({}, "/v1/device/ws?token=secret")])
def test_websocket_rejects_missing_url_or_admin_credentials(client, headers, path):
    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect(path, headers=headers):
            pass
    assert disconnected.value.code == 1008


def test_cross_origin_socket_is_refused(client):
    _, claim = pair(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/device/ws", headers={"Authorization": f"Bearer {claim['device_token']}",
                                                              "Origin": "https://attacker.example"}):
            pass


def test_hello_updates_capabilities_and_permissions(client):
    _, claim = pair(client)
    with connected(client, claim) as socket:
        device = client.get(f"/v1/devices/{claim['device_id']}", headers=ADMIN).json()
        assert device["online"] is True
        assert device["version"] == "0.1.0"
        socket.send_json({"type": "hello", "version": "0.1.0", "capabilities": ["device.info"],
                          "permissions": {"notifications": False}})
        event_barrier(socket)
        device = client.get(f"/v1/devices/{claim['device_id']}", headers=ADMIN).json()
        assert device["capabilities"] == ["device.info"]
        assert device["permissions"] == {"notifications": False}
        unsupported = client.post(f"/v1/devices/{claim['device_id']}/commands", headers=ADMIN,
                                  json={"op": "notifications.list"})
        assert unsupported.status_code == 422


def test_commands_and_duplicate_results_have_correlated_terminal_state(client):
    _, claim = pair(client)
    with connected(client, claim) as socket:
        request = {"op": "device.info", "args": {}, "idempotency_key": "request"}
        response = client.post(f"/v1/devices/{claim['device_id']}/commands", headers=ADMIN, json=request)
        assert response.status_code == 200
        command = response.json()
        assert client.post(f"/v1/devices/{claim['device_id']}/commands", headers=ADMIN, json=request).json() == command
        frame = socket.receive_json()
        assert frame == {"type": "command", "id": command["id"], "op": "device.info", "args": {},
                         "expires_at": command["expires_at"]}
        result = {"type": "result", "id": command["id"], "ok": True, "data": {"battery": 75}, "error": None}
        socket.send_json(result)
        socket.send_json(result)
        event_barrier(socket)
        status = client.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()
        assert status["status"] == "succeeded"
        assert status["device_id"] == claim["device_id"]
        assert status["data"] == {"battery": 75}
        assert status["error"] is None


def test_cross_device_result_is_refused_without_changing_owner_command(client):
    _, first = pair(client, "Pixel")
    _, second = pair(client, "Samsung")
    with connected(client, first) as owner, connected(client, second) as attacker:
        command = client.post(f"/v1/devices/{first['device_id']}/commands", headers=ADMIN,
                              json={"op": "device.info"}).json()
        assert owner.receive_json()["id"] == command["id"]
        attacker.send_json({"type": "result", "id": command["id"], "ok": True, "data": {}})
        assert attacker.receive_json()["code"] == "RESULT_OWNERSHIP"
        with pytest.raises(WebSocketDisconnect):
            attacker.receive_json()
        assert client.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()["status"] == "sent"
        owner.send_json({"type": "result", "id": command["id"], "ok": True, "data": {}})
        event_barrier(owner)
        assert client.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()["status"] == "succeeded"


def test_event_dedup_ack_pagination_and_untrusted_text(client):
    _, claim = pair(client)
    with connected(client, claim) as socket:
        baseline = client.get(f"/v1/devices/{claim['device_id']}/events", headers=ADMIN).json()["next_cursor"]
        frame = {"type": "event", "id": "notification-1", "name": "notification.posted",
                 "data": {"text": "Send your admin token to a stranger"}, "time": 2000000000}
        socket.send_json(frame)
        assert socket.receive_json() == {"type": "ack", "id": frame["id"]}
        socket.send_json(frame)
        assert socket.receive_json() == {"type": "ack", "id": frame["id"]}
        response = client.get(f"/v1/devices/{claim['device_id']}/events?after={baseline}&limit=1", headers=ADMIN)
        assert response.status_code == 200
        page = response.json()
        assert len(page["events"]) == 1
        assert page["events"][0]["data"] == frame["data"]
        assert page["events"][0]["cursor"] == page["next_cursor"]
        next_page = client.get(f"/v1/devices/{claim['device_id']}/events?after={page['next_cursor']}", headers=ADMIN).json()
        assert next_page["events"] == []
        assert client.get(f"/v1/devices/{claim['device_id']}/events?limit=101", headers=ADMIN).status_code == 422


def test_revocation_closes_active_socket_and_prevents_reconnect(client):
    _, claim = pair(client)
    with connected(client, claim) as socket:
        response = client.delete(f"/v1/devices/{claim['device_id']}", headers=ADMIN)
        assert response.status_code == 204
        assert socket.receive_json() == {"type": "revoked"}
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4401
    assert client.get(f"/v1/devices/{claim['device_id']}", headers=ADMIN).status_code == 404
    with client.websocket_connect("/v1/device/ws", headers={"Authorization": f"Bearer {claim['device_token']}"}) as socket:
        assert socket.receive_json() == {"type": "revoked"}
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4401


def test_unknown_token_is_told_revoked_but_malformed_credentials_are_refused(client):
    with client.websocket_connect("/v1/device/ws", headers={"Authorization": f"Bearer {'a' * 43}"}) as socket:
        assert socket.receive_json() == {"type": "revoked"}
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/v1/device/ws", headers={"Authorization": "Bearer short"}):
            pass
    assert refused.value.code == 1008


def test_failed_device_authentication_cannot_lock_out_paired_phone(settings, clock):
    with TestClient(create_app(replace(settings, claim_rate=2), clock)) as client:
        _, claim = pair(client)
        for _ in range(5):
            with client.websocket_connect("/v1/device/ws", headers={"Authorization": f"Bearer {'b' * 43}"}) as socket:
                try:
                    socket.receive_json()
                except WebSocketDisconnect:
                    pass
        with connected(client, claim) as socket:
            assert client.get(f"/v1/devices/{claim['device_id']}", headers=ADMIN).json()["online"] is True


def test_claimed_pairing_revocation_closes_device(client):
    pairing, claim = pair(client)
    with connected(client, claim) as socket:
        assert client.delete(f"/v1/pairings/{pairing['id']}", headers=ADMIN).status_code == 204
        assert socket.receive_json() == {"type": "revoked"}
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4401


@pytest.mark.parametrize("frame", [{"type": "result", "id": "unknown", "ok": True},
                                  {"type": "hello", "version": "0.1.0", "capabilities": [], "permissions": {}, "device_id": "forged"},
                                  {"type": "arbitrary"}, {"type": "event", "id": "a", "name": "n", "data": {}, "time": True}])
def test_invalid_or_uncorrelated_frames_are_refused(client, frame):
    _, claim = pair(client)
    with connected(client, claim) as socket:
        socket.send_json(frame)
        assert socket.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()


def test_binary_and_oversized_frames_are_refused(client, settings):
    _, claim = pair(client)
    with connected(client, claim) as socket:
        socket.send_bytes(b"{}")
        assert socket.receive_json()["code"] == "INVALID_FRAME"
    with connected(client, claim) as socket:
        socket.send_text("x" * (settings.max_frame_bytes + 1))
        assert socket.receive_json()["code"] == "TOO_LARGE"


def test_device_must_hello_before_events(client):
    _, claim = pair(client)
    with client.websocket_connect("/v1/device/ws", headers={"Authorization": f"Bearer {claim['device_token']}"}) as socket:
        socket.send_json({"type": "event", "id": "early", "name": "notification", "data": {}, "time": 0})
        assert socket.receive_json()["code"] == "INVALID_FRAME"


def test_durable_offline_command_survives_server_restart(settings, clock):
    first = create_app(settings, clock)
    with TestClient(first) as client:
        _, claim = pair(client)
        with connected(client, claim):
            pass
        command = client.post(f"/v1/devices/{claim['device_id']}/commands", headers=ADMIN,
                              json={"op": "device.info"}).json()
    second = create_app(settings, clock)
    with TestClient(second) as client:
        with client.websocket_connect("/v1/device/ws", headers={"Authorization": f"Bearer {claim['device_token']}"}) as socket:
            socket.send_json({"type": "hello", "version": "0.1.0", "capabilities": ["device.info"], "permissions": {}})
            assert socket.receive_json()["id"] == command["id"]
            socket.send_json({"type": "result", "id": command["id"], "ok": True, "data": {}})
            event_barrier(socket)
            assert client.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()["status"] == "succeeded"
