import asyncio
import json
import socket
import threading
import time
from dataclasses import replace

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from phonebridge_server.app import create_app

from phonebridge_test_support import ADMIN


pytestmark = pytest.mark.asyncio


@pytest.fixture
def live_server(settings):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(128)
    server_settings = replace(settings, public_url=f"http://127.0.0.1:{port}", allow_insecure=True)
    config = uvicorn.Config(create_app(server_settings), host="127.0.0.1", port=port,
                            access_log=False, log_config=None, ws="websockets-sansio",
                            ws_max_size=server_settings.max_frame_bytes, ws_max_queue=8, lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}/v1/device/ws"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()


async def receive(socket):
    return json.loads(await asyncio.wait_for(socket.recv(), 5))


async def test_real_http_websocket_pair_result_dedup_revocation(live_server):
    http_url, ws_url = live_server
    async with httpx.AsyncClient(base_url=http_url, timeout=5) as client:
        assert (await client.get("/healthz")).json()["version"] == "0.1.0"
        pairing = (await client.post("/v1/pairings", headers=ADMIN, json={"name": "CI phone"})).json()
        claim_response = await client.post("/v1/pair/claim", json={"token": pairing["qr_payload"]["token"],
                                                               "name": "CI phone", "platform": "android"})
        assert claim_response.status_code == 200
        claim = claim_response.json()
        assert claim["ws_url"] == ws_url
        async with connect(ws_url, additional_headers={"Authorization": f"Bearer {claim['device_token']}"},
                           max_size=65536, max_queue=32) as phone:
            await phone.send(json.dumps({"type": "hello", "version": "0.1.0", "capabilities": ["device.info"], "permissions": {}}))
            ready = {"type": "event", "id": "ready", "name": "network.ready", "data": {}, "time": 1}
            await phone.send(json.dumps(ready))
            assert await receive(phone) == {"type": "ack", "id": "ready"}
            command = (await client.post(f"/v1/devices/{claim['device_id']}/commands", headers=ADMIN,
                                         json={"op": "device.info", "idempotency_key": "network-command"})).json()
            assert (await receive(phone))["id"] == command["id"]
            result = {"type": "result", "id": command["id"], "ok": True, "data": {"model": "CI"}, "error": None}
            await phone.send(json.dumps(result))
            await phone.send(json.dumps(result))
            await phone.send(json.dumps(ready))
            assert await receive(phone) == {"type": "ack", "id": "ready"}
            status = (await client.get(f"/v1/commands/{command['id']}", headers=ADMIN)).json()
            assert status["status"] == "succeeded"
            events = (await client.get(f"/v1/devices/{claim['device_id']}/events?after=0&limit=50", headers=ADMIN)).json()
            assert len(events["events"]) == 1
            assert (await client.delete(f"/v1/devices/{claim['device_id']}", headers=ADMIN)).status_code == 204
            with pytest.raises(ConnectionClosed) as closed:
                await receive(phone)
            assert closed.value.rcvd.code == 1008
