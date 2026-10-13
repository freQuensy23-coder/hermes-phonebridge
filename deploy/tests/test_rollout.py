import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("rollout", Path(__file__).parents[1] / "rollout.py")
assert spec is not None and spec.loader is not None
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)


def test_rejects_stale_revision(monkeypatch):
    monkeypatch.setattr(rollout, "github", lambda path: {"sha": "b" * 40})
    with pytest.raises(RuntimeError, match="current main"):
        rollout.accepted_revision("a" * 40)


def test_checks_latest_result_not_older_success(monkeypatch):
    revision = "a" * 40
    checks = [{"id": index, "name": name, "conclusion": "success"} for index, name in enumerate(rollout.REQUIRED_CHECKS)]
    checks.append({"id": 100, "name": "python", "conclusion": "failure"})
    monkeypatch.setattr(rollout, "github", lambda path: {"check_runs": checks} if "check-runs" in path else {"sha": revision})
    with pytest.raises(RuntimeError, match="checks"):
        rollout.accepted_revision(revision)


def test_requires_all_checks(monkeypatch):
    revision = "a" * 40
    monkeypatch.setattr(rollout, "github", lambda path: {"check_runs": []} if "check-runs" in path else {"sha": revision})
    with pytest.raises(RuntimeError, match="checks"):
        rollout.accepted_revision(revision)


def test_accepts_current_successful_main(monkeypatch):
    revision = "a" * 40
    checks = [{"id": index, "name": name, "conclusion": "success"} for index, name in enumerate(rollout.REQUIRED_CHECKS)]
    monkeypatch.setattr(rollout, "github", lambda path: {"check_runs": checks} if "check-runs" in path else {"sha": revision})
    rollout.accepted_revision(revision)


@pytest.mark.parametrize("command", ["", "bash", "deploy main", "deploy " + "a" * 40 + " extra", "deploy $(id)", "deploy ../other"])
def test_forced_command_rejects_unsafe_input(monkeypatch, command):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", command)
    with pytest.raises(SystemExit):
        rollout.main()


def test_switch_atomic_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(rollout, "BASE", tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    rollout.switch(first)
    assert (tmp_path / "current").resolve() == first
    rollout.switch(second)
    assert (tmp_path / "current").resolve() == second
    assert not (tmp_path / "current.next").exists()
