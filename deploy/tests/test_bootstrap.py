import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("bootstrap", Path(__file__).parents[1] / "bootstrap.py")
assert spec is not None and spec.loader is not None
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "HOME", tmp_path)
    credential = tmp_path / ".hermes/credentials/phonebridge"
    credential.mkdir(parents=True)
    key = "ssh-ed25519 AAAAtest phonebridge-github-deploy"
    (credential / "deploy_ed25519.pub").write_text(key)
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    original = "ssh-ed25519 AAAAexisting keep-existing-access\n"
    (ssh / "authorized_keys").write_text(original)
    calls = []
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *args, **kwargs: calls.append(args[0]))
    return tmp_path, key, original, calls


def test_bootstrap_preserves_other_access_and_is_idempotent(setup):
    home, key, original, calls = setup
    bootstrap.main()
    authorized = home / ".ssh/authorized_keys"
    first = authorized.read_text()
    bootstrap.main()
    assert authorized.read_text() == first
    assert first.startswith(original)
    assert first.count(key) == 1
    assert 'restrict,command="/usr/bin/python3 ' in first
    assert authorized.stat().st_mode & 0o777 == 0o600
    assert (home / ".local/share/phonebridge/bin/deploy_entry.py").is_file()
    assert calls == [["systemctl", "--user", "daemon-reload"]] * 2


def test_bootstrap_refuses_unrestricted_existing_deployment_key(setup):
    home, key, original, calls = setup
    authorized = home / ".ssh/authorized_keys"
    authorized.write_text(original + key + "\n")
    before = authorized.read_text()
    with pytest.raises(RuntimeError, match="different restrictions"):
        bootstrap.main()
    assert authorized.read_text() == before
    assert calls == []
