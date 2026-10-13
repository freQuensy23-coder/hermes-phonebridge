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


def test_recursive_secret_output_removal():
    result = {"token": "secret-admin", "data": {"device_token": "secret-device", "echo": "secret-admin secret-device", "verified": False}, "events": [{"text": "Bearer some-other-secret"}], "idempotency_key": "ordinary-correlation"}
    clean = sanitize(result)
    encoded = json.dumps(clean)
    assert "secret-admin" not in encoded
    assert "secret-device" not in encoded
    assert "some-other-secret" not in encoded
    assert "token" not in clean
    assert clean["data"]["verified"] is False
    assert clean["idempotency_key"] == "ordinary-correlation"


def test_cli_filters_server_tokens_and_echoes(wire, environment, monkeypatch, capsys):
    monkeypatch.setenv("PHONEBRIDGE_URL", wire.url)
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", wire.client.settings.token)
    wire.respond({"devices": [], "token": "another-secret", "echo": f"another-secret {wire.client.settings.token}"})
    assert main(["devices", "list"]) == 0
    output = capsys.readouterr()
    assert "another-secret" not in output.out
    assert wire.client.settings.token not in output.out
    assert json.loads(output.out)["devices"] == []


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
