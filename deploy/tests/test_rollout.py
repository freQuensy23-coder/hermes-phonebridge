import copy
import json
import signal
import subprocess
from types import SimpleNamespace

import pytest

SHA = "a" * 40
OLD = "b" * 40


def pull(rollout):
    return {"id": 7, "number": 1, "merged": True, "state": "closed", "merge_commit_sha": SHA,
            "base": {"ref": "main", "repo": {"full_name": rollout.REPOSITORY}},
            "head": {"repo": {"full_name": rollout.REPOSITORY}},
            "user": {"login": rollout.OWNER}, "merged_by": {"login": rollout.OWNER}}


@pytest.fixture
def producer(modules, monkeypatch):
    rollout = modules["rollout"]
    repository = {"id": 3, "full_name": rollout.REPOSITORY, "default_branch": "main"}
    identity = {"id": 3, "full_name": rollout.REPOSITORY}
    run = {"id": 10, "workflow_id": 4, "path": rollout.WORKFLOW, "head_sha": SHA,
           "head_branch": "main", "event": "push", "status": "completed", "conclusion": "success",
           "repository": identity, "head_repository": identity, "run_attempt": 2,
           "run_started_at": "2026-10-13T10:00:00Z", "updated_at": "2026-10-13T11:00:00Z"}
    jobs = [{"id": index + 1, "name": name, "run_id": 10, "run_attempt": 2,
             "head_sha": SHA, "status": "completed", "conclusion": "success"}
            for index, name in enumerate(sorted(rollout.REQUIRED_JOBS))]
    artifact = {"id": 20, "name": "phonebridge-deployment-" + SHA, "created_at": "2026-10-13T10:10:00Z",
                "expired": False, "size_in_bytes": 1000, "digest": "sha256:" + "c" * 64,
                "workflow_run": {"id": 10, "repository_id": 3, "head_repository_id": 3, "head_sha": SHA, "head_branch": "main"}}
    state = {"main": SHA, "runs": [run], "latest": run, "attempt": copy.deepcopy(run),
             "jobs": jobs, "artifacts": [artifact], "pull": pull(rollout)}
    paths = []

    def github(path):
        paths.append(path)
        suffix = path.split(rollout.REPOSITORY, 1)[1]
        if suffix == "":
            return repository
        if suffix == "/commits/main":
            return {"sha": state["main"]}
        if suffix == "/actions/workflows/ci.yml":
            return {"id": 4, "path": rollout.WORKFLOW, "state": "active"}
        if suffix == "/actions/runs/10":
            return state["latest"]
        if suffix == "/actions/runs/10/attempts/2":
            return state["attempt"]
        if suffix == "/pulls/1":
            return state["pull"]
        page = int(path.rsplit("page=", 1)[1])
        if "/commits/" in suffix:
            return [{"id": 7, "number": 1}] if page == 1 else []
        key = "runs" if "/workflows/4/runs?" in suffix else "jobs" if "/jobs?" in suffix else "artifacts"
        api_key = "workflow_runs" if key == "runs" else key
        items = state[key]
        return {"total_count": len(items), api_key: items[(page - 1) * 100:page * 100]}

    monkeypatch.setattr(rollout, "github", github)
    return rollout, state, paths


def test_exact_successful_ci_attempt(producer):
    rollout, state, paths = producer
    accepted = rollout.accepted_revision(SHA)
    assert accepted["run_id"] == 10
    assert accepted["run_attempt"] == 2
    assert accepted["artifact_id"] == 20
    assert any("attempts/2/jobs?" in path for path in paths)
    assert not any("check-runs" in path for path in paths)
    assert state["main"] == SHA


@pytest.mark.parametrize("field,value", [("event", "pull_request"), ("event", "workflow_dispatch"), ("head_branch", "v0.1.0"), ("head_branch", None), ("path", ".github/workflows/impostor.yml"), ("workflow_id", 5), ("head_sha", OLD), ("conclusion", "failure"), ("status", "in_progress")])
def test_rejects_name_only_tag_or_nonproducer_runs(producer, field, value):
    rollout, state, paths = producer
    state["latest"][field] = value
    with pytest.raises(RuntimeError, match="producer"):
        rollout.accepted_revision(SHA)
    assert not any("check-runs" in path for path in paths)


@pytest.mark.parametrize("conclusion", [None, "skipped", "failure"])
def test_deployment_requires_positive_physical_oem_job(producer, conclusion):
    rollout, state, paths = producer
    state["jobs"] = [job for job in state["jobs"] if job["name"] != "android-physical"]
    if conclusion is not None:
        state["jobs"].append(dict(state["jobs"][0], id=900, name="android-physical", conclusion=conclusion))
    with pytest.raises(RuntimeError):
        rollout.accepted_revision(SHA)


def test_stale_main_rejected_before_artifact(producer):
    rollout, state, paths = producer
    state["main"] = OLD
    with pytest.raises(RuntimeError, match="current main"):
        rollout.accepted_revision(SHA)
    assert len(paths) == 1


@pytest.mark.parametrize("target", ["latest", "attempt"])
def test_rejects_fork_run_identity(producer, target):
    rollout, state, paths = producer
    state[target] = copy.deepcopy(state[target])
    state[target]["head_repository"]["id"] = 99
    with pytest.raises(RuntimeError, match="producer"):
        rollout.accepted_revision(SHA)


def test_latest_failed_attempt_cannot_reuse_success(producer):
    rollout, state, paths = producer
    state["latest"]["run_attempt"] = 3
    state["latest"]["conclusion"] = "failure"
    with pytest.raises(RuntimeError, match="producer"):
        rollout.accepted_revision(SHA)


def test_newer_incomplete_run_blocks_historical_success(producer):
    rollout, state, paths = producer
    state["runs"].append({"id": 11})
    prior = rollout.github
    def replacement(path):
        if path.endswith("/runs/11"):
            return {**state["latest"], "id": 11, "status": "in_progress"}
        return prior(path)

    rollout.github = replacement
    with pytest.raises(RuntimeError, match="producer"):
        rollout.accepted_revision(SHA)


@pytest.mark.parametrize("field,value", [("run_attempt", 1), ("run_id", 9), ("head_sha", OLD), ("conclusion", "failure"), ("status", "queued")])
def test_cannot_mix_job_attempts(producer, field, value):
    rollout, state, paths = producer
    state["jobs"][0][field] = value
    with pytest.raises(RuntimeError, match="incoherent"):
        rollout.accepted_revision(SHA)


def test_attempt_scoped_endpoint_supports_documented_jobs_without_attempt_field(producer):
    rollout, state, paths = producer
    for job in state["jobs"]:
        job.pop("run_attempt")
    assert rollout.accepted_revision(SHA)["run_attempt"] == 2


def test_paginates_jobs_and_artifacts(producer):
    rollout, state, paths = producer
    state["jobs"] = [{**state["jobs"][0], "id": 1000 + index, "name": f"additional-{index}"} for index in range(100)] + state["jobs"]
    state["artifacts"] = [{**state["artifacts"][0], "id": 1000 + index, "name": f"other-{index}"} for index in range(100)] + state["artifacts"]
    assert rollout.accepted_revision(SHA)["artifact_id"] == 20
    assert any("jobs?per_page=100&page=2" in path for path in paths)
    assert any("artifacts?per_page=100&page=2" in path for path in paths)


def test_paginates_producer_run_selection(producer):
    rollout, state, paths = producer
    state["latest"]["id"] = 101
    state["attempt"]["id"] = 101
    state["runs"] = [{"id": index + 1} for index in range(100)] + [state["latest"]]
    for job in state["jobs"]:
        job["run_id"] = 101
    state["artifacts"][0]["workflow_run"]["id"] = 101
    prior = rollout.github
    def replacement(path):
        return prior(path.replace("/runs/101", "/runs/10"))

    rollout.github = replacement
    assert rollout.accepted_revision(SHA)["run_id"] == 101
    assert any("page=2" in path for path in paths)


def test_prior_attempt_artifact_rejected(producer):
    rollout, state, paths = producer
    state["artifacts"][0]["created_at"] = "2026-10-13T09:00:00Z"
    with pytest.raises(RuntimeError, match="latest-attempt"):
        rollout.accepted_revision(SHA)


def test_duplicate_artifact_rejected(producer):
    rollout, state, paths = producer
    state["artifacts"].append({**state["artifacts"][0], "id": 21})
    with pytest.raises(RuntimeError, match="ambiguous"):
        rollout.accepted_revision(SHA)


@pytest.mark.parametrize("field,value", [("merged", False), ("merge_commit_sha", OLD), ("state", "open")])
def test_owner_merge_is_required(producer, field, value):
    rollout, state, paths = producer
    state["pull"][field] = value
    with pytest.raises(RuntimeError, match="owner-merged"):
        rollout.accepted_revision(SHA)


@pytest.mark.parametrize("target", ["user", "merged_by"])
def test_exact_owner_identity_required(producer, target):
    rollout, state, paths = producer
    state["pull"][target]["login"] = "outsider"
    with pytest.raises(RuntimeError, match="owner-merged"):
        rollout.accepted_revision(SHA)


def test_fork_pull_rejected(producer):
    rollout, state, paths = producer
    state["pull"]["head"]["repo"]["full_name"] = "outsider/fork"
    with pytest.raises(RuntimeError, match="owner-merged"):
        rollout.accepted_revision(SHA)


@pytest.mark.parametrize("command", ["", "bash", "deploy main", "deploy v0.1.0", "deploy " + "A" * 40, "deploy " + SHA + " extra", "deploy $(id)", "deploy ../other"])
def test_entry_preserves_exact_sha_only(modules, monkeypatch, command):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", command)
    with pytest.raises(SystemExit):
        modules["rollout"].main()


@pytest.fixture
def transaction(modules, tmp_path, monkeypatch):
    rollout = modules["rollout"]
    base = tmp_path / "runtime"
    base.mkdir()
    monkeypatch.setattr(rollout, "BASE", base)
    monkeypatch.setattr(rollout, "DEADLINE", None)
    monkeypatch.setattr(rollout, "public_origin", lambda: "https://bridge.example")
    provenance = {"source_sha": SHA, "run_id": 10, "run_attempt": 2}
    monkeypatch.setattr(rollout, "accepted_revision", lambda revision: provenance)
    monkeypatch.setattr(rollout, "revalidate", lambda accepted: None)
    monkeypatch.setattr(rollout, "download_artifact", lambda accepted, target: target.write_bytes(b"zip"))
    monkeypatch.setattr(rollout, "extract_archive", lambda archive, root: None)
    monkeypatch.setattr(rollout, "validate_manifest", lambda root, accepted: {"wheels": {}})
    monkeypatch.setattr(rollout, "validate_installed_trust", lambda manifest: None)
    calls = []
    monkeypatch.setattr(rollout, "run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(rollout, "healthy", lambda revision, url="http://127.0.0.1:18940/healthz", **kwargs: {"status": "ok", "version": "0.1.0", "revision": revision})

    def create(revision):
        path = base / "releases" / (revision + "-" + "1" * 32)
        path.mkdir(parents=True)
        (path / ".venv/bin").mkdir(parents=True)
        (path / ".venv/bin/python").write_text("previous interpreter")
        (path / "revision.env").write_text(f"PHONEBRIDGE_REVISION={revision}\n")
        (path / "release.json").write_text(json.dumps({"source_sha": revision, "version": "0.1.0", "schema_generation": 1}))
        return path

    monkeypatch.setattr(rollout, "prepare_release", lambda root, manifest, accepted: create(SHA))
    return rollout, base, create, calls


def test_same_sha_preparation_never_mutates_active_release(transaction, monkeypatch):
    rollout, base, create, calls = transaction
    previous = create(SHA)
    rollout.switch(previous)
    sentinel = previous / "unique.txt"
    sentinel.write_text("active contents")
    original = {str(path.relative_to(previous)): path.read_bytes() for path in previous.rglob("*") if path.is_file()}

    def prepare(root, manifest, accepted):
        assert (base / "current").resolve() == previous
        raise RuntimeError("Preparation failed")

    monkeypatch.setattr(rollout, "prepare_release", prepare)
    with pytest.raises(RuntimeError, match="Preparation"):
        rollout.deploy(SHA)
    assert (base / "current").resolve() == previous
    assert {str(path.relative_to(previous)): path.read_bytes() for path in previous.rglob("*") if path.is_file()} == original
    assert not calls


def test_healthy_candidate_publishes_evidence_without_provisioning(transaction, capsys):
    rollout, base, create, calls = transaction
    rollout.deploy(SHA)
    result = json.loads(capsys.readouterr().out)
    assert result["health"]["version"] == "0.1.0"
    assert result["public_health"]["revision"] == SHA
    assert not (base / "transaction.json").exists()
    assert not any("provision" in " ".join(call) or "sudo" in call for call in calls)


def test_candidate_failure_verifies_previous_local_and_public_health(transaction, monkeypatch):
    rollout, base, create, calls = transaction
    previous = create(OLD)
    rollout.switch(previous)
    reads = []

    def health(revision, url="http://127.0.0.1:18940/healthz", **kwargs):
        reads.append((revision, url))
        if revision == SHA:
            raise RuntimeError("Candidate unhealthy")
        return {"status": "ok", "version": "0.1.0", "revision": revision}

    monkeypatch.setattr(rollout, "healthy", health)
    with pytest.raises(RuntimeError, match="Candidate unhealthy"):
        rollout.deploy(SHA)
    assert (base / "current").resolve() == previous
    assert reads[-2:] == [(OLD, "http://127.0.0.1:18940/healthz"), (OLD, "https://bridge.example/healthz")]
    assert calls.count(("systemctl", "--user", "restart", "phonebridge.service")) == 2
    assert json.loads((base / "last-result.json").read_text())["rollback"]["public_health"]["revision"] == OLD


def test_both_original_and_rollback_health_failures_preserved(transaction, monkeypatch):
    rollout, base, create, calls = transaction
    previous = create(OLD)
    rollout.switch(previous)
    switched = False

    def health(revision, url="http://127.0.0.1:18940/healthz", **kwargs):
        nonlocal switched
        if revision == SHA:
            switched = True
            raise RuntimeError("Candidate unhealthy")
        if switched:
            raise RuntimeError("Rollback unhealthy")
        return {"status": "ok", "version": "0.1.0", "revision": revision}

    monkeypatch.setattr(rollout, "healthy", health)
    with pytest.raises(RuntimeError, match="rollback_failure"):
        rollout.deploy(SHA)
    result = json.loads((base / "last-result.json").read_text())
    assert "Candidate unhealthy" in result["original_failure"]
    assert "Rollback unhealthy" in result["rollback_failure"]
    assert (base / "transaction.json").exists()


def test_first_deployment_failure_stops_and_clears_link(transaction, monkeypatch):
    rollout, base, create, calls = transaction
    monkeypatch.setattr(rollout, "healthy", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("unhealthy")))
    with pytest.raises(RuntimeError, match="unhealthy"):
        rollout.deploy(SHA)
    assert not (base / "current").is_symlink()
    assert ("systemctl", "--user", "stop", "phonebridge.service") in calls


def test_pending_interrupted_transaction_recovered_before_new_gate(transaction, monkeypatch):
    rollout, base, create, calls = transaction
    previous, candidate = create(OLD), create(SHA)
    rollout.switch(candidate)
    rollout.atomic_json(base / "transaction.json", {"phase": "switching", "candidate": str(candidate), "previous": str(previous)})
    rollout.recover("https://bridge.example")
    assert (base / "current").resolve() == previous
    assert not (base / "transaction.json").exists()


def test_signal_after_switch_rolls_back(transaction, monkeypatch):
    rollout, base, create, calls = transaction
    previous = create(OLD)
    rollout.switch(previous)
    normal = rollout.run

    def interrupted(*args, **kwargs):
        if (base / "current").resolve() != previous:
            rollout.interrupted(signal.SIGTERM, None)
        return normal(*args, **kwargs)

    monkeypatch.setattr(rollout, "run", interrupted)
    with pytest.raises(RuntimeError, match="signal"):
        rollout.deploy(SHA)
    assert (base / "current").resolve() == previous


def test_external_rollback_target_rejected(transaction, tmp_path):
    rollout, base, create, calls = transaction
    outside = tmp_path / "other"
    outside.mkdir()
    with pytest.raises(RuntimeError, match="containment"):
        rollout.rollback(outside, "https://bridge.example")
    assert not calls


def test_hung_subprocess_kills_process_group(modules, monkeypatch):
    rollout = modules["rollout"]
    kills = []
    process = SimpleNamespace(pid=123, wait=lambda timeout: (_ for _ in ()).throw(subprocess.TimeoutExpired("gh", timeout)) if timeout != 5 else 0)
    monkeypatch.setattr(rollout.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(rollout.os, "killpg", lambda *args: kills.append(args))
    with pytest.raises(TimeoutError, match="subprocess deadline"):
        rollout.run("gh", timeout=1)
    assert kills == [(123, signal.SIGKILL)]


def test_bounded_lock_wait(modules, tmp_path, monkeypatch):
    rollout = modules["rollout"]
    monkeypatch.setattr(rollout.fcntl, "flock", lambda *args: (_ for _ in ()).throw(BlockingIOError()))
    with pytest.raises(TimeoutError, match="lock"):
        with rollout.deployment_lock(tmp_path / "lock", timeout=0):
            pytest.fail("Lock must not be acquired")


@pytest.mark.parametrize("violation", ["wrong_total", "duplicate", "changing_total"])
def test_incomplete_or_changing_pagination_fails(modules, monkeypatch, violation):
    rollout = modules["rollout"]
    first = [{"id": index + 1} for index in range(100)]

    def github(path):
        if violation == "wrong_total":
            return {"total_count": 2, "jobs": [{"id": 1}]}
        if path.endswith("page=1"):
            return {"total_count": 101, "jobs": first}
        return {"total_count": 102 if violation == "changing_total" else 101, "jobs": [{"id": 1 if violation == "duplicate" else 101}]}

    monkeypatch.setattr(rollout, "github", github)
    with pytest.raises(RuntimeError, match="paginated|pagination"):
        rollout.pages("jobs", "jobs")


def test_stale_main_after_preparation_never_switches(transaction, monkeypatch):
    rollout, base, create, calls = transaction
    previous = create(OLD)
    rollout.switch(previous)
    monkeypatch.setattr(rollout, "revalidate", lambda accepted: (_ for _ in ()).throw(RuntimeError("Revision is not current main")))
    with pytest.raises(RuntimeError, match="current main"):
        rollout.deploy(SHA)
    assert (base / "current").resolve() == previous
    assert not calls


@pytest.mark.parametrize("damage", ["missing", "metadata", "schema", "interpreter"])
def test_damaged_previous_release_blocks_promotion(transaction, damage):
    rollout, base, create, calls = transaction
    previous = create(OLD)
    rollout.switch(previous)
    if damage == "missing":
        (previous / "release.json").unlink()
    elif damage == "metadata":
        (previous / "revision.env").write_text("wrong revision")
    elif damage == "schema":
        data = json.loads((previous / "release.json").read_text())
        data["schema_generation"] = 2
        (previous / "release.json").write_text(json.dumps(data))
    else:
        (previous / ".venv/bin/python").unlink()
    with pytest.raises((RuntimeError, OSError)):
        rollout.deploy(SHA)
    assert (base / "current").resolve() == previous
    assert not calls


def test_rollback_restart_failure_preserves_candidate_failure(transaction, monkeypatch):
    rollout, base, create, calls = transaction
    previous = create(OLD)
    rollout.switch(previous)
    normal = rollout.run
    candidate_started = False

    def run(*args, **kwargs):
        nonlocal candidate_started
        if (base / "current").resolve() != previous:
            candidate_started = True
            raise RuntimeError("Candidate restart failed")
        if candidate_started:
            raise RuntimeError("Rollback restart failed")
        return normal(*args, **kwargs)

    monkeypatch.setattr(rollout, "run", run)
    with pytest.raises(RuntimeError, match="rollback_failure"):
        rollout.deploy(SHA)
    result = json.loads((base / "last-result.json").read_text())
    assert "Candidate restart failed" in result["original_failure"]
    assert "Rollback restart failed" in result["rollback_failure"]
