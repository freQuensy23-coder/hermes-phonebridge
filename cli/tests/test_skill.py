import json
import stat
from importlib.resources import files
from pathlib import Path

import pytest

from phonebridge.cli import main
from phonebridge.errors import BridgeError
from phonebridge.local import install_skill


def test_bundled_resource_matches_canonical():
    canonical = Path(__file__).resolve().parents[2] / "skills" / "phonebridge" / "SKILL.md"
    assert files("phonebridge").joinpath("resources", "SKILL.md").read_bytes() == canonical.read_bytes()


def test_install_honors_home_and_is_idempotent(tmp_path, environment, monkeypatch):
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    first = install_skill()
    target = home / "skills" / "phonebridge" / "SKILL.md"
    assert first == {"installed": True, "changed": True, "path": str(target)}
    assert target.read_bytes() == files("phonebridge").joinpath("resources", "SKILL.md").read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    second = install_skill()
    assert second == {"installed": True, "changed": False, "path": str(target)}
    assert not (home / "config.yaml").exists()
    assert not (home / ".env").exists()


def test_fallback_home_is_profile_safe(tmp_path, environment, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = install_skill()
    assert result["path"] == str(tmp_path / ".hermes" / "skills" / "phonebridge" / "SKILL.md")


def test_install_cli_needs_no_server_credentials(tmp_path, environment, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    assert main(["install-skill"]) == 0
    assert json.loads(capsys.readouterr().out)["installed"] is True


def test_conflicting_skill_is_not_overwritten(tmp_path, environment, monkeypatch):
    home = tmp_path / "home"
    target = home / "skills" / "phonebridge" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("preserve this skill")
    monkeypatch.setenv("HERMES_HOME", str(home))
    with pytest.raises(BridgeError) as caught:
        install_skill()
    assert caught.value.code == "SKILL_EXISTS"
    assert target.read_text() == "preserve this skill"


@pytest.mark.parametrize("component", ["home", "skills", "phonebridge", "SKILL.md"])
def test_installer_refuses_every_symlink_component(tmp_path, environment, monkeypatch, component):
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    outside.mkdir()
    if component == "home":
        home.symlink_to(outside, target_is_directory=True)
    elif component == "skills":
        home.mkdir()
        (home / "skills").symlink_to(outside, target_is_directory=True)
    elif component == "phonebridge":
        (home / "skills").mkdir(parents=True)
        (home / "skills" / "phonebridge").symlink_to(outside, target_is_directory=True)
    else:
        directory = home / "skills" / "phonebridge"
        directory.mkdir(parents=True)
        existing = outside / "original"
        existing.write_text("preserve")
        (directory / "SKILL.md").symlink_to(existing)
    monkeypatch.setenv("HERMES_HOME", str(home))
    with pytest.raises(BridgeError):
        install_skill()
    assert not (outside / "SKILL.md").exists()
    if component == "SKILL.md":
        assert (outside / "original").read_text() == "preserve"


def test_parent_traversal_refused(tmp_path, environment, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nested" / ".." / "target"))
    with pytest.raises(BridgeError):
        install_skill()
    assert not (tmp_path / "target").exists()



def test_empty_home_falls_back_without_writing_cwd(tmp_path, environment, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", "")
    result = install_skill()
    assert result["path"] == str(tmp_path / ".hermes" / "skills" / "phonebridge" / "SKILL.md")
