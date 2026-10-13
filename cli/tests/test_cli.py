import json

import pytest

from phonebridge.cli import main, operation_args, parser
from phonebridge.errors import BridgeError
from phonebridge.jsonio import object_args, sanitize


@pytest.mark.parametrize(("arguments", "expected"), [
    (["alarms", "create", "dev", "--hour", "7", "--minute", "30", "--label", "Work", "--days", "1,2,3,4,5", "--package", "com.google.android.deskclock"], {"hour": 7, "minute": 30, "label": "Work", "days": [1, 2, 3, 4, 5], "package": "com.google.android.deskclock"}),
    (["alarms", "dismiss", "dev", "--hour", "7", "--minute", "30"], {"hour": 7, "minute": 30}),
    (["alarms", "snooze", "dev", "--minutes", "10"], {"minutes": 10}),
    (["alarms", "show", "dev", "--package", "p"], {"package": "p"}),
    (["alarms", "list", "dev"], {}),
    (["alarms", "enable", "dev", "--id", "a"], {"id": "a"}),
    (["alarms", "disable", "dev", "--args", '{"selector":"exact"}'], {"selector": "exact"}),
    (["alarms", "delete", "dev", "--id", "a"], {"id": "a"}),
    (["timers", "create", "dev", "--seconds", "300", "--label", "Tea"], {"seconds": 300, "label": "Tea"}),
    (["timers", "dismiss", "dev", "--uri", "content://clock/timer/3"], {"uri": "content://clock/timer/3"}),
    (["timers", "dismiss", "dev"], {}),
    (["timers", "show", "dev"], {}),
    (["timers", "list", "dev"], {}),
    (["timers", "cancel", "dev", "--id", "t"], {"id": "t"}),
    (["timers", "pause", "dev", "--id", "t"], {"id": "t"}),
    (["timers", "resume", "dev", "--id", "t"], {"id": "t"}),
    (["notifications", "list", "dev", "--args", '{"limit":10}'], {"limit": 10}),
    (["notifications", "dismiss", "dev", "--key", "exact"], {"key": "exact"}),
    (["notifications", "reply", "dev", "--key", "exact", "--action", "0", "--text", "yes", "--result-key", "reply-key"], {"key": "exact", "action": 0, "text": "yes", "result_key": "reply-key"}),
    (["notifications", "action", "dev", "--key", "exact", "--action", "2"], {"key": "exact", "action": 2}),
    (["clock", "providers", "dev"], {}),
])
def test_ergonomic_args(arguments, expected):
    assert operation_args(parser().parse_args(arguments)) == expected


@pytest.mark.parametrize(("group", "actions", "prefix"), [
    ("alarms", ["create", "dismiss", "snooze", "show", "list", "enable", "disable", "delete"], "clock.alarms"),
    ("timers", ["create", "dismiss", "show", "list", "cancel", "pause", "resume"], "clock.timers"),
    ("notifications", ["list", "dismiss", "reply", "action"], "notifications"),
    ("clock", ["providers"], "clock"),
])
def test_helpers_send_current_contract_ops(wire, group, actions, prefix):
    from phonebridge.cli import dispatch

    for action in actions:
        args = {}
        if (group, action) == ("alarms", "create"):
            args = {"hour": 7, "minute": 30}
        elif (group, action) == ("timers", "create"):
            args = {"seconds": 60}
        elif group == "notifications" and action != "list":
            args = {"key": "exact"}
            if action in {"reply", "action"}:
                args["action"] = 0
            if action == "reply":
                args["text"] = "user-authorized"
        wire.respond({"id": "cmd", "status": "queued", "expires_at": 2000})
        options = parser().parse_args([group, action, "dev", "--args", json.dumps(args)])
        dispatch(options, wire.client)
        assert wire.requests[-1]["body"]["op"] == f"{prefix}.{action}"
        assert wire.requests[-1]["body"]["args"] == args
        assert wire.requests[-1]["method"] == "POST"


@pytest.mark.parametrize("value", ["[]", "null", "1", "NaN", '{"a":Infinity}', '{"a":1,"a":2}', "{" + " " * 65536 + "}"])
def test_args_must_be_bounded_strict_object(value):
    with pytest.raises(BridgeError) as caught:
        object_args(value)
    assert caught.value.code == "VALIDATION_ERROR"


@pytest.mark.parametrize("arguments", [
    ["alarms", "create", "dev", "--hour", "24", "--minute", "0"],
    ["alarms", "create", "dev", "--hour", "7", "--minute", "60"],
    ["alarms", "create", "dev", "--hour", "7", "--minute", "0", "--days", "0,1"],
    ["alarms", "create", "dev", "--hour", "7", "--minute", "0", "--days", "1,1"],
    ["timers", "create", "dev", "--seconds", "0"],
    ["timers", "create", "dev", "--seconds", "86401"],
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
    ["alarms", "create", "dev"],
    ["timers", "create", "dev"],
    ["notifications", "dismiss", "dev"],
    ["notifications", "reply", "dev", "--key", "k", "--text", "hello"],
    ["notifications", "action", "dev", "--key", "k"],
    ["alarms", "create", "dev", "--hour", "7", "--minute", "0", "--args", '{"hour":8}'],
    ["alarms", "create", "dev", "--args", '{"hour":true,"minute":0}'],
    ["timers", "create", "dev", "--args", '{"seconds":1.5}'],
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
