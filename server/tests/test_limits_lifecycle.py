import asyncio
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from phonebridge_server.app import create_app
from phonebridge_server.protocol import BridgeError, CommandRequest, EventFrame, PairingRequest, ResultFrame

from debug_test_support import DEBUG_SESSION, screenshot_data
from phonebridge_test_support import ADMIN, connected, pair, provision


pytestmark = pytest.mark.asyncio


async def test_pairing_and_device_record_limits(store_factory):
    store = store_factory(max_pairings=1, max_devices=1)
    _, _, device_id, session = provision(store)
    with pytest.raises(BridgeError) as full:
        store.create_pairing(PairingRequest(name="Another"))
    assert full.value.code == "QUEUE_FULL"
    store.disconnect(device_id, session)


async def test_global_connection_limit(store_factory):
    store = store_factory(max_connections=1)
    _, first, device_id, session = provision(store)
    _, replacement = store.connect(first["device_token"])
    assert replacement != session
    with pytest.raises(BridgeError):
        store.touch(device_id, session)
    store.disconnect(device_id, replacement)
    _, second, _, _ = provision(store)
    with pytest.raises(BridgeError) as full:
        store.connect(first["device_token"])
    assert full.value.code == "QUEUE_FULL"
    assert second["device_id"] != first["device_id"]


async def test_same_event_id_is_scoped_to_device(store_factory):
    store = store_factory()
    _, _, first, session_a = provision(store)
    _, _, second, session_b = provision(store)
    frame = EventFrame(type="event", id="same-id", name="notification", data={}, time=1)
    store.event(first, session_a, frame)
    store.event(second, session_b, frame.model_copy(update={"data": {"second": True}}))
    assert store.events(first, 0, 50)["events"][0]["data"] == {}
    assert store.events(second, 0, 50)["events"][0]["data"] == {"second": True}


async def test_expired_results_cannot_change_terminal_status(store_factory, clock):
    store = store_factory()
    _, _, device_id, session = provision(store)
    command = store.enqueue(device_id, CommandRequest(op="device.info", ttl_seconds=1))
    store.dispatch(device_id, session, command["id"])
    clock.advance(1)
    with pytest.raises(BridgeError) as expired:
        store.result(device_id, session, ResultFrame(type="result", id=command["id"], ok=True, data={}))
    assert expired.value.code == "RESULT_UNCORRELATED"
    assert store.command(command["id"])["status"] == "expired"


async def test_screenshot_sized_result_is_bounded_and_preserved(store_factory):
    store = store_factory()
    _, _, device_id, session = provision(store, capabilities=["screen.screenshot"])
    command = store.enqueue(device_id, CommandRequest(op="screen.screenshot", args={"debug_session": DEBUG_SESSION}))
    store.dispatch(device_id, session, command["id"])
    data = screenshot_data()
    store.result(device_id, session, ResultFrame(type="result", id=command["id"], ok=True,
                                               data=data))
    assert store.command(command["id"])["data"] == data


async def test_hello_has_deadline(settings, clock):
    application = create_app(replace(settings, hello_timeout=0.02), clock)
    with TestClient(application) as client:
        _, claim = pair(client)
        with client.websocket_connect("/v1/device/ws", headers={"Authorization": f"Bearer {claim['device_token']}"}) as socket:
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 1008


async def test_frame_rate_is_bounded(settings, clock):
    application = create_app(replace(settings, websocket_rate=1), clock)
    with TestClient(application) as client:
        _, claim = pair(client)
        with connected(client, claim) as socket:
            socket.send_json({"type": "event", "id": "second", "name": "notification", "data": {}, "time": 0})
            assert socket.receive_json()["code"] == "RATE_LIMITED"
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()


async def test_idle_authorized_phone_keeps_persistent_connection(settings, clock):
    application = create_app(replace(settings, idle_timeout=0.02), clock)
    with TestClient(application) as client:
        _, claim = pair(client)
        with connected(client, claim) as socket:
            await asyncio.sleep(0.06)
            socket.send_json({"type": "event", "id": "after-idle", "name": "notification", "data": {}, "time": 0})
            assert socket.receive_json() == {"type": "ack", "id": "after-idle"}


async def test_external_worker_revocation_closes_active_socket(settings, clock):
    first = create_app(settings, clock)
    second = create_app(settings, clock)
    with TestClient(first) as first_client, TestClient(second) as second_client:
        _, claim = pair(first_client)
        with connected(first_client, claim) as socket:
            assert second_client.delete(f"/v1/devices/{claim['device_id']}", headers=ADMIN).status_code == 204
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    socket.receive_json()
            assert closed.value.code == 1008


async def test_event_receipt_survives_application_restart(settings, clock):
    first = create_app(settings, clock)
    frame = {"type": "event", "id": "persistent-event", "name": "notification", "data": {"text": "untrusted"}, "time": 1}
    with TestClient(first) as client:
        _, claim = pair(client)
        with connected(client, claim) as socket:
            socket.send_json(frame)
            assert socket.receive_json() == {"type": "ack", "id": frame["id"]}
            events = client.get(f"/v1/devices/{claim['device_id']}/events", headers=ADMIN).json()["events"]
            original = next(event for event in events if event["id"] == frame["id"])
    second = create_app(settings, clock)
    with TestClient(second) as client:
        with connected(client, claim) as socket:
            socket.send_text(json.dumps(frame))
            assert socket.receive_json() == {"type": "ack", "id": frame["id"]}
            events = client.get(f"/v1/devices/{claim['device_id']}/events", headers=ADMIN).json()["events"]
            matching = [event for event in events if event["id"] == frame["id"]]
            assert matching == [original]
