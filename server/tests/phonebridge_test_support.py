import threading
from contextlib import contextmanager
from uuid import uuid4

from phonebridge_server.protocol import ClaimRequest, HelloFrame, PairingRequest


ADMIN_TOKEN = "ci-phonebridge-test-admin-token-000000000000000000"
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
CAPABILITIES = ["device.info", "clock.alarms.create", "notifications.list"]


class Clock:
    def __init__(self):
        self.value = 2000000000.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += seconds


def pair(client, name="Pixel", ttl=300):
    pairing = client.post("/v1/pairings", headers=ADMIN,
                          json={"name": name, "ttl_seconds": ttl}).json()
    response = client.post("/v1/pair/claim", json={"token": pairing["qr_payload"]["token"],
                                                "name": name, "platform": "android"})
    assert response.status_code == 200
    return pairing, response.json()


def provision(store, capabilities=None):
    pairing = store.create_pairing(PairingRequest(name="Pixel"))
    claim = store.claim(ClaimRequest(token=pairing["qr_payload"]["token"], name="Pixel", platform="android"))
    device_id, session_id = store.connect(claim["device_token"])
    store.hello(device_id, session_id, HelloFrame(type="hello", version="0.1.0",
                                               capabilities=capabilities or CAPABILITIES, permissions={}))
    return pairing, claim, device_id, session_id


@contextmanager
def connected(client, claim, capabilities=None):
    with client.websocket_connect("/v1/device/ws", headers={"Authorization": f"Bearer {claim['device_token']}"}) as socket:
        socket.send_json({"type": "hello", "version": "0.1.0",
                          "capabilities": capabilities or CAPABILITIES,
                          "permissions": {"notifications": True}})
        ready_id = str(uuid4())
        socket.send_json({"type": "event", "id": ready_id, "name": "connection.ready", "data": {}, "time": 0})
        assert socket.receive_json() == {"type": "ack", "id": ready_id}
        yield socket
