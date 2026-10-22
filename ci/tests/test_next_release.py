import importlib.util
import io
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("next_release", Path(__file__).parents[1] / "next_release.py")
assert spec is not None and spec.loader is not None
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.mark.parametrize("refs,expected", [
    ([], 1),
    (["refs/tags/phonebridge-ci-vendor-fixtures"], 1),
    (["refs/tags/v1"], 2),
    (["refs/tags/v2", "refs/tags/v10", "refs/tags/v9"], 11),
    (["refs/tags/v0.1.0", "refs/tags/v01", "refs/tags/v0", "refs/tags/vnext", "refs/tags/v3-rc"], 1),
    (["refs/heads/v7", "refs/tags/v7/x"], 1),
])
def test_next_release_increments_highest_numeric_tag(refs, expected):
    assert release.next_release(refs) == expected


def test_main_writes_github_output_line(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("refs/tags/v4\nrefs/tags/v5\n"))
    release.main()
    assert capsys.readouterr().out == "number=6\n"
