import json
import os
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

from phonebridge import __version__


CLI_ROOT = Path(__file__).resolve().parents[1]


def run(arguments, cwd, env=None):
    return subprocess.run(arguments, cwd=cwd, env=env, check=True, text=True, capture_output=True, timeout=180)


def test_package_metadata_and_direct_dependency_pins():
    with (CLI_ROOT / "pyproject.toml").open("rb") as stream:
        metadata = tomllib.load(stream)
    assert metadata["project"]["name"] == "phonebridge-cli"
    assert metadata["project"]["version"] == __version__ == "0.1.0"
    assert metadata["project"]["requires-python"] == ">=3.11"
    assert metadata["project"]["scripts"] == {"phonebridge": "phonebridge.cli:main"}
    dependencies = metadata["project"]["dependencies"] + metadata["project"]["optional-dependencies"]["test"] + metadata["build-system"]["requires"]
    assert all("==" in dependency and "*" not in dependency for dependency in dependencies)


def test_wheel_sdist_installed_entry_and_skill_resource(tmp_path, wire):
    artifacts = tmp_path / "artifacts"
    run([sys.executable, "-m", "build", str(CLI_ROOT), "--outdir", str(artifacts)], cwd=tmp_path)
    wheel = next(artifacts.glob("phonebridge_cli-0.1.0-*.whl"))
    sdist = next(artifacts.glob("phonebridge_cli-0.1.0.tar.gz"))
    canonical = CLI_ROOT.parent / "skills" / "phonebridge" / "SKILL.md"
    with zipfile.ZipFile(wheel) as archive:
        assert archive.read("phonebridge/resources/SKILL.md") == canonical.read_bytes()
        entries = archive.read("phonebridge_cli-0.1.0.dist-info/entry_points.txt").decode()
        assert "phonebridge = phonebridge.cli:main" in entries
        assert archive.read("phonebridge/_resolve.py")
        assert archive.read("phonebridge/transport.py")
    extracted = tmp_path / "sdist"
    extracted.mkdir()
    with tarfile.open(sdist) as archive:
        archive.extractall(extracted, filter="data")
    standalone = next(extracted.iterdir())
    standalone_wheels = tmp_path / "standalone-wheels"
    run([sys.executable, "-m", "build", "--wheel", str(standalone), "--outdir", str(standalone_wheels)], cwd=tmp_path)
    standalone_wheel = next(standalone_wheels.glob("*.whl"))
    with zipfile.ZipFile(standalone_wheel) as archive:
        assert archive.read("phonebridge/resources/SKILL.md") == canonical.read_bytes()
    installed = tmp_path / "installed"
    run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-index", "--target", str(installed), str(standalone_wheel)], cwd=tmp_path)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(installed)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HERMES_HOME"] = str(tmp_path / "isolated-hermes")
    env.pop("PHONEBRIDGE_TOKEN", None)
    env.pop("PHONEBRIDGE_URL", None)
    assert run([str(installed / "bin" / "phonebridge"), "--version"], cwd=tmp_path, env=env).stdout.strip() == "0.1.0"
    assert run([sys.executable, "-m", "phonebridge", "--version"], cwd=tmp_path, env=env).stdout.strip() == "0.1.0"
    result = run([str(installed / "bin" / "phonebridge"), "install-skill"], cwd=tmp_path, env=env)
    metadata = json.loads(result.stdout)
    assert metadata["installed"] is True
    assert Path(metadata["path"]).read_bytes() == canonical.read_bytes()
    assert not (Path(env["HERMES_HOME"]) / "config.yaml").exists()
    assert not (Path(env["HERMES_HOME"]) / ".env").exists()

    env["PHONEBRIDGE_URL"] = wire.url.replace("127.0.0.1", "localhost")
    env["PHONEBRIDGE_TOKEN"] = wire.client.settings.token
    wire.respond({"devices": []})
    response = run([str(installed / "bin" / "phonebridge"), "devices", "list"], cwd=tmp_path, env=env)
    assert json.loads(response.stdout) == {"devices": []}
    assert response.stderr == ""
    assert [request["method"] for request in wire.requests] == ["GET"]
