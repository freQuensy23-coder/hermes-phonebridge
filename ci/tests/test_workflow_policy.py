import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("workflow_policy", Path(__file__).parents[1] / "classify_workflow_audit.py")
assert spec is not None and spec.loader is not None
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def finding(ident, severity, path=".github/workflows/deploy.yml", job="deploy"):
    return {
        "ident": ident,
        "determinations": {"severity": severity},
        "locations": [{"symbolic": {
            "key": {"Local": {"verbatim_path": path}},
            "route": {"route": [{"Key": "jobs"}, {"Key": job}, {"Key": "steps"}]},
        }}],
    }


def test_known_environment_limitation_is_visible_not_suppressed():
    item = finding("secrets-outside-env", "Medium")
    result = policy.classify([item])
    assert result["blocking"] == []
    assert result["documented_exceptions"][0]["finding"] == item
    assert result["documented_exceptions"][0]["reasons"]


@pytest.mark.parametrize("ident,severity,job", [
    ("cache-poisoning", "High", "deploy"),
    ("template-injection", "Medium", "deploy"),
    ("secrets-outside-env", "High", "deploy"),
    ("secrets-outside-env", "Medium", "untrusted"),
])
def test_unknown_or_more_severe_findings_remain_blocking(ident, severity, job):
    item = finding(ident, severity, job=job)
    result = policy.classify([item])
    assert result["blocking"] == [item]
    assert result["documented_exceptions"] == []


def test_environment_exception_does_not_cover_build_job():
    item = finding("secrets-outside-env", "Medium", ".github/workflows/release.yml", "build")
    assert policy.classify([item])["blocking"] == [item]


def test_info_and_low_findings_are_retained():
    items = [finding("anonymous-definition", "Informational"), finding("undocumented-permissions", "Low")]
    assert policy.classify(items)["non_blocking"] == items


@pytest.mark.parametrize("data", [{}, [{}], [{"ident": "x", "determinations": {"severity": "Unknown"}}]])
def test_malformed_or_unrecognized_audit_fails_closed(data):
    with pytest.raises(ValueError):
        policy.classify(data)


def test_mixed_trusted_and_untrusted_locations_are_not_excepted():
    item = finding("secrets-outside-env", "Medium")
    item["locations"] += finding("secrets-outside-env", "Medium", job="untrusted")["locations"]
    assert policy.classify([item])["blocking"] == [item]


def test_reviewed_nonexecuting_trigger_is_retained_with_explicit_reason():
    item = finding("dangerous-triggers", "High")
    result = policy.classify([item])
    assert result["blocking"] == []
    assert result["documented_exceptions"][0]["finding"] == item
    assert "remains visible" in result["documented_exceptions"][0]["reasons"][0]


def test_changed_deploy_workflow_is_not_covered_by_trigger_review(monkeypatch):
    item = finding("dangerous-triggers", "High")
    monkeypatch.setattr(policy, "REVIEWED_DEPLOY_WORKFLOW_SHA256", "0" * 64)
    assert policy.classify([item])["blocking"] == [item]


def test_other_workflow_trigger_is_not_excepted():
    item = finding("dangerous-triggers", "High", ".github/workflows/release.yml", "sign")
    assert policy.classify([item])["blocking"] == [item]
