import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("check_no_comments", ROOT / "ci/check_no_comments.py")
assert spec is not None and spec.loader is not None
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def lines(name, text):
    return sorted(set(checker.scanner_for(Path(name), text)(text)))


@pytest.mark.parametrize("name,text,expected", [
    ("a.py", "#!/usr/bin/env python3\nx = 1\n", []),
    ("a.py", "x = 1  # note\n", [1]),
    ("a.py", "def f():\n    \"\"\"Doc.\"\"\"\n    return 1\n", [2]),
    ("a.py", "x = 1\n'''block'''\n", [2]),
    ("a.py", "x = '#' + \"# not\"\ny = f\"{x}#\"\n", []),
    ("a.kt", "val a = 1 // note\n", [1]),
    ("a.kt", "/**\n * Doc\n */\nfun f() = 1\n", [1]),
    ("a.kt", "/* outer /* inner */ still */ val a = 1\n", [1]),
    ("a.kt", "val url = \"https://example.com/*x*/\"\n", []),
    ("a.kt", "val c = '\"'\nval d = 2 // x\n", [2]),
    ("a.kt", "val q = '\\''\nval s = \"//\"\n", []),
    ("a.kt", "val r = \"\"\"\n// raw\n/* raw */\n\"\"\"\n", []),
    ("a.kt", "val t = \"${map[\"//\"]} //\"\n", []),
    ("a.kt", "val u = \"${f { 1 } /* c */}\"\n", [1]),
    ("a.kt", "val v = \"\"\"${\"\\\"\"}\"\"\"\" // c\n", [1]),
    ("a.kts", "#!/usr/bin/env kotlin\nprintln(1)\n", []),
    ("a.xml", "<a>\n<!-- c -->\n</a>\n", [2]),
    ("a.xml", "<a><![CDATA[<!-- data -->]]></a>\n", []),
    ("a.sh", "#!/bin/bash\necho hi\n", []),
    ("a.sh", "echo hi # note\n", [1]),
    ("a.sh", "  # note\n", [1]),
    ("a.sh", "echo $# ${#a[@]} a#b '#' \"#\" $'\\'#'\n", []),
    ("a.sh", "echo 'multi\n# line'\n", []),
    ("a.sh", "cat <<'EOF'\n# data\nEOF\necho x # c\n", [4]),
    ("a.sh", "x=$(( 1 << 2 )) # c\n", [1]),
    ("a.yml", "a: 1 # c\n", [1]),
    ("a.yml", "# c\na: 1\n", [1]),
    ("a.yml", "a: '#x'\nb: \"#y\"\nc: d#e\n", []),
    ("a.yml", "a: |  # c\n  text\n", [1]),
    ("a.yml", "steps:\n  - run: |\n      echo hi\n      # note\n", [4]),
    ("a.yml", "steps:\n  - run: echo ${#X} \"$#\"\n", []),
    ("a.yml", "steps:\n  - run: |\n      python3 - <<'PY'\n      # data\n      PY\n      echo x # c\n", [6]),
    ("a.toml", "a = 1 # c\n", [1]),
    ("a.toml", "a = \"#\"\nb = '#'\nc = \"\"\"\n#\n\"\"\"\nd = '''\n#'''\n", []),
    ("requirements-dev.txt", "pytest==8.3.5 # c\nfoo @ https://x/y#egg=foo\n", [1]),
    ("Dockerfile", "FROM x\n# c\nRUN echo a # b\nCMD [\"a\", \"#\"]\n", [2, 3]),
    (".gitignore", "# c\nbuild/\n", [1]),
    ("a.properties", "! c\n# d\na=b#c\n", [1, 2]),
    ("a.service", "; c\n[Unit]\n", [1]),
    ("a.bat", "@rem c\nREM d\n:: e\n@echo off\n", [1, 2, 3]),
])
def test_comment_detection(name, text, expected):
    assert lines(name, text) == expected


@pytest.mark.parametrize("name,text", [
    ("README.md", "# Title\n"),
    ("data.json", "{\"a\": \"#\"}\n"),
    ("notes.txt", "# not code\n"),
    ("tool", "plain text\n# no shebang\n"),
])
def test_non_code_files_are_not_scanned(name, text):
    assert checker.scanner_for(Path(name), text) is None


def test_extensionless_scripts_use_their_shebang():
    assert checker.scanner_for(Path("tool"), "#!/bin/sh\n") is checker.shell_comments
    assert checker.scanner_for(Path("tool"), "#!/usr/bin/env python3\n") is checker.python_comments


def test_cli_reports_every_commented_file(tmp_path, capsys):
    (tmp_path / "bad.py").write_text("x = 1  # c\n")
    (tmp_path / "bad.kt").write_text("// c\n")
    (tmp_path / "good.py").write_text("x = 1\n")
    paths = [str(tmp_path / name) for name in ("bad.py", "bad.kt", "good.py")]
    assert checker.main(paths) == 1
    output = capsys.readouterr().out
    assert f"{tmp_path / 'bad.py'}:1: comments are forbidden" in output
    assert f"{tmp_path / 'bad.kt'}:1: comments are forbidden" in output
    assert "good.py" not in output


def test_repository_has_no_comments():
    names = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True).stdout.decode().split("\0")
    checked, findings = checker.scan([ROOT / name for name in names if name])
    assert checked > 100
    assert findings == []
