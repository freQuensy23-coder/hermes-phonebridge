import threading
from contextlib import contextmanager
from uuid import uuid4

from phonebridge_server.protocol import ClaimRequest, HelloFrame, PairingRequest


ADMIN_TOKEN = "ci-phonebridge-test-admin-token-000000000000000000"
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
CAPABILITIES = ["device.info", "debug.status", "files.receive"]


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
                                               capabilities=CAPABILITIES if capabilities is None else capabilities, permissions={}))
    return pairing, claim, device_id, session_id


class BufferedSocket:
    def __init__(self, socket):
        self.socket = socket
        self.pending = []

    def receive_json(self):
        if self.pending:
            return self.pending.pop(0)
        return self.socket.receive_json()

    def __getattr__(self, name):
        return getattr(self.socket, name)


@contextmanager
def connected(client, claim, capabilities=None):
    with client.websocket_connect("/v1/device/ws", headers={"Authorization": f"Bearer {claim['device_token']}"}) as socket:
        socket.send_json({"type": "hello", "version": "0.1.0",
                          "capabilities": CAPABILITIES if capabilities is None else capabilities,
                          "permissions": {"notifications": True}})
        ready_id = str(uuid4())
        socket.send_json({"type": "event", "id": ready_id, "name": "connection.ready", "data": {}, "time": 0})
        buffered = BufferedSocket(socket)
        while (frame := socket.receive_json()) != {"type": "ack", "id": ready_id}:
            assert frame.get("type") == "command"
            buffered.pending.append(frame)
        yield buffered
