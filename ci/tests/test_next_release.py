import importlib.util
import io
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("next_release", Path(__file__).parents[1] / "next_release.py")
assert spec is not None and spec.loader is not None
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

HEAD = "a" * 40
OTHER = "b" * 40


@pytest.mark.parametrize("lines,expected", [
    ([], 1),
    ([f"refs/tags/phonebridge-ci-vendor-fixtures {OTHER}"], 1),
    ([f"refs/tags/v1 {OTHER}"], 2),
    ([f"refs/tags/v2 {OTHER}", f"refs/tags/v10 {OTHER}", f"refs/tags/v9 {OTHER}"], 11),
    ([f"refs/tags/v0.1.0 {OTHER}", f"refs/tags/v01 {OTHER}", f"refs/tags/v0 {OTHER}", f"refs/tags/vnext {OTHER}", f"refs/tags/v3-rc {OTHER}"], 1),
    ([f"refs/heads/v7 {OTHER}", f"refs/tags/v7/x {OTHER}", ""], 1),
])
def test_next_release_increments_highest_numeric_tag(lines, expected):
    assert release.next_release(lines, HEAD) == expected


def test_tag_already_on_revision_is_reused_for_resumed_release():
    lines = [f"refs/tags/v3 {OTHER}", f"refs/tags/v4 {HEAD}", f"refs/tags/v5 {OTHER}"]
    assert release.next_release(lines, HEAD) == 4


def test_non_release_tag_on_revision_is_ignored():
    lines = [f"refs/tags/v2 {OTHER}", f"refs/tags/phonebridge-ci-vendor-fixtures {HEAD}"]
    assert release.next_release(lines, HEAD) == 3


def test_main_writes_github_output_line(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(f"refs/tags/v4 {OTHER}\nrefs/tags/v5 {OTHER}\n"))
    monkeypatch.setattr("sys.argv", ["next_release.py", HEAD])
    release.main()
    assert capsys.readouterr().out == "number=6\n"
