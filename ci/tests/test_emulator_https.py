import importlib.util
import json
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location("prepare_emulator_server", Path(__file__).parents[1] / "prepare_emulator_server.py")
assert SPEC is not None and SPEC.loader is not None
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


@pytest.mark.parametrize("origin", ["http://clock.trycloudflare.com", "https://clock.trycloudflare.com/", "https://clock.trycloudflare.com?token=x", "https://clock.trycloudflare.com.evil.example", "https://user@clock.trycloudflare.com", "https://clock.trycloudflare.com:443", "https://-clock.trycloudflare.com"])
def test_public_origin_contract_rejects_insecure_or_ambiguous_origins(origin):
    assert SERVER.PUBLIC_ORIGIN.fullmatch(origin) is None


def test_public_origin_contract_accepts_exact_ephemeral_https_hostname():
    assert SERVER.PUBLIC_ORIGIN.fullmatch("https://quiet-vendor-clock.trycloudflare.com") is not None


def test_child_environment_does_not_receive_github_or_google_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-github-secret")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/synthetic/oauth.json")
    monkeypatch.setenv("PHONEBRIDGE_CI_ADMIN_TOKEN", "synthetic-admin-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    environment = SERVER.isolated_environment(tmp_path)
    assert environment["HOME"] == str(tmp_path)
    assert environment["TMPDIR"] == str(tmp_path)
    assert environment["PATH"] == "/usr/bin"
    assert not {"GITHUB_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "PHONEBRIDGE_CI_ADMIN_TOKEN"} & environment.keys()


def test_private_state_files_have_restricted_permissions(tmp_path):
    path = tmp_path / "token"
    SERVER.private_write(path, "synthetic-token")
    assert path.read_text() == "synthetic-token"
    assert path.stat().st_mode & 0o777 == 0o600


def test_saved_process_records_bind_pid_to_start_time(tmp_path):
    records = [{"pid": 123, "start": "456"}]
    SERVER.save_processes(tmp_path, records)
    assert json.loads((tmp_path / "processes.json").read_text()) == records
    assert not (tmp_path / "processes.pending.json").exists()


def test_cleanup_never_signals_a_reused_pid(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir()
    records = [{"pid": 123, "start": "previous-start"}]
    monkeypatch.setattr(SERVER, "process_identity", lambda pid: "different-start")
    signals = []
    monkeypatch.setattr(SERVER.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    SERVER.stop(root, records)
    assert signals == []
    assert not root.exists()


def test_ephemeral_server_cannot_be_started_outside_automatic_ci(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(SERVER.sys, "argv", ["prepare_emulator_server.py", "start"])
    with pytest.raises(SystemExit) as error:
        SERVER.main()
    assert error.value.code == 2
