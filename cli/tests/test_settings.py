import json

import pytest

from phonebridge.client import Settings, segment, validate_url
from phonebridge.errors import BridgeError


def test_defaults(environment, monkeypatch):
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", "private-test-token")
    settings = Settings.load()
    assert settings.url == "http://127.0.0.1:18940"
    assert settings.token == "private-test-token"
    assert settings.request_timeout == 10


def test_missing_token_safe(environment):
    with pytest.raises(BridgeError) as caught:
        Settings.load()
    assert caught.value.code == "CONFIG_ERROR"


def test_private_config_precedence(tmp_path, environment, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"url": "https://configured.example", "token": "config-token"}))
    path.chmod(0o600)
    assert Settings.load(str(path)).url == "https://configured.example"
    assert Settings.load(str(path)).token == "config-token"
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", "env-token")
    monkeypatch.setenv("PHONEBRIDGE_URL", "https://env.example")
    settings = Settings.load(str(path), url="https://explicit.example")
    assert settings.url == "https://explicit.example"
    assert settings.token == "env-token"
    assert json.loads(path.read_text())["token"] == "config-token"


@pytest.mark.parametrize("mode", [0o644, 0o400, 0o666, 0o700])
def test_config_requires_exact_private_mode(tmp_path, environment, mode):
    path = tmp_path / "config.json"
    path.write_text('{"token":"secret"}')
    path.chmod(mode)
    with pytest.raises(BridgeError):
        Settings.load(str(path))


def test_config_refuses_symlink_and_fifo(tmp_path, environment):
    import os

    target = tmp_path / "config.json"
    target.write_text('{"token":"secret"}')
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(BridgeError):
        Settings.load(str(link))
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(BridgeError):
        Settings.load(str(fifo))


@pytest.mark.parametrize("content", ['{"unknown":1}', '{"token":"one","token":"two"}', '[]', 'invalid', '{"token":123}', '{"url":null,"token":"secret"}'])
def test_invalid_config_is_safe(tmp_path, environment, content):
    path = tmp_path / "config.json"
    path.write_text(content)
    path.chmod(0o600)
    with pytest.raises(BridgeError):
        Settings.load(str(path))


@pytest.mark.parametrize("url", ["http://example.com", "ftp://example.com", "https://u:p@example.com", "https://example.com?token=x", "https://example.com/#x", "https://example.com:bad", "https://example.com:0", "https://example.com/../x", "https://example.com/ x", "https://", "http://localhost.attacker.invalid"])
def test_unsafe_urls_refused(url):
    with pytest.raises(BridgeError):
        validate_url(url)


@pytest.mark.parametrize("url", ["https://bridge.example", "https://bridge.example/prefix", "http://localhost:18940", "http://127.0.0.1:18940", "http://[::1]:18940"])
def test_safe_urls_allowed(url):
    assert validate_url(url).hostname


@pytest.mark.parametrize("token", ["", "with space", "newline\n", "é", "tab\t", "x" * 8193])
def test_unsafe_tokens_refused(environment, monkeypatch, token):
    monkeypatch.setenv("PHONEBRIDGE_TOKEN", token)
    with pytest.raises(BridgeError):
        Settings.load()


def test_identifier_preservation():
    assert segment("literal/a?b%2F") == "literal%2Fa%3Fb%252F"


@pytest.mark.parametrize("value", ["", ".", "..", "a\n", "x" * 257])
def test_invalid_identifier_refused(value):
    with pytest.raises(BridgeError):
        segment(value)



def test_settings_repr_does_not_expose_token():
    settings = Settings("https://bridge.example", "private-test-token")
    assert "private-test-token" not in repr(settings)
