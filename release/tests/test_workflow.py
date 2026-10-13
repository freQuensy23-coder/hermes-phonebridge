import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def workflow_text():
    return (ROOT / ".github/workflows/release.yml").read_text()


def test_inline_workflow_code_matches_owned_source_exactly():
    text = workflow_text()
    literals = re.findall(r"^          source = (.+)$", text, re.MULTILINE)
    sources = [(ROOT / "release" / name).read_text() for name in ("gate.py", "payload.py", "runtime.py")]
    assert [ast.literal_eval(value) for value in literals] == sources * 3


def test_every_inline_run_is_within_github_limit():
    text = workflow_text()
    blocks = re.findall(r"^        run: \|\n((?:          .*\n)+)", text, re.MULTILINE)
    assert len(blocks) == 13
    for block in blocks:
        script = "\n".join(line[10:] for line in block.splitlines())
        assert len(script) <= 21000


def test_no_tag_owned_checkout_build_or_environment():
    text = workflow_text()
    assert "workflow_dispatch:" in text
    assert "    tags:" not in text
    assert "environment:" not in text
    assert "actions/checkout@" not in text
    assert "sdkmanager" not in text
    assert "setup-gradle" not in text
    assert "pip install" not in text
    assert "deploy/" not in text
    assert text.count("runs-on: ubuntu-24.04") == 3


def test_signing_secrets_only_in_isolated_sign_step():
    text = workflow_text()
    assert text.count("${{ secrets.ANDROID_KEYSTORE_BASE64 }}") == 1
    assert text.count("${{ secrets.ANDROID_KEYSTORE_PASSWORD }}") == 1
    sign = text.split("  sign:\n", 1)[1].split("  publish:\n", 1)[0]
    assert "SIGNING_KEY: ${{ secrets.ANDROID_KEYSTORE_BASE64 }}" in sign
    assert "contents: write" not in sign
    assert "PHONEBRIDGE_SIGNER_SHA256" in sign


def test_publisher_has_only_one_credential_injection_and_no_actions():
    publish = workflow_text().split("  publish:\n", 1)[1]
    assert "contents: write" in publish
    assert publish.count("GH_TOKEN:") == 1
    assert "uses:" not in publish
    assert "secrets." not in publish


def test_only_verified_full_sha_action_reference():
    references = re.findall(r"uses: ([^\n]+)", workflow_text())
    assert references == ["actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"]


def test_no_new_python_comments_or_docstrings():
    import io
    import tokenize

    for path in (ROOT / "release").rglob("*.py"):
        source = path.read_text()
        assert not any(token.type == tokenize.COMMENT for token in tokenize.generate_tokens(io.StringIO(source).readline))
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                assert ast.get_docstring(node) is None
