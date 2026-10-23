import importlib.metadata
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from phonebridge_server import __version__
from phonebridge_server.app import create_app


ROOT = Path(__file__).resolve().parents[1]


def test_installed_package_version_and_factory():
    assert __version__ == "0.1.0"
    assert importlib.metadata.version("phonebridge-server") == __version__
    assert callable(create_app)
    distribution = importlib.metadata.distribution("phonebridge-server")
    assert distribution.metadata["Requires-Python"] == ">=3.11"
    installed = {str(path) for path in distribution.files}
    assert "phonebridge_server/app.py" in installed
    assert "phonebridge_server/store.py" in installed


def test_runtime_requirements_are_fully_pinned_and_match_package_metadata():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = config["project"]["dependencies"]
    locked = (ROOT / "requirements.txt").read_text().splitlines()

    def requirements(values):
        result = {}
        for value in values:
            requirement = Requirement(value)
            assert len(list(requirement.specifier)) == 1
            assert next(iter(requirement.specifier)).operator == "=="
            result[canonicalize_name(requirement.name)] = str(requirement.specifier)
        return result

    assert requirements(declared) == requirements(locked)
    installed = [value for value in importlib.metadata.requires("phonebridge-server")
                 if Requirement(value).marker is None]
    assert requirements(installed) == requirements(locked)
    assert config["build-system"]["build-backend"] == "setuptools.build_meta"
