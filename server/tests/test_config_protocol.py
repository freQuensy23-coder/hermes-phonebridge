from dataclasses import replace

import pytest
from pydantic import ValidationError

from phonebridge_server.config import Settings
from phonebridge_server.protocol import (
    ClaimRequest,
    CommandRequest,
    EventFrame,
    HelloFrame,
    PairingRequest,
    ResultFrame,
    decode_json,
    validate_json,
)
from phonebridge_server.transport import RateLimiter

from phonebridge_test_support import ADMIN_TOKEN


@pytest.mark.parametrize("token", ["", "short", "a" * 64, "space in a sufficiently long token 0123456789"])
def test_weak_or_invalid_admin_tokens_are_rejected(token):
    with pytest.raises(ValueError):
        Settings(admin_token=token)


@pytest.mark.parametrize("url", ["http://phone.example", "https://user:secret@phone.example",
                                 "https://phone.example/path", "https://phone.example?token=x",
                                 "https://phone.example#fragment", "https://phone.example:bad", "https://", "https://phone.example:0",
                                 "https://bad_host.example", "https://phone.example\\attacker", "https://.example"])
def test_unsafe_public_urls_are_rejected(url):
    with pytest.raises(ValueError):
        Settings(admin_token=ADMIN_TOKEN, public_url=url)


def test_debug_http_requires_explicit_opt_in(settings):
    insecure = replace(settings, public_url="http://10.0.2.2:18940/", allow_insecure=True)
    assert insecure.ws_url == "ws://10.0.2.2:18940/v1/device/ws"
    assert settings.ws_url == "wss://phone.example.test/v1/device/ws"
    assert ADMIN_TOKEN not in repr(settings)


def test_environment_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("PHONEBRIDGE_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("PHONEBRIDGE_PUBLIC_URL", "https://phone.example")
    monkeypatch.setenv("PHONEBRIDGE_DB_PATH", str(tmp_path / "bridge.sqlite3"))
    monkeypatch.setenv("PHONEBRIDGE_REVISION", "exact-sha")
    monkeypatch.setenv("PHONEBRIDGE_ALLOW_INSECURE", "false")
    settings = Settings.from_env()
    assert settings.revision == "exact-sha"
    assert settings.db_path == tmp_path / "bridge.sqlite3"
    monkeypatch.setenv("PHONEBRIDGE_ALLOW_INSECURE", "yes")
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.parametrize("value", [{"name": "", "ttl_seconds": 300}, {"name": "x", "ttl_seconds": True},
                                   {"name": "x", "ttl_seconds": 901}, {"name": "\n"}, {"name": "x", "extra": 1}])
def test_strict_pairing_validation(value):
    with pytest.raises(ValidationError):
        PairingRequest.model_validate(value)


def test_token_and_platform_are_strict():
    with pytest.raises(ValidationError):
        ClaimRequest(token="not-a-token", name="x", platform="android")
    with pytest.raises(ValidationError):
        ClaimRequest(token="a" * 43, name="x", platform="ios")
    assert ClaimRequest(token="a" * 43, name="Pixel", platform="android").name == "Pixel"


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{bad}', '"\\ud800"'])
def test_ambiguous_or_invalid_json_is_rejected(raw):
    with pytest.raises((ValueError, UnicodeError)):
        decode_json(raw)


def test_nested_and_oversized_json_is_rejected():
    nested = {}
    for _ in range(20):
        nested = {"value": nested}
    with pytest.raises(ValueError):
        validate_json(nested)
    with pytest.raises(ValidationError):
        CommandRequest(op="device.info", args={"text": "x" * 17000})
    with pytest.raises(ValidationError):
        EventFrame(type="event", id="1", name="notification", data={"a": "x" * 16000, "b": "y" * 1000}, time=1)


def test_hello_ignores_unknown_operations_and_rejects_fake_permissions():
    hello = HelloFrame(type="hello", version="0.2.0", capabilities=["shell.exec", "device.info"], permissions={})
    assert hello.capabilities == ["device.info"]
    with pytest.raises(ValidationError):
        HelloFrame(type="hello", version="0.1.0", capabilities=["bad op"], permissions={})
    with pytest.raises(ValidationError):
        HelloFrame(type="hello", version="0.1.0", capabilities=["device.info"], permissions={"sms": "true"})
    with pytest.raises(ValidationError):
        HelloFrame(type="hello", version="0.1.0", capabilities=["device.info", "device.info"], permissions={})


def test_result_identity_cannot_be_supplied():
    with pytest.raises(ValidationError):
        ResultFrame.model_validate({"type": "result", "id": "1", "ok": True, "device_id": "other"})


def test_bounded_token_bucket_and_refill():
    now = [1.0]
    limiter = RateLimiter(2, lambda: now[0])
    assert limiter.take("first", 2)
    assert limiter.take("first", 2)
    assert not limiter.take("first", 2)
    assert limiter.take("second", 2)
    assert not limiter.take("third", 2)
    now[0] += 30
    assert limiter.take("first", 2)
    now[0] += 121
    assert limiter.take("third", 2)
