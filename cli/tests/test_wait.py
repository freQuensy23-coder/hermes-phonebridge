import json

import pytest

from phonebridge.cli import main
from phonebridge.errors import BridgeError


def use_time(wire, fake_time):
    wire.client.clock = fake_time.monotonic
    wire.client.wall_clock = fake_time.wall
    wire.client.sleep = fake_time.sleep


def command(status, expires=1060, **extra):
    return {"id": "cmd", "device_id": "dev", "op": "clock.alarms.create", "status": status, "expires_at": expires, "data": None, "error": None, **extra}


def test_wait_submits_once_and_preserves_native_unverified(wire, fake_time):
    use_time(wire, fake_time)
    wire.respond({"id": "cmd", "status": "queued", "expires_at": 1060})
    wire.respond(command("queued"))
    wire.respond(command("sent"))
    native = {"outcome": "dispatched", "verified": False, "package": "com.google.android.deskclock"}
    wire.respond(command("succeeded", data=native))
    result = wire.client.submit("dev", "clock.alarms.create", {"hour": 7, "minute": 30}, wait=True)
    assert result["data"] == native
    assert [request["method"] for request in wire.requests] == ["POST", "GET", "GET", "GET"]
    assert all(request["path"] == "/v1/commands/cmd" for request in wire.requests[1:])
    assert fake_time.sleeps == [0.5, 0.5]


def test_wait_timeout_has_id_without_resubmit(wire, fake_time):
    use_time(wire, fake_time)
    wire.respond({"id": "cmd", "status": "queued", "expires_at": 1060})
    wire.respond(command("queued"))
    wire.respond(command("sent"))
    with pytest.raises(BridgeError) as caught:
        wire.client.submit("dev", "sms.send", {}, wait=True, timeout=1)
    assert caught.value.code == "WAIT_TIMEOUT"
    assert caught.value.details["command_id"] == "cmd"
    assert [request["method"] for request in wire.requests] == ["POST", "GET", "GET"]
    assert fake_time.elapsed == 1


def test_local_expiry_does_not_invent_server_result(wire, fake_time):
    use_time(wire, fake_time)
    wire.respond(command("sent", expires=1000))
    with pytest.raises(BridgeError) as caught:
        wire.client.wait("cmd")
    assert caught.value.code == "COMMAND_EXPIRY_REACHED"
    assert caught.value.details == {"command_id": "cmd", "last_status": "sent"}
    assert len(wire.requests) == 1


def test_wait_stops_at_expiry(wire, fake_time):
    use_time(wire, fake_time)
    wire.respond(command("queued", expires=1000.25))
    wire.respond(command("expired", expires=1000.25))
    result = wire.client.wait("cmd")
    assert result["status"] == "expired"
    assert fake_time.sleeps == [0.25]
    assert [request["method"] for request in wire.requests] == ["GET", "GET"]


@pytest.mark.parametrize("status", ["failed", "expired", "succeeded"])
def test_terminal_command_never_polled_again(wire, fake_time, status):
    use_time(wire, fake_time)
    wire.respond(command(status))
    assert wire.client.wait("cmd")["status"] == status
    assert not fake_time.sleeps
    assert len(wire.requests) == 1


def test_poll_failure_preserves_command_id(wire, fake_time):
    use_time(wire, fake_time)
    wire.respond({}, status=503)
    with pytest.raises(BridgeError) as caught:
        wire.client.wait("cmd")
    assert caught.value.code == "SERVER_ERROR"
    assert caught.value.details["command_id"] == "cmd"
    assert len(wire.requests) == 1


def test_wrong_command_id_is_rejected(wire):
    wire.respond({"id": "wrong", "status": "succeeded", "expires_at": 1060})
    with pytest.raises(BridgeError) as caught:
        wire.client.get_command("cmd")
    assert caught.value.code == "INVALID_RESPONSE"
    assert caught.value.details["command_id"] == "cmd"


@pytest.mark.parametrize("status", ["failed", "expired"])
def test_cli_terminal_failures_are_nonzero(wire, environment, monkeypatch, capsys, status):
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.client.settings.token)
    wire.respond(command(status, error={"code": "PHONE_LOCKED", "message": wire.client.settings.token}))
    assert main(["command-status", "cmd"]) == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["status"] == status
    if status == "failed":
        assert result["error"]["code"] == "PHONE_LOCKED"
    assert wire.client.settings.token not in output.out
    assert output.err == ""


def test_existing_id_wait_never_posts(wire, environment, monkeypatch, capsys):
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.client.settings.token)
    wire.respond(command("succeeded", data={"verified": False, "outcome": "dispatched"}))
    assert main(["command-status", "cmd", "--wait"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["verified"] is False
    assert [request["method"] for request in wire.requests] == ["GET"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 0, True])
def test_invalid_wait_is_rejected_before_request(wire, value):
    with pytest.raises(BridgeError):
        wire.client.wait("cmd", timeout=value)
    assert not wire.requests



def test_slow_response_is_bounded_without_retry(wire):
    wire.respond(command("sent"), test_delay="0.3")
    with pytest.raises(BridgeError) as caught:
        wire.client.wait("cmd", timeout=0.1)
    assert caught.value.code == "TRANSPORT_ERROR"
    assert caught.value.details["command_id"] == "cmd"
    assert [request["method"] for request in wire.requests] == ["GET"]


@pytest.mark.parametrize("status", ["unknown", {}, None])
def test_malformed_command_status_has_safe_id(wire, status):
    wire.respond(command(status))
    with pytest.raises(BridgeError) as caught:
        wire.client.wait("cmd")
    assert caught.value.code == "INVALID_RESPONSE"
    assert caught.value.details["command_id"] == "cmd"



def test_wait_interrupt_keeps_original_id(wire, fake_time):
    use_time(wire, fake_time)
    wire.respond(command("queued"))

    def interrupt(seconds):
        raise KeyboardInterrupt

    wire.client.sleep = interrupt
    with pytest.raises(BridgeError) as caught:
        wire.client.wait("cmd")
    assert caught.value.code == "INTERRUPTED"
    assert caught.value.details["command_id"] == "cmd"
    assert [request["method"] for request in wire.requests] == ["GET"]


def test_native_dispatch_cannot_claim_verification(wire, environment, monkeypatch, capsys):
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.client.settings.token)
    wire.respond(command("succeeded", data={"outcome": "dispatched", "verified": True, "package": "com.google.android.deskclock"}))
    assert main(["command-status", "cmd"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["verified"] is False
