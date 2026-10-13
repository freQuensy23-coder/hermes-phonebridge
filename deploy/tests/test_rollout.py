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


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    import io
    import tarfile
    from types import SimpleNamespace

    base = tmp_path / "runtime"
    base.mkdir()
    monkeypatch.setattr(rollout, "BASE", base)
    monkeypatch.setattr(rollout, "SOURCE", tmp_path / "source")
    monkeypatch.setattr(rollout.Path, "home", staticmethod(lambda: tmp_path))
    settings = tmp_path / ".config/phonebridge/server.env"
    settings.parent.mkdir(parents=True)
    settings.write_text("PHONEBRIDGE_PUBLIC_URL=https://bridge.example\n")
    revision = "a" * 40
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w"):
        pass
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("git", "rev-parse", "origin/main"):
            return SimpleNamespace(stdout=revision)
        if args[:2] == ("git", "archive"):
            return SimpleNamespace(stdout=archive.getvalue())
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(rollout, "run", run)
    monkeypatch.setattr(rollout, "accepted_revision", lambda value: None)
    return base, revision, calls


def test_failed_health_restores_previous_release(deployment, monkeypatch):
    base, revision, calls = deployment
    previous = base / "releases/previous"
    previous.mkdir(parents=True)
    rollout.switch(previous)

    def unhealthy(value, url="http://127.0.0.1:18940/healthz"):
        raise RuntimeError("Deployed revision health check failed")

    monkeypatch.setattr(rollout, "healthy", unhealthy)
    with pytest.raises(RuntimeError, match="health check failed"):
        rollout.deploy(revision)
    assert (base / "current").resolve() == previous
    assert calls.count(("systemctl", "--user", "restart", "phonebridge.service")) == 2
    assert ("systemctl", "--user", "enable", "phonebridge.service") not in calls


def test_first_deployment_failure_stops_service_and_removes_current(deployment, monkeypatch):
    base, revision, calls = deployment

    def unhealthy(value, url="http://127.0.0.1:18940/healthz"):
        raise RuntimeError("Unhealthy first deployment")

    monkeypatch.setattr(rollout, "healthy", unhealthy)
    with pytest.raises(RuntimeError, match="Unhealthy first"):
        rollout.deploy(revision)
    assert not (base / "current").is_symlink()
    assert ("systemctl", "--user", "stop", "phonebridge.service") in calls


def test_public_health_failure_rolls_back_previous_release(deployment, monkeypatch):
    base, revision, calls = deployment
    previous = base / "releases/previous"
    previous.mkdir(parents=True)
    rollout.switch(previous)
    urls = []

    def health(value, url="http://127.0.0.1:18940/healthz"):
        urls.append(url)
        if url.startswith("https:"):
            raise RuntimeError("Public TLS health failed")
        return {"status": "ok", "revision": value}

    monkeypatch.setattr(rollout, "healthy", health)
    with pytest.raises(RuntimeError, match="Public TLS"):
        rollout.deploy(revision)
    assert (base / "current").resolve() == previous
    assert urls == ["http://127.0.0.1:18940/healthz", "https://bridge.example/healthz"]
    assert ("systemctl", "--user", "enable", "phonebridge.service") not in calls


def test_deploy_publishes_local_and_public_revision_evidence(deployment, monkeypatch, capsys):
    import json

    base, revision, calls = deployment
    monkeypatch.setattr(rollout, "healthy", lambda value, url="http://127.0.0.1:18940/healthz": {"status": "ok", "revision": value})
    rollout.deploy(revision)
    evidence = json.loads(capsys.readouterr().out)
    assert evidence["deployed"] is True
    assert evidence["health"]["revision"] == revision
    assert evidence["public_health"]["revision"] == revision
    assert (base / "current").resolve() == base / "releases" / revision
    assert ("systemctl", "--user", "enable", "phonebridge.service") in calls
