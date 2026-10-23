import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from starlette.websockets import WebSocket

from phonebridge_server.app import create_app
from phonebridge_server.protocol import BridgeError
from phonebridge_server.transport import Hub, Peer

from phonebridge_test_support import ADMIN


pytestmark = pytest.mark.asyncio
OLD_PORT = 41001


class Barrier:
    def __init__(self):
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self):
        self.reached.set()
        await self.release.wait()


class AsyncSocket:
    def __init__(self, application, token, port):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.accepted = asyncio.Event()
        self.closed = asyncio.Event()
        self.sent = []
        self.frame_reads = 0
        self.receives = 0
        self.incoming.put_nowait({"type": "websocket.connect"})
        scope = {
            "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "scheme": "wss", "path": "/v1/device/ws", "raw_path": b"/v1/device/ws",
            "root_path": "", "query_string": b"", "subprotocols": [],
            "headers": [(b"host", b"phone.example.test"),
                        (b"authorization", f"Bearer {token}".encode())],
            "client": ("127.0.0.1", port), "server": ("phone.example.test", 443),
        }
        self.task = asyncio.create_task(application(scope, self.receive, self.send))

    async def receive(self):
        self.receives += 1
        if self.receives > 1:
            self.frame_reads += 1
        return await self.incoming.get()

    async def send(self, message):
        self.sent.append(message)
        if message["type"] == "websocket.accept":
            self.accepted.set()
        else:
            self.outgoing.put_nowait(message)
        if message["type"] == "websocket.close":
            self.closed.set()
            self.incoming.put_nowait({"type": "websocket.disconnect", "code": message["code"]})

    def send_json(self, value):
        self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(value)})

    async def receive_json(self):
        message = await asyncio.wait_for(self.outgoing.get(), 5)
        assert message["type"] == "websocket.send", message
        return json.loads(message["text"])

    async def ready(self, event_id):
        await asyncio.wait_for(self.accepted.wait(), 5)
        self.send_json({"type": "hello", "version": "0.1.0",
                        "capabilities": ["device.info"], "permissions": {}})
        await self.barrier(event_id)

    async def barrier(self, event_id):
        self.send_json({"type": "event", "id": event_id, "name": "test.ready", "data": {}, "time": 0})
        assert await self.receive_json() == {"type": "ack", "id": event_id}

    @property
    def close_codes(self):
        return [message["code"] for message in self.sent if message["type"] == "websocket.close"]


@asynccontextmanager
async def socket_group(application):
    sockets = []

    def start(claim, port):
        socket = AsyncSocket(application, claim["device_token"], port)
        sockets.append(socket)
        return socket

    try:
        yield start
    finally:
        for socket in sockets:
            if not socket.task.done():
                socket.task.cancel()
        await asyncio.gather(*(socket.task for socket in sockets), return_exceptions=True)


@pytest.fixture
async def race_service(settings, clock):
    application = create_app(replace(settings, send_timeout=30, hello_timeout=30), clock)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                     base_url="https://phone.example.test", timeout=5) as client:
            yield application, client


async def pair_async(client, name="Pixel"):
    response = await client.post("/v1/pairings", headers=ADMIN, json={"name": name})
    assert response.status_code == 200
    pairing = response.json()
    response = await client.post("/v1/pair/claim", json={"token": pairing["qr_payload"]["token"],
                                                       "name": name, "platform": "android"})
    assert response.status_code == 200
    return pairing, response.json()


def pause_handshake(application, monkeypatch, phase):
    barrier = Barrier()
    hub = application.state.hub
    if phase == "connect":
        original = hub.connect

        async def connect(token, peer, execute):
            result = await original(token, peer, execute)
            if peer.websocket.client.port == OLD_PORT:
                await barrier.wait()
            return result

        monkeypatch.setattr(hub, "connect", connect)
    elif phase == "accept":
        original = WebSocket.accept

        async def accept(websocket, *args, **kwargs):
            await original(websocket, *args, **kwargs)
            if websocket.client.port == OLD_PORT:
                await barrier.wait()

        monkeypatch.setattr(WebSocket, "accept", accept)
    else:
        original = hub.add

        async def add(device_id, session_id, peer, execute):
            if phase == "add" and peer.websocket.client.port == OLD_PORT:
                await barrier.wait()
            await original(device_id, session_id, peer, execute)
            if phase == "registered" and peer.websocket.client.port == OLD_PORT:
                await barrier.wait()

        monkeypatch.setattr(hub, "add", add)
    return barrier


@pytest.mark.parametrize("phase", ["connect", "add"])
async def test_stale_handshake_cannot_replace_newer_peer(race_service, monkeypatch, phase):
    application, client = race_service
    _, claim = await pair_async(client)
    gate = pause_handshake(application, monkeypatch, phase)
    hub = application.state.hub
    device_id = claim["device_id"]
    async with socket_group(application) as start:
        try:
            old = start(claim, OLD_PORT)
            await asyncio.wait_for(gate.reached.wait(), 5)
            old_session = hub.devices[device_id].current_session
            newer = start(claim, OLD_PORT + 1)
            await newer.ready("newer-ready")
            current_session = hub.devices[device_id].current_session
            current_peer = hub.peers[device_id]
            assert current_session != old_session
            assert current_peer.websocket.client.port == OLD_PORT + 1
            gate.release.set()
            await asyncio.wait_for(old.task, 5)
            assert old.close_codes == [1008]
            assert old.frame_reads == 0
            assert not newer.closed.is_set()
            assert hub.peers[device_id] is current_peer
            assert hub.devices[device_id].current_session == current_session
            await asyncio.to_thread(application.state.store.check_session, device_id, current_session)
            response = await client.get(f"/v1/devices/{device_id}", headers=ADMIN)
            assert response.json()["online"] is True
            response = await client.post(f"/v1/devices/{device_id}/commands", headers=ADMIN,
                                         json={"op": "device.info", "idempotency_key": "newer-command"})
            assert response.status_code == 200
            command = response.json()
            frame = await newer.receive_json()
            assert frame == {"type": "command", "id": command["id"], "op": "device.info",
                             "args": {}, "expires_at": command["expires_at"]}
            newer.send_json({"type": "result", "id": command["id"], "ok": True, "data": {"newer": True}})
            await newer.barrier("after-stale-cleanup")
            response = await client.get(f"/v1/commands/{command['id']}", headers=ADMIN)
            assert response.json()["status"] == "succeeded"
            assert response.json()["data"] == {"newer": True}
        finally:
            gate.release.set()
    assert hub.peers == {}
    assert hub.devices == {}


@pytest.mark.parametrize("target", ["device", "pairing"])
@pytest.mark.parametrize("phase", ["connect", "accept", "add", "registered"])
async def test_revocation_closes_pending_handshake_before_release(race_service, monkeypatch, target, phase):
    application, client = race_service
    pairing, claim = await pair_async(client)
    gate = pause_handshake(application, monkeypatch, phase)
    hub = application.state.hub
    device_id = claim["device_id"]
    path = f"/v1/devices/{device_id}" if target == "device" else f"/v1/pairings/{pairing['id']}"
    async with socket_group(application) as start:
        try:
            pending = start(claim, OLD_PORT)
            await asyncio.wait_for(gate.reached.wait(), 5)
            assert pending.accepted.is_set()
            assert pending.frame_reads == 0
            response = await asyncio.wait_for(client.delete(path, headers=ADMIN), 5)
            assert response.status_code == 204
            assert pending.closed.is_set() is (phase != "accept")
            assert device_id not in hub.peers
            if phase != "accept":
                assert pending.close_codes == [4401]
                assert hub.devices[device_id].current_session is None
            gate.release.set()
            await asyncio.wait_for(pending.task, 5)
            assert pending.frame_reads == 0
            assert pending.close_codes == [4401]
            assert await pending.receive_json() == {"type": "revoked"}
            assert (await client.get(f"/v1/devices/{device_id}", headers=ADMIN)).status_code == 404
            if target == "pairing":
                response = await client.get(path, headers=ADMIN)
                assert response.json()["status"] == "revoked"
            reconnect = start(claim, OLD_PORT + 1)
            await asyncio.wait_for(reconnect.task, 5)
            assert await reconnect.receive_json() == {"type": "revoked"}
            assert reconnect.close_codes == [4401]
        finally:
            gate.release.set()
    assert hub.peers == {}
    assert hub.devices == {}


@pytest.mark.parametrize("target", ["device", "pairing"])
async def test_revocation_closes_registered_and_pending_same_device(race_service, monkeypatch, target):
    application, client = race_service
    pairing, claim = await pair_async(client)
    hub = application.state.hub
    gate = pause_handshake(application, monkeypatch, "accept")
    device_id = claim["device_id"]
    path = f"/v1/devices/{device_id}" if target == "device" else f"/v1/pairings/{pairing['id']}"
    sender_gate = Barrier()
    original_send = Peer.send

    async def send(peer, value):
        await original_send(peer, value)
        if peer.websocket.client.port == OLD_PORT + 1 and value == {"type": "ack", "id": "registered-ready"}:
            await sender_gate.wait()

    monkeypatch.setattr(Peer, "send", send)
    async with socket_group(application) as start:
        try:
            registered = start(claim, OLD_PORT + 1)
            await registered.ready("registered-ready")
            await asyncio.wait_for(sender_gate.reached.wait(), 5)
            pending = start(claim, OLD_PORT)
            await asyncio.wait_for(gate.reached.wait(), 5)
            assert not registered.closed.is_set()
            assert not pending.closed.is_set()
            response = await asyncio.wait_for(client.delete(path, headers=ADMIN), 5)
            assert response.status_code == 204
            assert registered.closed.is_set()
            assert registered.close_codes == [4401]
            gate.release.set()
            await asyncio.wait_for(pending.task, 5)
            await asyncio.wait_for(registered.task, 5)
            assert pending.close_codes == [4401]
            assert pending.frame_reads == 0
            assert hub.peers == {}
        finally:
            gate.release.set()
            sender_gate.release.set()
    assert hub.devices == {}


async def test_coordination_entries_are_bounded_and_reclaimed(settings):
    hub = Hub(replace(settings, max_connections=1, max_http_requests=1))
    limit = hub.settings.max_connections + hub.settings.max_http_requests + 16
    contexts = []
    try:
        for index in range(limit):
            context = hub.coordinate(str(index))
            await context.__aenter__()
            contexts.append(context)
        assert len(hub.devices) == limit
        with pytest.raises(BridgeError) as full:
            async with hub.coordinate("overflow"):
                raise AssertionError("Coordination must stay bounded")
        assert full.value.code == "QUEUE_FULL"
    finally:
        for context in reversed(contexts):
            await context.__aexit__(None, None, None)
    assert hub.devices == {}
    async with hub.coordinate("reused"):
        assert len(hub.devices) == 1
    assert hub.devices == {}


@pytest.mark.parametrize("phase", ["add", "registered"])
@pytest.mark.parametrize("change", ["replace", "revoke"])
async def test_registration_rechecks_durable_authorization(race_service, monkeypatch, phase, change):
    application, client = race_service
    _, claim = await pair_async(client)
    device_id = claim["device_id"]
    store = application.state.store
    gate = pause_handshake(application, monkeypatch, phase)
    async with socket_group(application) as start:
        try:
            pending = start(claim, OLD_PORT)
            await asyncio.wait_for(gate.reached.wait(), 5)
            assert pending.accepted.is_set()
            if change == "replace":
                _, current_session = await asyncio.to_thread(store.connect, claim["device_token"])
            else:
                await asyncio.to_thread(store.revoke_device, device_id)
            gate.release.set()
            await asyncio.wait_for(pending.task, 5)
            assert pending.close_codes == ([1008] if change == "replace" else [4401])
            assert pending.frame_reads == 0
            assert application.state.hub.peers == {}
            if change == "replace":
                await asyncio.to_thread(store.check_session, device_id, current_session)
                await asyncio.to_thread(store.disconnect, device_id, current_session)
            else:
                assert (await client.get(f"/v1/devices/{device_id}", headers=ADMIN)).status_code == 404
        finally:
            gate.release.set()
    assert application.state.hub.devices == {}


async def test_slow_replacement_close_does_not_hold_coordination(race_service, monkeypatch):
    application, client = race_service
    _, first_claim = await pair_async(client)
    _, second_claim = await pair_async(client, "Samsung")
    close_gate = Barrier()
    original_close = WebSocket.close
    revoke_task = None

    async def close(websocket, *args, **kwargs):
        if websocket.client.port == OLD_PORT:
            await close_gate.wait()
        await original_close(websocket, *args, **kwargs)

    monkeypatch.setattr(WebSocket, "close", close)
    async with socket_group(application) as start:
        try:
            old = start(first_claim, OLD_PORT)
            await old.ready("old-ready")
            replacement = start(first_claim, OLD_PORT + 1)
            await asyncio.wait_for(close_gate.reached.wait(), 5)
            assert replacement.accepted.is_set()
            unrelated = start(second_claim, OLD_PORT + 2)
            await unrelated.ready("unrelated-ready")
            response = await asyncio.wait_for(
                client.delete(f"/v1/devices/{second_claim['device_id']}", headers=ADMIN), 5,
            )
            assert response.status_code == 204
            assert unrelated.closed.is_set()
            revoke_task = asyncio.create_task(
                client.delete(f"/v1/devices/{first_claim['device_id']}", headers=ADMIN),
            )
            await asyncio.wait_for(replacement.closed.wait(), 5)
            assert replacement.close_codes == [4401]
            assert not close_gate.release.is_set()
            close_gate.release.set()
            assert (await asyncio.wait_for(revoke_task, 5)).status_code == 204
            await asyncio.wait_for(old.task, 5)
            await asyncio.wait_for(replacement.task, 5)
            assert replacement.frame_reads == 0
        finally:
            close_gate.release.set()
            if revoke_task is not None and not revoke_task.done():
                revoke_task.cancel()
                await asyncio.gather(revoke_task, return_exceptions=True)
    assert application.state.hub.peers == {}
    assert application.state.hub.devices == {}


async def test_cancelled_pending_handshake_reclaims_session_identity(race_service, monkeypatch):
    application, client = race_service
    _, claim = await pair_async(client)
    gate = pause_handshake(application, monkeypatch, "connect")
    async with socket_group(application) as start:
        try:
            pending = start(claim, OLD_PORT)
            await asyncio.wait_for(gate.reached.wait(), 5)
            session_id = application.state.hub.devices[claim["device_id"]].current_session
            pending.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending.task
            assert application.state.hub.peers == {}
            assert application.state.hub.devices == {}
            with pytest.raises(BridgeError) as disconnected:
                await asyncio.to_thread(application.state.store.check_session, claim["device_id"], session_id)
            assert disconnected.value.code == "AUTH_REVOKED"
        finally:
            gate.release.set()
