import os
import shutil
import subprocess

import pytest


@pytest.fixture
def proxy(modules, tmp_path, monkeypatch):
    provisioner = modules["provision_https"]
    site = tmp_path / "available/phonebridge"
    enabled = tmp_path / "enabled/phonebridge"
    site.parent.mkdir()
    enabled.parent.mkdir()
    monkeypatch.setattr(provisioner, "SITE", site)
    monkeypatch.setattr(provisioner, "ENABLED", enabled)
    monkeypatch.setattr(provisioner, "SCRATCH", tmp_path / "scratch")
    calls = []
    failures = []

    def run(*args, **kwargs):
        calls.append(args)
        command = args[2:]
        if command[:2] == ("nginx", "-t") and failures:
            failures.pop()
            raise subprocess.CalledProcessError(1, args)
        if command[0] == "install":
            shutil.copyfile(command[-2], command[-1])
            os.chmod(command[-1], int(command[2], 8))
        elif command[0] == "mv":
            os.replace(command[-2], command[-1])
        elif command[0] == "ln":
            enabled.symlink_to(command[-2])
        elif command[0] == "rm":
            from pathlib import Path
            Path(command[-1]).unlink(missing_ok=True)

    monkeypatch.setattr(provisioner, "run", run)
    return provisioner, site, enabled, calls, failures


def test_invalid_config_restores_prior_file_and_enabled_link(proxy):
    provisioner, site, enabled, calls, failures = proxy
    site.write_text("prior valid config")
    site.chmod(0o640)
    enabled.symlink_to(site)
    failures.append(True)
    with pytest.raises(subprocess.CalledProcessError):
        provisioner.install_config("invalid config")
    assert site.read_text() == "prior valid config"
    assert site.stat().st_mode & 0o777 == 0o640
    assert os.readlink(enabled) == str(site)
    assert calls[-1][2:] == ("systemctl", "reload", "nginx")


def test_first_config_failure_removes_created_file_and_link(proxy):
    provisioner, site, enabled, calls, failures = proxy
    failures.append(True)
    with pytest.raises(subprocess.CalledProcessError):
        provisioner.install_config("invalid")
    assert not site.exists()
    assert not enabled.is_symlink()


def test_outer_acme_failure_restores_original_configuration(proxy):
    provisioner, site, enabled, calls, failures = proxy
    site.write_text("before ACME")

    def operation():
        provisioner.promote("temporary HTTP ACME")
        raise RuntimeError("ACME failed")

    with pytest.raises(RuntimeError, match="ACME failed"):
        provisioner.transaction(operation)
    assert site.read_text() == "before ACME"
    assert not enabled.is_symlink()


def test_wrong_enabled_origin_rejected_before_mutation(proxy):
    provisioner, site, enabled, calls, failures = proxy
    enabled.symlink_to(site.parent / "other")
    with pytest.raises(RuntimeError, match="another origin"):
        provisioner.install_config("new config")
    assert not calls


def test_provisioning_and_restoration_errors_both_reported(proxy):
    provisioner, site, enabled, calls, failures = proxy
    failures.extend([True, True])
    with pytest.raises(RuntimeError, match="provisioning failed.*restore failed"):
        provisioner.install_config("invalid")
