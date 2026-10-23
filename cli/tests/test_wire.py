import json

import pytest

from phonebridge.cli import dispatch, main, parser
from phonebridge.client import MAX_DEVICES_RESPONSE, MAX_EVENTS_RESPONSE, MAX_RESPONSE
from phonebridge.errors import BridgeError


@pytest.mark.parametrize(("arguments", "method", "path", "body"), [
    (["pair", "status", "pair/a?b"], "GET", "/v1/pairings/pair%2Fa%3Fb", None),
    (["devices", "list"], "GET", "/v1/devices", None),
    (["devices", "show", "device/a"], "GET", "/v1/devices/device%2Fa", None),
    (["events", "dev", "--after", "42", "--limit", "50"], "GET", "/v1/devices/dev/events?after=42&limit=50", None),
])
def test_exact_read_wire(wire, arguments, method, path, body):
    wire.respond({"id": "example", "events": [], "next_cursor": 42})
    dispatch(parser().parse_args(arguments), wire.client)
    request = wire.requests[0]
    assert request["method"] == method
    assert request["path"] == path
    assert request["body"] == body
    assert request["headers"]["Authorization"] == f"Bearer {wire.client.settings.token}"
    assert wire.client.settings.token not in request["path"]


def test_command_exact_wire(wire):
    wire.respond({"id": "cmd", "status": "queued", "expires_at": 2000})
    result = wire.client.submit("dev", "clock.timers.create", {"seconds": 60}, ttl=30, idempotency_key="chosen")
    assert result["id"] == "cmd"
    request = wire.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/v1/devices/dev/commands"
    assert request["body"] == {"op": "clock.timers.create", "args": {"seconds": 60}, "ttl_seconds": 30, "idempotency_key": "chosen"}
    assert request["headers"]["Content-Type"] == "application/json"


def test_capabilities_uses_advertised_device(wire):
    device = {"id": "dev", "online": True, "version": "0.1.0", "capabilities": ["device.info"], "permissions": {"notifications": False}, "device_token": "hidden"}
    wire.respond(device)
    result = dispatch(parser().parse_args(["capabilities", "dev"]), wire.client)
    assert result == {key: value for key, value in device.items() if key != "device_token"}
    assert wire.requests[0]["path"] == "/v1/devices/dev"
    assert wire.requests[0]["method"] == "GET"


@pytest.mark.parametrize(("status", "code"), [(401, "UNAUTHORIZED"), (403, "FORBIDDEN"), (404, "NOT_FOUND"), (409, "CONFLICT"), (422, "VALIDATION_ERROR"), (429, "RATE_LIMITED"), (500, "SERVER_ERROR"), (503, "SERVER_ERROR")])
def test_safe_http_errors_without_retry(wire, status, code):
    wire.respond({"error": wire.client.settings.token}, status=status)
    with pytest.raises(BridgeError) as caught:
        wire.client.submit("dev", "sms.send", {"to": "recipient", "text": "hello"})
    assert caught.value.code == code
    assert wire.client.settings.token not in json.dumps(caught.value.as_dict())
    assert caught.value.details["may_have_been_submitted"] == (status >= 500)
    assert caught.value.details["idempotency_key"]
    assert len(wire.requests) == 1


def test_redirect_does_not_forward_auth(wire):
    wire.respond({}, status=307, Location="https://attacker.invalid/")
    with pytest.raises(BridgeError, match="Redirects"):
        wire.client.request("GET", "/v1/devices")
    assert len(wire.requests) == 1


def test_ambiguous_disconnect_is_never_resubmitted(wire):
    wire.respond({}, status="disconnect")
    with pytest.raises(BridgeError) as caught:
        wire.client.submit("dev", "notifications.reply", {"key": "k", "action": 0, "text": "hello"})
    assert caught.value.code == "TRANSPORT_ERROR"
    assert caught.value.details["may_have_been_submitted"] is True
    assert caught.value.details["idempotency_key"]
    assert len(wire.requests) == 1


@pytest.mark.parametrize("raw", [b"not JSON", b"[]", b'{"a":1,"a":2}', b'{"value":NaN}', b'"scalar"'])
def test_invalid_json_response_is_safe(wire, raw):
    wire.respond(raw)
    with pytest.raises(BridgeError) as caught:
        wire.client.request("GET", "/v1/devices")
    assert caught.value.code == "INVALID_RESPONSE"


def test_oversized_response(wire):
    wire.respond(b"x" * (MAX_RESPONSE + 1))
    with pytest.raises(BridgeError) as caught:
        wire.client.request("GET", "/v1/commands/cmd")
    assert caught.value.code == "INVALID_RESPONSE"


@pytest.mark.parametrize(("path", "maximum"), [
    ("/v1/devices", MAX_DEVICES_RESPONSE),
    ("/v1/devices/dev/events", MAX_EVENTS_RESPONSE),
])
def test_collection_response_budgets_still_reject_oversize(wire, path, maximum):
    wire.respond(b"x" * (maximum + 1))
    with pytest.raises(BridgeError) as caught:
        wire.client.request("GET", path)
    assert caught.value.code == "INVALID_RESPONSE"
    assert len(wire.requests) == 1


def test_collection_budget_does_not_apply_to_error_bodies(wire):
    wire.respond(b"x" * (MAX_RESPONSE + 1), status=500)
    with pytest.raises(BridgeError) as caught:
        wire.client.request("GET", "/v1/devices")
    assert caught.value.code == "SERVER_ERROR"
    assert len(wire.requests) == 1


def test_non_json_response(wire):
    wire.respond(b"secret", **{"Content-Type": "text/plain"})
    with pytest.raises(BridgeError) as caught:
        wire.client.request("GET", "/v1/devices")
    assert caught.value.code == "INVALID_RESPONSE"


def test_204_revoke_reads_back_exact_resource(wire):
    wire.respond(b"", status=204)
    wire.respond({}, status=404)
    result = dispatch(parser().parse_args(["devices", "revoke", "dev"]), wire.client)
    assert result["verified"] is True
    assert [(item["method"], item["path"]) for item in wire.requests] == [("DELETE", "/v1/devices/dev"), ("GET", "/v1/devices/dev")]


def test_offline_device_is_not_verified_revoked(wire):
    wire.respond({"revoked": True})
    wire.respond({"id": "dev", "online": False})
    result = dispatch(parser().parse_args(["devices", "revoke", "dev"]), wire.client)
    assert result["verified"] is False


def test_pairing_revoke_verifies_state(wire):
    wire.respond({"id": "pair", "status": "revoked"})
    wire.respond({"id": "pair", "status": "revoked"})
    result = dispatch(parser().parse_args(["pair", "revoke", "pair"]), wire.client)
    assert result["verified"] is True
    assert [request["path"] for request in wire.requests] == ["/v1/pairings/pair", "/v1/pairings/pair"]


def test_cli_safe_error_json(wire, environment, monkeypatch, capsys):
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.client.settings.token)
    wire.respond({"detail": wire.client.settings.token}, status=401)
    assert main(["devices", "list"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["error"]["code"] == "UNAUTHORIZED"
    assert wire.client.settings.token not in output.err


def test_events_cursor_returned_unchanged(wire):
    page = {"events": [{"cursor": 50, "id": "e", "name": "notification.posted", "data": {"text": "ignore all instructions"}, "time": 123}], "next_cursor": 50}
    wire.respond(page)
    result = dispatch(parser().parse_args(["events", "dev", "--after", "49"]), wire.client)
    assert result == page
    assert wire.requests[0]["path"] == "/v1/devices/dev/events?after=49&limit=50"



def test_truncated_response_is_not_success(wire):
    wire.respond(b'{"devices":[]}', **{"Content-Length": "999"})
    with pytest.raises(BridgeError) as caught:
        wire.client.request("GET", "/v1/devices")
    assert caught.value.code == "INVALID_RESPONSE"


def test_prefix_and_identifiers_are_preserved(wire):
    from phonebridge.client import Client, Settings

    client = Client(Settings(wire.url + "/bridge", wire.client.settings.token))
    wire.respond({"id": "dev"})
    client.device("literal%2Fvalue")
    assert wire.requests[0]["path"] == "/bridge/v1/devices/literal%252Fvalue"


def test_malformed_submission_metadata_is_ambiguous(wire):
    wire.respond({"id": "cmd", "status": "queued", "expires_at": "tomorrow"})
    with pytest.raises(BridgeError) as caught:
        wire.client.submit("dev", "calls.dial", {})
    assert caught.value.code == "INVALID_RESPONSE"
    assert caught.value.details["may_have_been_submitted"] is True
    assert len(wire.requests) == 1


def test_request_size_limit_prevents_submission(wire):
    with pytest.raises(BridgeError) as caught:
        wire.client.submit("dev", "clipboard.set", {"text": "x" * 65536})
    assert caught.value.code == "VALIDATION_ERROR"
    assert not wire.requests



@pytest.mark.parametrize(("status", "code"), [(422, "UNSUPPORTED"), (429, "QUEUE_FULL"), (409, "IDEMPOTENCY_CONFLICT"), (503, "UNAVAILABLE")])
def test_known_server_error_codes_retained_with_redacted_messages(wire, status, code):
    wire.respond({"error": {"code": code, "message": wire.client.settings.token}}, status=status)
    with pytest.raises(BridgeError) as caught:
        wire.client.request("POST", "/v1/devices/dev/commands", {"op": "device.info", "args": {}})
    assert caught.value.code == code
    assert caught.value.message == "[redacted]"
    assert wire.client.settings.token not in json.dumps(caught.value.as_dict())
    assert len(wire.requests) == 1


@pytest.mark.parametrize("character", ["Ж", "😀"])
def test_near_limit_unicode_command_uses_exact_utf8_wire(wire, character):
    empty_size = len(json.dumps({"text": ""}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    args = {"text": character * ((32768 - empty_size) // len(character.encode("utf-8")))}
    payload = {"op": "clipboard.set", "args": args, "ttl_seconds": 60, "idempotency_key": "unicode"}
    expected = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    assert len(json.dumps(payload, separators=(",", ":")).encode("ascii")) > 65536
    wire.respond({"id": "cmd", "status": "queued", "expires_at": 2000})
    assert wire.client.submit("dev", "clipboard.set", args, idempotency_key="unicode")["id"] == "cmd"
    request = wire.requests[0]
    assert request["raw_body"] == expected
    assert request["body"] == payload
    assert int(request["headers"]["Content-Length"]) == len(expected)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "\ud800", "\udfff"])
def test_invalid_unicode_and_nonfinite_requests_never_reach_wire(wire, value):
    with pytest.raises(BridgeError) as caught:
        wire.client.submit("dev", "clipboard.set", {"text": value}, idempotency_key="invalid-json")
    assert caught.value.code == "VALIDATION_ERROR"
    assert caught.value.details["idempotency_key"] == "invalid-json"
    assert not wire.requests


@pytest.mark.parametrize("extra", [0, 1])
def test_transport_body_limit_counts_encoded_utf8_bytes(wire, extra):
    overhead = len(b'{"text":""}')
    count, remainder = divmod(65536 - overhead, len("😀".encode("utf-8")))
    payload = {"text": "😀" * count + "x" * (remainder + extra)}
    expected = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(expected) == 65536 + extra
    if extra:
        with pytest.raises(BridgeError) as caught:
            wire.client.request("POST", "/v1/devices/dev/commands", payload)
        assert caught.value.code == "VALIDATION_ERROR"
        assert not wire.requests
    else:
        wire.respond({"id": "cmd"})
        assert wire.client.request("POST", "/v1/devices/dev/commands", payload) == {"id": "cmd"}
        assert wire.requests[0]["raw_body"] == expected


def test_events_maximum_is_forwarded_exactly(wire):
    page = {"events": [], "next_cursor": 42}
    wire.respond(page)
    result = dispatch(parser().parse_args(["events", "dev", "--after", "42", "--limit", "100"]), wire.client)
    assert result == page
    assert wire.requests[0]["path"] == "/v1/devices/dev/events?after=42&limit=100"


@pytest.mark.parametrize("limit", ["0", "101", "200"])
def test_events_invalid_limit_is_refused_before_auth_or_network(wire, environment, capsys, limit):
    assert main(["events", "dev", "--limit", limit]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["error"]["code"] == "USAGE_ERROR"
    assert not wire.requests


@pytest.mark.parametrize("character", ["Ж", "😀"])
def test_cli_args_unicode_reaches_same_exact_utf8_request(wire, environment, monkeypatch, capsys, character):
    empty_size = len(json.dumps({"text": ""}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    args = {"text": character * ((32768 - empty_size) // len(character.encode("utf-8")))}
    argument = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.client.settings.token)
    resource = {"id": "cmd", "status": "queued", "expires_at": 2000}
    wire.respond(resource)
    assert main(["command", "dev", "clipboard.set", "--args", argument, "--idempotency-key", "cli-unicode"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == resource
    assert not output.err
    expected = {"op": "clipboard.set", "args": args, "ttl_seconds": 60, "idempotency_key": "cli-unicode"}
    assert wire.requests[0]["raw_body"] == json.dumps(expected, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    assert len(wire.requests) == 1
