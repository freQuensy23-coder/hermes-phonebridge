import json

import pytest

from phonebridge.cli import main, operation_args, parser
from phonebridge.errors import BridgeError
from phonebridge.jsonio import object_args, sanitize


@pytest.mark.parametrize(("arguments", "expected"), [
    (["debug", "status", "dev"], {}),
    (["screen", "tree", "dev", "--debug-session", "s1"], {"debug_session": "s1"}),
    (["screen", "tap", "dev", "--debug-session", "s1", "--package", "com.example", "--x", "10", "--y", "20"],
     {"debug_session": "s1", "expected_package": "com.example", "x": 10, "y": 20}),
    (["screen", "global", "dev", "--debug-session", "s1", "--action", "back"], {"debug_session": "s1", "action": "back"}),
])
def test_ergonomic_args(arguments, expected):
    assert operation_args(parser().parse_args(arguments)) == expected


@pytest.mark.parametrize("group", ["alarms", "timers", "notifications", "clock", "calendar", "sms"])
def test_removed_native_helpers_are_not_exposed(group, capsys):
    assert main([group, "list", "dev"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "USAGE_ERROR"


@pytest.mark.parametrize("value", ["[]", "null", "1", "NaN", '{"a":Infinity}', '{"a":1,"a":2}', "{" + " " * 65536 + "}"])
def test_args_must_be_bounded_strict_object(value):
    with pytest.raises(BridgeError) as caught:
        object_args(value)
    assert caught.value.code == "VALIDATION_ERROR"


@pytest.mark.parametrize("arguments", [
    ["command", "dev", "device.info", "--timeout", "nan"],
    ["events", "dev", "--after", "-1"],
    ["command", "dev", "device.info", "--args", "[]"],
])
def test_invalid_usage_has_safe_json(arguments, capsys):
    assert main(arguments) == 1
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["error"]["code"] in {"USAGE_ERROR", "VALIDATION_ERROR"}


@pytest.mark.parametrize("arguments", [
    ["debug", "status", "dev", "--args", '{"enable":true}'],
    ["screen", "tap", "dev", "--debug-session", "s1", "--package", "p", "--x", "1"],
    ["screen", "tree", "dev", "--debug-session", "bad session"],
])
def test_missing_conflicting_or_invalid_operation_args(arguments):
    with pytest.raises(BridgeError):
        operation_args(parser().parse_args(arguments))


def test_trusted_credentials_removed_without_filtering_phone_content():
    admin = "actual-admin-credential-00000000000000000000"
    device = "d" * 43
    result = {"token": admin, "data": {"device_token": "private-phone-password", "password": "status",
              "echo": f"{admin} {device}", "clipboard": "Bearer some-other-secret", "verified": False},
              "events": [{"data": {"cookie": "personal-cookie"}}], "idempotency_key": "ordinary-correlation"}
    clean = sanitize(result, {admin, device})
    assert "token" not in clean
    assert clean["data"] == {"device_token": "private-phone-password", "password": "status",
                             "echo": "[redacted] [redacted]", "clipboard": "Bearer some-other-secret", "verified": False}
    assert clean["events"] == [{"data": {"cookie": "personal-cookie"}}]
    assert clean["idempotency_key"] == "ordinary-correlation"
    assert admin not in json.dumps(clean)
    assert device not in json.dumps(clean)


def test_untrusted_metadata_token_does_not_become_a_credential(wire, environment, monkeypatch, capsys):
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.client.settings.token)
    wire.respond({"devices": [], "token": "another-secret", "echo": f"another-secret {wire.client.settings.token}"})
    assert main(["devices", "list"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"devices": [], "echo": "another-secret [redacted]"}
    assert wire.client.settings.token not in output.out
    assert not output.err


@pytest.mark.parametrize("target", ["status", "id", "device_id", "op", "data", "outcome", "verified", "cmd", "dev", "succeeded", "dispatched"])
def test_phone_secrets_cannot_rewrite_metadata_or_correlation(wire, environment, monkeypatch, capsys, target):
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.client.settings.token)
    data = {"token": target, "password": target, "nested": [{"admin_token": "phone-only", "cookies": "private-cookie"}],
            "clipboard": "Bearer private-clipboard-string", "outcome": "observed", "verified": True}
    resource = {"id": "cmd", "device_id": "dev", "op": "clipboard.get", "status": "succeeded",
                "data": data, "error": None, "expires_at": 2000}
    wire.respond(resource)
    assert main(["command-status", "cmd"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == resource
    assert not output.err
    assert wire.requests[0]["path"] == "/v1/commands/cmd"


def test_opaque_data_cannot_poison_later_response_inventory(wire):
    resource = {"id": "cmd", "status": "succeeded", "data": {"token": "status", "password": "cmd"}}
    wire.respond(resource)
    assert wire.client.get_command("cmd") == resource
    assert wire.client.secrets == {wire.client.settings.token}
    assert sanitize(resource, wire.client.secrets) == resource
    wire.respond({"devices": [{"id": "cmd", "name": "status", "permissions": {"token": True, "password": False}}]})
    later = wire.client.request("GET", "/v1/devices")
    assert sanitize(later, wire.client.secrets) == later
    assert wire.client.secrets == {wire.client.settings.token}


@pytest.mark.parametrize(("path", "response", "credential"), [
    ("/v1/pairings", {"id": "pair", "qr_payload": {"token": "p" * 43}}, "p" * 43),
    ("/v1/pair/claim", {"device_id": "dev", "device_token": "d" * 43}, "d" * 43),
])
def test_actual_pairing_and_auth_envelope_credentials_are_protected(wire, path, response, credential):
    wire.respond(response)
    resource = wire.client.request("POST", path, {})
    assert wire.client.secrets == {wire.client.settings.token, credential}
    safe = sanitize({**resource, "echo": f"Bearer {credential}"}, wire.client.secrets)
    assert credential not in json.dumps(safe)
    assert "qr_payload" not in safe
    assert "device_token" not in safe
    assert safe["echo"] == "Bearer [redacted]"


def test_actual_admin_credential_is_redacted_inside_opaque_phone_data(wire, environment, monkeypatch, capsys):
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.client.settings.token)
    resource = {"id": "cmd", "status": "succeeded", "data": {"password": wire.client.settings.token,
                "clipboard": f"Bearer {wire.client.settings.token}", "token": "ordinary-phone-token"}}
    wire.respond(resource)
    assert main(["command-status", "cmd"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"id": "cmd", "status": "succeeded", "data": {
        "password": "[redacted]", "clipboard": "Bearer [redacted]", "token": "ordinary-phone-token"}}
    assert wire.client.settings.token not in output.out
    assert not output.err


def test_unexpected_exception_is_safe(environment, monkeypatch, capsys):
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", "private-token")

    def explode(*args):
        raise RuntimeError("private-token stack secret")

    monkeypatch.setattr("phonebridge.cli.dispatch", explode)
    assert main(["devices", "list"]) == 1
    output = capsys.readouterr()
    assert "private-token" not in output.err
    assert "stack" not in output.err
    assert json.loads(output.err)["error"]["code"] == "CLIENT_ERROR"


def test_interrupt_has_nonzero_safe_error(environment, monkeypatch, capsys):
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", "private-token")

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr("phonebridge.cli.dispatch", interrupt)
    assert main(["devices", "list"]) == 130
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "INTERRUPTED"


def test_native_outcome_and_verification_keys_survive_opaque_token_values(wire, environment, monkeypatch, capsys):
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.client.settings.token)
    resource = {"id": "cmd", "status": "succeeded", "data": {"token": "outcome", "password": "dispatched",
                "outcome": "dispatched", "verified": False, "package": "com.google.android.deskclock"}}
    wire.respond(resource)
    assert main(["command-status", "cmd"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == resource
    assert not output.err
