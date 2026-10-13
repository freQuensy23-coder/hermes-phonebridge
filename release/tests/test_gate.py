import copy

import pytest

from release.gate import CI_PATH, OWNER, REPOSITORY, RELEASE_PATH, REQUIRED_JOBS, GitHub, accepted_pr, artifact_bytes, authorize, current_binding, peeled_tag

MAIN = "a" * 40
SOURCE = "b" * 40
DIGEST = "c" * 64


class FakeAPI:
    def __init__(self):
        repository = {"id": 7, "full_name": REPOSITORY, "default_branch": "main", "owner": {"login": OWNER}}
        self.repository = repository
        self.run = {"id": 10, "workflow_id": 9, "path": CI_PATH, "event": "push", "head_branch": "main", "head_sha": SOURCE, "repository": repository, "head_repository": repository, "actor": {"login": OWNER}, "triggering_actor": {"login": OWNER}, "status": "completed", "conclusion": "success", "run_attempt": 2, "run_started_at": "2026-01-01T02:00:00Z"}
        self.jobs = [{"id": index + 20, "name": name, "run_id": 10, "run_attempt": 2, "head_sha": SOURCE, "started_at": "2026-01-01T02:01:00Z", "completed_at": "2026-01-01T02:05:00Z", "status": "completed", "conclusion": "success"} for index, name in enumerate(sorted(REQUIRED_JOBS))]
        self.artifact = {"id": 80, "name": "phonebridge-release-input-" + SOURCE, "expired": False, "size_in_bytes": 1024, "digest": "sha256:" + DIGEST, "created_at": "2026-01-01T02:03:00Z", "updated_at": "2026-01-01T02:03:00Z", "workflow_run": {"id": 10, "repository_id": 7, "head_repository_id": 7, "head_branch": "main", "head_sha": SOURCE}}
        self.main_run = dict(self.run, id=110, head_sha=MAIN)
        self.main_artifact = copy.deepcopy(self.artifact)
        self.main_artifact.update(id=180, name="phonebridge-release-input-" + MAIN)
        self.main_artifact["workflow_run"].update(id=110, head_sha=MAIN)
        self.main_jobs = [dict(job, id=job["id"] + 100, run_id=110, head_sha=MAIN) for job in self.jobs]
        self.pulls = {1: dict(self.pull(SOURCE), number=1), 2: dict(self.pull(MAIN), number=2)}
        self.runs = [{"id": 10}]
        self.artifacts = [self.artifact]
        self.tag_object = {"type": "commit", "sha": SOURCE}
        self.calls = []

    def pull(self, sha):
        return {"state": "closed", "merged": True, "merge_commit_sha": sha, "user": {"login": OWNER}, "merged_by": {"login": OWNER}, "base": {"ref": "main", "repo": {"full_name": REPOSITORY}}, "head": {"repo": {"full_name": REPOSITORY}}}

    def get(self, path):
        self.calls.append(path)
        values = {"": self.repository, "git/ref/heads/main": {"object": {"sha": MAIN}}, "git/ref/tags/v0.1.0": {"ref": "refs/tags/v0.1.0", "object": self.tag_object}, "compare/" + SOURCE + "..." + MAIN: {"status": "ahead", "merge_base_commit": {"sha": SOURCE}}, "actions/workflows/ci.yml": {"id": 9, "path": CI_PATH, "state": "active"}, "actions/runs/10": self.run, "actions/runs/110": self.main_run, "actions/artifacts/80": self.artifact, "actions/artifacts/180": self.main_artifact, "pulls/1": self.pulls[1], "pulls/2": self.pulls[2]}
        return copy.deepcopy(values[path])

    def pages(self, path, key=None):
        self.calls.append(path)
        if path == "commits/" + MAIN + "/pulls":
            return [{"id": 2, "number": 2, "merged": None}]
        if path == "commits/" + SOURCE + "/pulls":
            return [{"id": 1, "number": 1, "merged": None}]
        if path == "actions/runs/10/attempts/2/jobs":
            return copy.deepcopy(self.jobs)
        if path == "actions/runs/10/artifacts":
            return copy.deepcopy(self.artifacts)
        if path == "actions/runs/110/attempts/2/jobs":
            return copy.deepcopy(self.main_jobs)
        if path == "actions/runs/110/artifacts":
            return [copy.deepcopy(self.main_artifact)]
        if path.startswith("actions/workflows/9/runs?") and path.endswith(MAIN):
            return [{"id": 110}]
        if path.startswith("actions/workflows/9/runs?"):
            return copy.deepcopy(self.runs)
        raise AssertionError(path)


@pytest.fixture
def api(monkeypatch):
    for name, value in {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR": OWNER, "GITHUB_TRIGGERING_ACTOR": OWNER, "GITHUB_SHA": MAIN, "GITHUB_WORKFLOW_SHA": MAIN, "GITHUB_WORKFLOW_REF": REPOSITORY + "/" + RELEASE_PATH + "@refs/heads/main", "TAG": "v0.1.0", "SOURCE_SHA": SOURCE, "ALLOW_HISTORICAL": "true"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("EXPECTED_BINDING", raising=False)
    return FakeAPI()


def run_gate(api, historical="true", tag="v0.1.0", workflow_sha=MAIN, workflow_ref=None):
    return authorize(api, tag, SOURCE, historical, workflow_sha, workflow_ref or REPOSITORY + "/" + RELEASE_PATH + "@refs/heads/main")


def test_historical_accepted_main_and_full_pr_fetch(api):
    binding = run_gate(api)
    assert binding["source_sha"] == SOURCE
    assert binding["ci_run_attempt"] == 2
    assert binding["input_artifact_id"] == 80
    assert "pulls/1" in api.calls
    assert "pulls/2" in api.calls
    assert "actions/runs/10/attempts/2/jobs" in api.calls


def test_historical_requires_explicit_opt_in(api):
    with pytest.raises(ValueError, match="explicit"):
        run_gate(api, historical="false")


@pytest.mark.parametrize("field,value", [("path", ".github/workflows/spoof.yml"), ("workflow_id", 8), ("event", "pull_request"), ("head_branch", "feat/evil"), ("head_sha", MAIN), ("conclusion", "failure"), ("status", "in_progress")])
def test_run_provenance_fail_closed(api, field, value):
    api.run[field] = value
    with pytest.raises(ValueError):
        run_gate(api)


@pytest.mark.parametrize("target", ["actor", "triggering_actor"])
def test_ci_actor_exact_owner(api, target):
    api.run[target]["login"] = "collaborator"
    with pytest.raises(ValueError):
        run_gate(api)


def test_wrong_head_repository(api):
    api.run["head_repository"] = {"id": 42, "full_name": "attacker/fork"}
    with pytest.raises(ValueError):
        run_gate(api)


@pytest.mark.parametrize("conclusion", [None, "skipped", "failure"])
def test_release_requires_positive_physical_oem_job(api, conclusion):
    api.jobs = [job for job in api.jobs if job["name"] != "android-physical"]
    if conclusion is not None:
        api.jobs.append(dict(api.jobs[0], id=900, name="android-physical", conclusion=conclusion))
    with pytest.raises(ValueError):
        run_gate(api)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "failed", "old-time", "old-attempt", "wrong-sha"])
def test_required_jobs_one_coherent_attempt(api, mutation):
    job = api.jobs[0]
    if mutation == "missing":
        api.jobs.pop(0)
    elif mutation == "duplicate":
        duplicate = dict(job, id=500)
        api.jobs.append(duplicate)
    elif mutation == "failed":
        job["conclusion"] = "failure"
    elif mutation == "old-time":
        job["started_at"] = "2026-01-01T01:00:00Z"
    elif mutation == "old-attempt":
        job["run_attempt"] = 1
    else:
        job["head_sha"] = MAIN
    with pytest.raises(ValueError):
        run_gate(api)


@pytest.mark.parametrize("field,value", [("expired", True), ("digest", ""), ("size_in_bytes", 999999999), ("created_at", "2026-01-01T01:00:00Z"), ("updated_at", "2026-01-01T03:00:00Z"), ("name", "spoof")])
def test_artifact_contract_provenance(api, field, value):
    api.artifact[field] = value
    with pytest.raises(ValueError):
        run_gate(api)


def test_artifact_ambiguity(api):
    api.artifacts.append(dict(api.artifact, id=81))
    with pytest.raises(ValueError):
        run_gate(api)


@pytest.mark.parametrize("field", ["head_sha", "head_branch", "repository_id", "head_repository_id", "id"])
def test_artifact_run_binding(api, field):
    api.artifact["workflow_run"][field] = "wrong" if field.startswith("head_") and field != "head_repository_id" else 999
    with pytest.raises(ValueError):
        run_gate(api)


@pytest.mark.parametrize("mutation", ["author", "merger", "fork", "open", "not-merged", "not-exact-merge"])
def test_exact_owner_merged_pr_required(api, mutation):
    pull = api.pulls[1]
    if mutation == "author":
        pull["user"]["login"] = "other"
    elif mutation == "merger":
        pull["merged_by"]["login"] = "other"
    elif mutation == "fork":
        pull["head"]["repo"]["full_name"] = "other/fork"
    elif mutation == "open":
        pull["state"] = "open"
    elif mutation == "not-merged":
        pull["merged"] = False
    else:
        pull["merge_commit_sha"] = MAIN
    with pytest.raises(ValueError):
        accepted_pr(api, SOURCE)


def test_unreviewed_historical_ancestor_rejected(api):
    api.pulls[1]["merge_commit_sha"] = "d" * 40
    with pytest.raises(ValueError):
        run_gate(api)


def test_moved_tag_rejected(api):
    api.tag_object["sha"] = MAIN
    with pytest.raises(ValueError, match="Tag moved"):
        run_gate(api)


@pytest.mark.parametrize("tag", ["v0.1", "v0.1.0-rc1", "v1.2.3", "v0.1.0/evil", "0.1.0"])
def test_precise_tag_grammar(api, tag):
    with pytest.raises(ValueError):
        run_gate(api, tag=tag)


def test_tag_owned_workflow_is_not_trusted(api):
    with pytest.raises(ValueError):
        run_gate(api, workflow_ref=REPOSITORY + "/" + RELEASE_PATH + "@refs/tags/v0.1.0")


def test_main_movement_rejected(api):
    with pytest.raises(ValueError):
        run_gate(api, workflow_sha=SOURCE)


def test_binding_revalidation_detects_change(api, monkeypatch):
    import json

    binding = run_gate(api)
    binding["ci_run_attempt"] = 1
    monkeypatch.setenv("EXPECTED_BINDING", json.dumps(binding))
    with pytest.raises(ValueError, match="changed"):
        current_binding(api)


def test_annotated_tag_resolution(api, monkeypatch):
    old_get = api.get
    api.tag_object = {"type": "tag", "sha": "d" * 40}
    monkeypatch.setattr(api, "get", lambda path: {"object": {"type": "commit", "sha": SOURCE}} if path.startswith("git/tags/") else old_get(path))
    assert peeled_tag(api, "v0.1.0") == SOURCE


def test_newer_failed_run_does_not_fall_back(api):
    api.runs.append({"id": 11})
    old_get = api.get
    api.get = lambda path: dict(api.run, id=11, conclusion="failure") if path == "actions/runs/11" else old_get(path)
    with pytest.raises(ValueError, match="Latest"):
        run_gate(api)


def test_attempt_changes_during_authorization(api):
    old_get = api.get
    reads = []

    def get(path):
        result = old_get(path)
        if path == "actions/runs/10":
            reads.append(path)
            if len(reads) == 2:
                result["run_attempt"] = 3
        return result

    api.get = get
    with pytest.raises(ValueError, match="attempt changed"):
        run_gate(api)


def test_pagination_reads_all_pages(monkeypatch):
    client = GitHub("fake-nonsecret")
    calls = []

    def page(path):
        calls.append(path)
        page_number = int(path.rsplit("page=", 1)[1])
        entries = [{"id": value} for value in (range(100) if page_number == 1 else range(100, 105))]
        return {"total_count": 105, "jobs": entries}

    monkeypatch.setattr(client, "get", page)
    assert len(client.pages("actions/runs/10/attempts/2/jobs", "jobs")) == 105
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["missing", "duplicates", "changing-total"])
def test_pagination_incomplete_or_ambiguous_rejected(monkeypatch, kind):
    client = GitHub("fake-nonsecret")

    def page(path):
        page_number = int(path.rsplit("page=", 1)[1])
        if page_number == 1:
            return {"total_count": 101, "artifacts": [{"id": value} for value in range(100)]}
        values = [] if kind == "missing" else [{"id": 0 if kind == "duplicates" else 100}]
        return {"total_count": 102 if kind == "changing-total" else 101, "artifacts": values}

    monkeypatch.setattr(client, "get", page)
    with pytest.raises(ValueError):
        client.pages("actions/runs/10/artifacts", "artifacts")


def test_transport_digest_verified():
    class Download:
        def download(self, path):
            return b"archive bytes"

    with pytest.raises(ValueError, match="digest"):
        artifact_bytes(Download(), 80, "sha256:" + DIGEST)


def test_historical_release_requires_successful_current_trust_root(api):
    api.main_run["conclusion"] = "failure"
    with pytest.raises(ValueError, match="Latest"):
        run_gate(api)


def test_historical_release_binds_current_trust_root_attempt(api):
    binding = run_gate(api)
    assert binding["trusted_ci_run_id"] == 110
    assert binding["trusted_ci_run_attempt"] == 2


def test_current_main_release_requires_no_historical_opt_in(api):
    api.tag_object["sha"] = MAIN
    binding = authorize(api, "v0.1.0", MAIN, "false", MAIN, REPOSITORY + "/" + RELEASE_PATH + "@refs/heads/main")
    assert binding["source_sha"] == MAIN
    assert binding["ci_run_id"] == 110
    assert binding["trusted_ci_run_id"] == 110


def test_missing_job_attempt_is_not_assumed(api):
    del api.jobs[0]["run_attempt"]
    with pytest.raises(ValueError, match="missing"):
        run_gate(api)


def test_tag_is_rebound_at_end_of_authorization(api):
    old_get = api.get
    tag_reads = []

    def get(path):
        result = old_get(path)
        if path == "git/ref/tags/v0.1.0":
            tag_reads.append(path)
            if len(tag_reads) == 2:
                result["object"]["sha"] = MAIN
        return result

    api.get = get
    with pytest.raises(ValueError, match="during authorization"):
        run_gate(api)


def test_trust_root_is_rebound_at_end_of_authorization(api):
    old_get = api.get
    reads = []

    def get(path):
        result = old_get(path)
        if path == "git/ref/heads/main":
            reads.append(path)
            if len(reads) == 2:
                result["object"]["sha"] = SOURCE
        return result

    api.get = get
    with pytest.raises(ValueError, match="main moved during"):
        run_gate(api)
