import json

import httpx
from websockets.asyncio.client import connect

from screen_test_support import screenshot_data
from phonebridge_test_support import ADMIN
from test_network import live_server as live_server, receive


async def test_mock_phone_real_network_screen_commands_and_bounded_screenshot_readback(request):
    http_url, ws_url = request.getfixturevalue("live_server")
    async with httpx.AsyncClient(base_url=http_url, timeout=5) as client:
        pairing_response = await client.post("/v1/pairings", headers=ADMIN, json={"name": "Mock phone"})
        assert pairing_response.status_code == 200
        claim_response = await client.post("/v1/pair/claim", json={"token": pairing_response.json()["qr_payload"]["token"],
                                                               "name": "Mock phone", "platform": "android"})
        assert claim_response.status_code == 200
        claim = claim_response.json()
        async with connect(ws_url, additional_headers={"Authorization": "Bearer " + claim["device_token"]},
                           max_size=65536, max_queue=8) as phone:
            await phone.send(json.dumps({"type": "hello", "version": "0.1.0",
                                         "capabilities": ["screen.tap", "screen.screenshot"], "permissions": {}}))
            ready = {"type": "event", "id": "mock-ready", "name": "mock.ready", "data": {}, "time": 1}
            await phone.send(json.dumps(ready))
            assert await receive(phone) == {"type": "ack", "id": ready["id"]}
            tap_data = {"outcome": "dispatched", "gesture_completed": True, "verified": False}
            for op, args, data in (("screen.tap", {"x": 10, "y": 20}, tap_data),
                                   ("screen.screenshot", {}, screenshot_data())):
                response = await client.post(f"/v1/devices/{claim['device_id']}/commands", headers=ADMIN,
                                             json={"op": op, "args": args, "idempotency_key": op})
                assert response.status_code == 200
                command = response.json()
                assert await receive(phone) == {"type": "command", "id": command["id"], "op": op,
                                                "args": args, "expires_at": command["expires_at"]}
                result = {"type": "result", "id": command["id"], "ok": True, "data": data, "error": None}
                await phone.send(json.dumps(result))
                await phone.send(json.dumps(result))
                await phone.send(json.dumps(ready))
                assert await receive(phone) == {"type": "ack", "id": ready["id"]}
                readback = await client.get(f"/v1/commands/{command['id']}", headers=ADMIN)
                assert readback.status_code == 200
                assert readback.json() == {"id": command["id"], "device_id": claim["device_id"], "op": op,
                                           "status": "succeeded", "expires_at": command["expires_at"], "data": data, "error": None}
                assert len(readback.content) <= 1048576
                assert readback.headers["cache-control"] == "no-store"
                assert (await client.get(f"/v1/commands/{command['id']}")).status_code == 401
