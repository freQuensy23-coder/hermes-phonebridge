import json
from pathlib import Path
import shlex

import pytest

WORKFLOW = Path(__file__).parents[2] / ".github/workflows/deploy.yml"


def gate_commands():
    lines = WORKFLOW.read_text().splitlines()
    guards = [shlex.split(line.strip())[2] for line in lines if line.strip().startswith("python -c 'import os,re;") or (line.strip().startswith("python -c 'import json,os; from pathlib") and "upstream.json" in line)]
    assert len(guards) == 2
    return guards


@pytest.fixture
def metadata(tmp_path, monkeypatch):
    revision = "a" * 40
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("RUN_ID", "12345")
    monkeypatch.setenv("REVISION", revision)
    repo = "freQuensy23-coder/hermes-phonebridge"
    data = {
        "id": 12345,
        "repository": {"full_name": repo},
        "head_repository": {"full_name": repo},
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "head_branch": "main",
        "status": "completed",
        "conclusion": "success",
        "head_sha": revision,
        "actor": {"login": "freQuensy23-coder"},
        "triggering_actor": {"login": "freQuensy23-coder"},
        "run_attempt": 1,
    }
    main = {"sha": revision}
    return tmp_path, data, main


def execute(metadata):
    root, data, main = metadata
    (root / "upstream.json").write_text(json.dumps(data))
    (root / "main.json").write_text(json.dumps(main))
    for code in gate_commands():
        exec(compile(code, str(WORKFLOW), "exec"), {})


def test_current_successful_owner_main_ci_passes_exact_inline_guard(metadata, capsys):
    execute(metadata)
    assert "Accepted CI 12345" in capsys.readouterr().out


@pytest.mark.parametrize("field,value", [
    ("id", 67890),
    ("repository", {"full_name": "outsider/repository"}),
    ("head_repository", {"full_name": "outsider/repository"}),
    ("path", ".github/workflows/fake.yml"),
    ("event", "pull_request"),
    ("head_branch", "feat/untrusted"),
    ("status", "in_progress"),
    ("conclusion", "failure"),
    ("head_sha", "b" * 40),
    ("actor", {"login": "outsider"}),
    ("triggering_actor", {"login": "outsider"}),
])
def test_mismatched_ci_identity_fails_before_secret_step(metadata, field, value):
    metadata[1][field] = value
    with pytest.raises(AssertionError):
        execute(metadata)


def test_stale_main_fails_before_secret_step(metadata):
    metadata[2]["sha"] = "b" * 40
    with pytest.raises(AssertionError):
        execute(metadata)


@pytest.mark.parametrize("name,value", [("RUN_ID", "1/../../other"), ("REVISION", "$(id)"), ("REVISION", "main")])
def test_malformed_context_fails_before_api_or_secret_use(metadata, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(AssertionError):
        execute(metadata)


def test_deploy_runner_does_not_checkout_or_execute_upstream_artifacts():
    text = WORKFLOW.read_text()
    assert "uses:" not in text
    assert "checkout" not in text
    assert "download-artifact" not in text
    assert text.index("Bind completed CI producer") < text.index("SSH_KEY:")
    assert "StrictHostKeyChecking=yes" in text
    assert "umask 077" in text
