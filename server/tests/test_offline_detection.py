import json
import time

import httpx
from websockets.asyncio.client import connect

from phonebridge_test_support import ADMIN
from test_network import live_server as live_server, receive


async def test_dropped_phone_leaves_the_device_list_immediately(request):
    http_url, ws_url = request.getfixturevalue("live_server")
    async with httpx.AsyncClient(base_url=http_url, timeout=5) as client:
        pairing = (await client.post("/v1/pairings", headers=ADMIN, json={"name": "Phone"})).json()
        claim = (await client.post("/v1/pair/claim", json={"token": pairing["qr_payload"]["token"],
                                                           "name": "Phone", "platform": "android"})).json()
        async with connect(ws_url, additional_headers={"Authorization": "Bearer " + claim["device_token"]}) as phone:
            await phone.send(json.dumps({"type": "hello", "version": "0.1.0", "capabilities": ["device.info"], "permissions": {}}))
            ready = {"type": "event", "id": "ready", "name": "mock.ready", "data": {}, "time": 1}
            await phone.send(json.dumps(ready))
            assert await receive(phone) == {"type": "ack", "id": "ready"}
            listed = (await client.get("/v1/devices", headers=ADMIN)).json()["devices"]
            assert [device["id"] for device in listed] == [claim["device_id"]]
        started = time.monotonic()
        while (await client.get("/v1/devices", headers=ADMIN)).json()["devices"]:
            assert time.monotonic() - started < 3, "Dropped phone stayed listed until the online timeout"
            time.sleep(0.05)
        assert (await client.get(f"/v1/devices/{claim['device_id']}", headers=ADMIN)).json()["online"] is False
