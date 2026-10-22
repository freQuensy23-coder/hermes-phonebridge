import base64
import json
import sqlite3
from contextlib import asynccontextmanager
from time import monotonic

import anyio
import pytest
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from phonebridge_server.app import create_app
from phonebridge_server.protocol import BridgeError, CommandRequest, HelloFrame, ResultFrame, encode_json

from debug_test_support import (
    DEBUG_SESSION, SCREEN_OPERATIONS, DeadlineWebSocketSession, PrimaryPreservingTestClient,
    WebSocketReceiveTimeout, connected, screenshot_data,
)
from phonebridge_test_support import ADMIN, pair, provision


class _ServiceFailureCapture:
    def __init__(self, node, client):
        self.node = node
        self.client = client

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        if item is self.node and call.when in ("setup", "call") and call.excinfo is not None:
            self.client.primary_error = call.excinfo.value


@pytest.fixture
def service(settings, clock, request):
    application = create_app(settings, clock)
    with PrimaryPreservingTestClient(application) as client:
        capture = _ServiceFailureCapture(request.node, client)
        request.config.pluginmanager.register(capture)
        try:
            yield application, client
        finally:
            request.config.pluginmanager.unregister(capture)


def test_mock_phone_missing_message_deadline_cleans_up_socket_and_lifespan(settings, clock):
    app = create_app(settings, clock)
    client = PrimaryPreservingTestClient(app)
    deadline = None
    started = monotonic()
    with pytest.raises(WebSocketReceiveTimeout, match="WebSocket receive deadline") as failure:
        with client:
            _, claim = pair(client)
            with connected(client, claim, capabilities=["debug.status"]) as phone:
                phone.receive_timeout = 0.05
                with pytest.raises(WebSocketReceiveTimeout) as expired:
                    phone.receive_json()
                deadline = expired.value
                assert isinstance(deadline.__cause__, TimeoutError)
                phone.receive_timeout = 5
                barrier(phone, "after-cancelled-receive")
                raise deadline
    assert deadline is not None
    assert failure.value is deadline
    assert monotonic() - started < 10
    assert client.portal is None
    assert not app.state.hub.peers
    assert not app.state.hub.devices
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        app.state.store.connection.execute("SELECT 1")

@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_mock_phone_socket_cleanup_preserves_primary_assertion(settings, clock, cleanup_fails):
    def fail_cleanup():
        raise RuntimeError("secondary socket cleanup")

    app = create_app(settings, clock)
    client = PrimaryPreservingTestClient(app)
    primary = AssertionError("primary debug assertion")
    started = monotonic()
    with pytest.raises(AssertionError, match="primary debug assertion") as failure:
        with client:
            _, claim = pair(client)
            with connected(client, claim, capabilities=["debug.status"]) as phone:
                if cleanup_fails:
                    phone.exit_stack.callback(fail_cleanup)
                raise primary
    assert failure.value is primary
    if cleanup_fails:
        assert primary.__notes__ == ["WebSocket cleanup also failed: RuntimeError('secondary socket cleanup')"]
    assert monotonic() - started < 10
    assert client.portal is None
    assert not app.state.hub.peers
    assert not app.state.hub.devices
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        app.state.store.connection.execute("SELECT 1")


def failing_lifespan(app, secondary):
    lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def context(application):
        async with lifespan(application):
            yield
        raise secondary

    app.router.lifespan_context = context

@pytest.mark.parametrize("primary_kind", ["assertion", "deadline"])
@pytest.mark.parametrize("cleanup_boundary", ["client", "lifespan"])
def test_mock_phone_outer_cleanup_preserves_primary(settings, clock, primary_kind, cleanup_boundary):
    secondary = RuntimeError("secondary outer cleanup")

    def fail_cleanup():
        raise secondary

    app = create_app(settings, clock)
    if cleanup_boundary == "lifespan":
        failing_lifespan(app, secondary)
    client = PrimaryPreservingTestClient(app)
    primary = AssertionError("primary debug assertion") if primary_kind == "assertion" else None
    with pytest.raises(AssertionError) as failure:
        with client:
            _, claim = pair(client)
            with connected(client, claim, capabilities=["debug.status"]) as phone:
                if cleanup_boundary == "client":
                    client.exit_stack.callback(fail_cleanup)
                if primary_kind == "deadline":
                    phone.receive_timeout = 0.05
                    with pytest.raises(WebSocketReceiveTimeout) as expired:
                        phone.receive_json()
                    primary = expired.value
                raise primary
    assert primary is not None
    assert failure.value is primary
    assert primary.__notes__ == ["TestClient cleanup also failed: RuntimeError('secondary outer cleanup')"]
    assert client.portal is None
    assert client.task.done()
    assert not app.state.hub.peers
    assert not app.state.hub.devices
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        app.state.store.connection.execute("SELECT 1")


@pytest.mark.parametrize("primary_kind", ["assertion", "deadline"])
def test_mock_phone_fixture_report_preserves_primary_during_client_cleanup(settings, clock, request, primary_kind):
    secondary = RuntimeError("secondary fixture cleanup")
    primary = AssertionError("primary fixture assertion") if primary_kind == "assertion" else None

    def fail_cleanup():
        raise secondary

    app = create_app(settings, clock)
    client = PrimaryPreservingTestClient(app)
    with client:
        _, claim = pair(client)
        client.exit_stack.callback(fail_cleanup)
        capture = _ServiceFailureCapture(request.node, client)
        request.config.pluginmanager.register(capture)
        try:
            def failing_call():
                nonlocal primary
                with connected(client, claim, capabilities=["debug.status"]) as phone:
                    if primary_kind == "deadline":
                        phone.receive_timeout = 0.05
                        with pytest.raises(WebSocketReceiveTimeout) as expired:
                            phone.receive_json()
                        primary = expired.value
                    raise primary

            call = pytest.CallInfo.from_call(failing_call, when="call")
            report = request.node.ihook.pytest_runtest_makereport(item=request.node, call=call)
            assert primary is not None
            assert call.excinfo is not None
            assert call.excinfo.value is primary
            assert client.primary_error is primary
            assert report.failed
        finally:
            request.config.pluginmanager.unregister(capture)
    assert client.primary_error is primary
    assert primary.__notes__ == ["TestClient cleanup also failed: RuntimeError('secondary fixture cleanup')"]
    assert client.portal is None
    assert client.task.done()
    assert not app.state.hub.peers
    assert not app.state.hub.devices
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        app.state.store.connection.execute("SELECT 1")

@pytest.mark.parametrize("cleanup_boundary", ["socket", "client", "lifespan"])
def test_mock_phone_cleanup_without_primary_still_fails(settings, clock, cleanup_boundary):
    secondary = RuntimeError("unaccompanied cleanup failure")

    def fail_cleanup():
        raise secondary

    app = create_app(settings, clock)
    if cleanup_boundary == "lifespan":
        failing_lifespan(app, secondary)
    client = PrimaryPreservingTestClient(app)
    with pytest.raises(RuntimeError, match="unaccompanied cleanup failure") as failure:
        with client:
            _, claim = pair(client)
            with connected(client, claim, capabilities=["debug.status"]) as phone:
                if cleanup_boundary == "socket":
                    phone.exit_stack.callback(fail_cleanup)
                elif cleanup_boundary == "client":
                    client.exit_stack.callback(fail_cleanup)
    assert failure.value is secondary
    assert client.portal is None
    assert client.task.done()
    assert not app.state.hub.peers
    assert not app.state.hub.devices
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        app.state.store.connection.execute("SELECT 1")

@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_mock_phone_withheld_accept_preserves_deadline_through_entry_cleanup(settings, clock, cleanup_fails):
    app = create_app(settings, clock)
    started = []
    finished = []
    secondary = RuntimeError("secondary failed accept cleanup")

    async def withheld_accept(scope, receive, send):
        if scope["type"] != "websocket" or scope["path"] != "/withheld-accept":
            await app(scope, receive, send)
            return
        started.append(scope["path"])
        try:
            assert await receive() == {"type": "websocket.connect"}
            await anyio.sleep_forever()
        finally:
            finished.append(scope["path"])
            if cleanup_fails:
                raise secondary

    class ObservedDeadlineSession(DeadlineWebSocketSession):
        def receive(self):
            try:
                return super().receive()
            except WebSocketReceiveTimeout as error:
                self.deadline = error
                raise

    client = PrimaryPreservingTestClient(withheld_accept)
    with client:
        _, claim = pair(client)
        session = ObservedDeadlineSession(client.websocket_connect("/withheld-accept"), receive_timeout=0.05)
        with pytest.raises(WebSocketReceiveTimeout, match="WebSocket receive deadline") as failure:
            with session:
                pytest.fail("withheld acceptance entered the socket body")
        assert failure.value is session.deadline
        assert isinstance(failure.value.__cause__, TimeoutError)
        if cleanup_fails:
            assert failure.value.__notes__ == [
                "WebSocket cleanup also failed: RuntimeError('secondary failed accept cleanup')"
            ]
        assert started == ["/withheld-accept"]
        assert finished == started
        assert session._send_rx.statistics().tasks_waiting_receive == 0
        assert session.portal is client.portal
        with connected(client, claim, capabilities=["debug.status"]) as phone:
            barrier(phone, "after-failed-accept")
    assert client.portal is None
    assert client.task.done()
    assert not app.state.hub.peers
    assert not app.state.hub.devices
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        app.state.store.connection.execute("SELECT 1")


def barrier(phone, event_id="debug-barrier"):
    phone.send_json({"type": "event", "id": event_id, "name": "mock.barrier", "data": {}, "time": 1})
    assert phone.receive_json() == {"type": "ack", "id": event_id}

@pytest.mark.parametrize("data", [
    {"enabled": False, "experimental": True, "remaining_seconds": 0, "debug_session": None, "screenshot_supported": False},
    {"enabled": True, "experimental": True, "remaining_seconds": 600, "debug_session": DEBUG_SESSION, "screenshot_supported": True},
    {"enabled": True, "experimental": True, "remaining_seconds": None, "debug_session": DEBUG_SESSION, "screenshot_supported": True},
])
def test_mock_phone_debug_status_http_ws_relay(client, data):
    _, claim = pair(client)
    with connected(client, claim, capabilities=["debug.status"]) as phone:
        path = f"/v1/devices/{claim['device_id']}/commands"
        request = {"op": "debug.status", "args": {}, "idempotency_key": "debug-status"}
        response = client.post(path, headers=ADMIN, json=request)
        assert response.status_code == 200
        command = response.json()
        assert client.post(path, headers=ADMIN, json=request).json() == command
        assert phone.receive_json() == {"type": "command", "id": command["id"], "op": "debug.status",
                                        "args": {}, "expires_at": command["expires_at"]}
        result = {"type": "result", "id": command["id"], "ok": True, "data": data}
        phone.send_json(result)
        phone.send_json(result)
        barrier(phone)
        response = client.get(f"/v1/commands/{command['id']}", headers=ADMIN)
        assert response.status_code == 200
        assert response.json() == {"id": command["id"], "device_id": claim["device_id"], "op": "debug.status",
                                   "status": "succeeded", "expires_at": command["expires_at"], "data": data, "error": None}
        assert response.headers["cache-control"] == "no-store"

@pytest.mark.parametrize("op", ["debug.enable", "debug.renew", "debug.disable", "missing.operation", "debug.status"])
def test_mock_phone_unadvertised_debug_commands_never_queue(service, op):
    app, client = service
    _, claim = pair(client)
    with connected(client, claim, capabilities=["device.info"]):
        response = client.post(f"/v1/devices/{claim['device_id']}/commands", headers=ADMIN, json={"op": op})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "UNSUPPORTED"
        assert app.state.store.connection.execute("SELECT count(*) FROM commands").fetchone()[0] == 0


def test_mock_phone_debug_auth_missing_operation_and_no_enable_endpoint(service):
    app, client = service
    _, claim = pair(client)
    path = f"/v1/devices/{claim['device_id']}/commands"
    with connected(client, claim, capabilities=["debug.status"]):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer " + claim["device_token"]}):
            assert client.post(path, headers=headers, json={"op": "debug.status"}).status_code == 401
        response = client.post(path, headers=ADMIN, json={"args": {}})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        for op in ("enable", "renew"):
            assert client.post(f"/v1/devices/{claim['device_id']}/debug/{op}", headers=ADMIN, json={}).status_code == 404
        assert app.state.store.connection.execute("SELECT count(*) FROM commands").fetchone()[0] == 0

@pytest.mark.parametrize("op", ["debug.enable", "debug.renew"])
def test_remote_consent_operations_cannot_be_advertised(op):
    assert HelloFrame(type="hello", version="0.1.0", capabilities=[op, "debug.status"], permissions={}).capabilities == ["debug.status"]

@pytest.mark.parametrize("op", SCREEN_OPERATIONS)
@pytest.mark.parametrize("args", [{}, {"debug_session": None}, {"debug_session": ""}, {"debug_session": 7},
                                  {"debug_session": "x" * 129}, {"debug_session": "bad session"}])
def test_screen_commands_require_bounded_session_before_enqueue(service, op, args):
    app, client = service
    _, claim = pair(client)
    with connected(client, claim, capabilities=SCREEN_OPERATIONS):
        response = client.post(f"/v1/devices/{claim['device_id']}/commands", headers=ADMIN, json={"op": op, "args": args})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        assert app.state.store.connection.execute("SELECT count(*) FROM commands").fetchone()[0] == 0

@pytest.mark.parametrize("op", SCREEN_OPERATIONS)
def test_mock_phone_is_authority_for_current_screen_session(client, op):
    _, claim = pair(client)
    with connected(client, claim, capabilities=SCREEN_OPERATIONS) as phone:
        args = {"debug_session": "stale-but-syntactic"}
        response = client.post(f"/v1/devices/{claim['device_id']}/commands", headers=ADMIN, json={"op": op, "args": args})
        assert response.status_code == 200
        command = response.json()
        frame = phone.receive_json()
        assert frame["id"] == command["id"]
        assert frame["args"] == args
        phone.send_json({"type": "result", "id": command["id"], "ok": False,
                         "error": {"code": "PERMISSION_REQUIRED", "message": "Session is not current"}})
        barrier(phone)
        result = client.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()
        assert result["status"] == "failed"
        assert result["error"]["code"] == "PERMISSION_REQUIRED"


def test_mock_phone_large_screenshot_exact_http_readback_restart_and_terminal_conflict(settings, clock):
    data = screenshot_data()
    data["padding"] = ""
    data["padding"] = "x" * (384 * 1024 - len(encode_json(data).encode()))
    assert len(data["base64"]) == 327680
    assert len(base64.b64decode(data["base64"], validate=True)) == 240 * 1024
    assert len(encode_json(data).encode()) == 384 * 1024
    app = create_app(settings, clock)
    with PrimaryPreservingTestClient(app) as client:
        _, claim = pair(client)
        with connected(client, claim, capabilities=["screen.screenshot"]) as phone:
            command = client.post(f"/v1/devices/{claim['device_id']}/commands", headers=ADMIN,
                                  json={"op": "screen.screenshot", "args": {"debug_session": DEBUG_SESSION}}).json()
            assert phone.receive_json()["id"] == command["id"]
            result = {"type": "result", "id": command["id"], "ok": True, "data": data}
            phone.send_json(result)
            phone.send_json(result)
            barrier(phone)
            response = client.get(f"/v1/commands/{command['id']}", headers=ADMIN)
            assert response.status_code == 200
            assert response.json()["status"] == "succeeded"
            assert response.json()["data"] == data
            assert len(response.content) <= settings.max_frame_bytes
            assert client.get(f"/v1/commands/{command['id']}").status_code == 401
            phone.send_json({**result, "data": {**data, "width": 2}})
            assert phone.receive_json()["code"] == "RESULT_CONFLICT"
    with PrimaryPreservingTestClient(create_app(settings, clock)) as restarted:
        result = restarted.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()
        assert result["status"] == "succeeded"
        assert result["data"] == data

@pytest.mark.parametrize("field,value", [
    ("mime", "image/png"), ("base64", "not base64!"), ("base64", ""),
    ("base64", "AB=="), ("base64", "YQ==\n"), ("base64", 123), ("width", True), ("height", 0),
    ("base64", "YQ=="), ("base64", "\u00e9"), ("width", 1281),
])
def test_mock_phone_invalid_screenshot_is_not_stored(client, field, value):
    _, claim = pair(client)
    with connected(client, claim, capabilities=["screen.screenshot"]) as phone:
        command = client.post(f"/v1/devices/{claim['device_id']}/commands", headers=ADMIN,
                              json={"op": "screen.screenshot", "args": {"debug_session": DEBUG_SESSION}}).json()
        assert phone.receive_json()["id"] == command["id"]
        phone.send_json({"type": "result", "id": command["id"], "ok": True, "data": {**screenshot_data(256), field: value}})
        assert phone.receive_json()["code"] == "INVALID_RESULT"
        result = client.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()
        assert result["status"] == "sent"
        assert result["data"] is None

@pytest.mark.parametrize("data", [None, {}, {"mime": "image/jpeg", "width": 1, "height": 1}])
def test_mock_phone_missing_screenshot_payload_is_not_stored(client, data):
    _, claim = pair(client)
    with connected(client, claim, capabilities=["screen.screenshot"]) as phone:
        command = client.post(f"/v1/devices/{claim['device_id']}/commands", headers=ADMIN,
                              json={"op": "screen.screenshot", "args": {"debug_session": DEBUG_SESSION}}).json()
        assert phone.receive_json()["id"] == command["id"]
        phone.send_json({"type": "result", "id": command["id"], "ok": True, "data": data})
        assert phone.receive_json()["code"] == "INVALID_RESULT"
        assert client.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()["data"] is None


def test_result_data_budget_exact_boundary():
    data = {"a": "x" * 196600, "b": "y" * 196601}
    assert len(encode_json(data).encode()) == 384 * 1024
    assert ResultFrame(type="result", id="boundary", ok=True, data=data).data == data
    with pytest.raises(ValidationError):
        ResultFrame(type="result", id="boundary", ok=True, data={**data, "b": data["b"] + "y"})
    with pytest.raises(ValidationError):
        ResultFrame(type="result", id="boundary", ok=True, data={"base64": "A" * 327681})


def test_mock_phone_oversized_jpeg_does_not_store(client):
    _, claim = pair(client)
    with connected(client, claim, capabilities=["screen.screenshot"]) as phone:
        command = client.post(f"/v1/devices/{claim['device_id']}/commands", headers=ADMIN,
                              json={"op": "screen.screenshot", "args": {"debug_session": DEBUG_SESSION}}).json()
        assert phone.receive_json()["id"] == command["id"]
        phone.send_json({"type": "result", "id": command["id"], "ok": True, "data": screenshot_data(240 * 1024 + 1)})
        assert phone.receive_json()["code"] == "INVALID_FRAME"
        assert client.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()["data"] is None

@pytest.mark.parametrize("kind", ["event", "hello", "command"])
def test_mock_phone_existing_incoming_limits_remain_strict(client, kind):
    _, claim = pair(client)
    with connected(client, claim, capabilities=["debug.status"]) as phone:
        if kind == "event":
            frame = {"type": "event", "id": "oversized", "name": "mock.event", "time": 1, "data": {"text": "x" * 16385}}
        elif kind == "hello":
            frame = {"type": "hello", "version": "0.1.0", "capabilities": ["debug.status"] * 101, "permissions": {}}
        else:
            frame = {"type": "command", "id": "incoming", "op": "debug.status", "args": {}}
        phone.send_text(json.dumps(frame))
        assert phone.receive_json()["code"] == "INVALID_FRAME"
        with pytest.raises(WebSocketDisconnect):
            phone.receive_json()
        events = client.get(f"/v1/devices/{claim['device_id']}/events", headers=ADMIN).json()["events"]
        assert "oversized" not in {event["id"] for event in events}


def test_mock_phone_large_commands_still_rejected(service, settings):
    app, client = service
    _, claim = pair(client)
    with connected(client, claim, capabilities=["debug.status"]):
        path = f"/v1/devices/{claim['device_id']}/commands"
        for args in ({"text": "x" * 16385}, {"a": "x" * 16380, "b": "y" * 16380, "c": "z" * 100}):
            response = client.post(path, headers=ADMIN, json={"op": "debug.status", "args": args})
            assert response.status_code == 422
        assert client.post(path, headers=ADMIN, json={"op": "debug.status", "args": {"text": "x" * settings.max_body_bytes}}).status_code == 413
        assert app.state.store.connection.execute("SELECT count(*) FROM commands").fetchone()[0] == 0


def test_mock_phone_cross_device_screenshot_cannot_change_owner(client):
    _, owner_claim = pair(client)
    _, attacker_claim = pair(client)
    with connected(client, owner_claim, capabilities=["screen.screenshot"]) as owner:
        with connected(client, attacker_claim, capabilities=["screen.screenshot"]) as attacker:
            command = client.post(f"/v1/devices/{owner_claim['device_id']}/commands", headers=ADMIN,
                                  json={"op": "screen.screenshot", "args": {"debug_session": DEBUG_SESSION}}).json()
            assert owner.receive_json()["id"] == command["id"]
            frame = {"type": "result", "id": command["id"], "ok": True, "data": screenshot_data()}
            attacker.send_json(frame)
            assert attacker.receive_json()["code"] == "RESULT_OWNERSHIP"
            assert client.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()["data"] is None
            owner.send_json(frame)
            barrier(owner)
            assert client.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()["data"] == frame["data"]


def test_debug_expiry_and_revoked_device_results_remain_fail_closed(store_factory, clock):
    store = store_factory()
    _, _, device_id, session = provision(store, capabilities=["debug.status"])
    command = store.enqueue(device_id, CommandRequest(op="debug.status", ttl_seconds=1))
    store.dispatch(device_id, session, command["id"])
    clock.advance(1)
    store.result(device_id, session, ResultFrame(type="result", id=command["id"], ok=True, data={}))
    assert store.command(command["id"])["status"] == "expired"
    assert store.command(command["id"])["data"] is None
    next_command = store.enqueue(device_id, CommandRequest(op="debug.status"))
    store.revoke_device(device_id)
    with pytest.raises(BridgeError):
        store.result(device_id, session, ResultFrame(type="result", id=next_command["id"], ok=True, data={}))
    assert store.command(next_command["id"])["error"]["code"] == "DEVICE_REVOKED"


def test_configured_result_storage_limit_still_applies(store_factory):
    store = store_factory(max_json_bytes=32768)
    _, _, device_id, session = provision(store, capabilities=["screen.screenshot"])
    command = store.enqueue(device_id, CommandRequest(op="screen.screenshot", args={"debug_session": DEBUG_SESSION}))
    store.dispatch(device_id, session, command["id"])
    with pytest.raises(BridgeError) as oversized:
        store.result(device_id, session, ResultFrame(type="result", id=command["id"], ok=True, data=screenshot_data()))
    assert oversized.value.code == "VALIDATION_ERROR"
    assert store.command(command["id"])["data"] is None


def test_mock_phone_over_budget_result_is_not_stored(client):
    _, claim = pair(client)
    data = screenshot_data()
    data["padding"] = ""
    data["padding"] = "x" * (384 * 1024 - len(encode_json(data).encode()) + 1)
    with connected(client, claim, capabilities=["screen.screenshot"]) as phone:
        command = client.post(f"/v1/devices/{claim['device_id']}/commands", headers=ADMIN,
                              json={"op": "screen.screenshot", "args": {"debug_session": DEBUG_SESSION}}).json()
        assert phone.receive_json()["id"] == command["id"]
        phone.send_json({"type": "result", "id": command["id"], "ok": True, "data": data})
        assert phone.receive_json()["code"] == "INVALID_FRAME"
        readback = client.get(f"/v1/commands/{command['id']}", headers=ADMIN).json()
        assert readback["status"] == "sent"
        assert readback["data"] is None


def test_other_operations_preserve_opaque_phone_data(store_factory):
    store = store_factory()
    _, _, device_id, session = provision(store)
    command = store.enqueue(device_id, CommandRequest(op="device.info"))
    store.dispatch(device_id, session, command["id"])
    data = {"mime": "application/unknown", "base64": "opaque", "width": -1,
            "phone_specific": {"anything": [None, True, 1, "untrusted"]}}
    store.result(device_id, session, ResultFrame(type="result", id=command["id"], ok=True, data=data))
    assert store.command(command["id"])["data"] == data


def test_result_budget_applies_after_server_normalization(store_factory):
    store = store_factory()
    _, _, device_id, session = provision(store)
    command = store.enqueue(device_id, CommandRequest(op="device.info"))
    store.dispatch(device_id, session, command["id"])
    data = {"outcome": "dispatched", "a": "x" * 196000, "b": ""}
    data["b"] = "y" * (384 * 1024 - len(encode_json(data).encode()))
    with pytest.raises(BridgeError) as oversized:
        store.result(device_id, session, ResultFrame(type="result", id=command["id"], ok=True, data=data))
    assert oversized.value.code == "VALIDATION_ERROR"
    assert store.command(command["id"])["data"] is None
