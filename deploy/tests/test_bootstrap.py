import base64
import json
import subprocess

import pytest


@pytest.fixture
def setup(modules, tmp_path, monkeypatch):
    bootstrap = modules["bootstrap"]
    monkeypatch.setattr(bootstrap, "HOME", tmp_path)
    credential = tmp_path / ".hermes/credentials/phonebridge"
    credential.mkdir(parents=True)
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32
    key = "ssh-ed25519 " + base64.b64encode(blob).decode() + " phonebridge-github-deploy"
    (credential / "deploy_ed25519.pub").write_text(key)
    authorized = tmp_path / ".ssh/authorized_keys"
    authorized.parent.mkdir()
    authorized.write_text("ssh-ed25519 existing keep-access\n")
    calls = []
    monkeypatch.setattr(bootstrap, "run", lambda *args: calls.append(args))
    return bootstrap, tmp_path, key, calls


def test_bootstrap_digest_and_canonical_entry_are_idempotent(setup):
    bootstrap, home, key, calls = setup
    bootstrap.main()
    authorized = home / ".ssh/authorized_keys"
    first = authorized.read_bytes()
    bootstrap.main()
    assert authorized.read_bytes() == first
    assert first.decode().count(key) == 1
    entry = home / ".local/share/phonebridge/bin/deploy_entry.py"
    assert f'command="/usr/bin/python3 {entry}"' in first.decode()
    assert entry.read_bytes() == (bootstrap.SOURCE / "rollout.py").read_bytes()
    trust = json.loads((home / ".local/share/phonebridge/trust.json").read_text())
    assert set(trust["trust"]) == set(bootstrap.FILES)
    assert authorized.stat().st_mode & 0o777 == 0o600
    assert calls == [("systemctl", "--user", "daemon-reload")] * 2


def test_rejected_key_does_not_mutate_existing_controller_or_unit(setup):
    bootstrap, home, key, calls = setup
    entry = home / ".local/share/phonebridge/bin/deploy_entry.py"
    unit = home / ".config/systemd/user/phonebridge.service"
    entry.parent.mkdir(parents=True)
    unit.parent.mkdir(parents=True)
    entry.write_text("previous controller")
    unit.write_text("previous unit")
    authorized = home / ".ssh/authorized_keys"
    authorized.write_text(key + "\n")
    before = {path: path.read_bytes() for path in (entry, unit, authorized)}
    with pytest.raises(RuntimeError, match="different restrictions"):
        bootstrap.main()
    assert {path: path.read_bytes() for path in before} == before
    assert not (home / ".local/share/phonebridge/trust.json").exists()
    assert not calls


def test_malformed_key_rejected_before_creating_runtime(setup):
    bootstrap, home, key, calls = setup
    (home / ".hermes/credentials/phonebridge/deploy_ed25519.pub").write_text("ssh-ed25519 AAAAtest phonebridge-github-deploy")
    with pytest.raises(RuntimeError, match="encoding"):
        bootstrap.main()
    assert not (home / ".local/share/phonebridge").exists()
    assert not calls


def test_reload_failure_restores_all_bootstrap_files(setup, monkeypatch):
    bootstrap, home, key, calls = setup
    bootstrap.main()
    targets = [home / ".ssh/authorized_keys", home / ".local/share/phonebridge/trust.json", home / ".local/share/phonebridge/bin/deploy_entry.py", home / ".config/systemd/user/phonebridge.service"]
    before = {path: path.read_bytes() for path in targets}
    failed = False

    def run(*args):
        nonlocal failed
        if not failed:
            failed = True
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(bootstrap, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        bootstrap.main()
    assert {path: path.read_bytes() for path in targets} == before
