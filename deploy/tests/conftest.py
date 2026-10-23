import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.fixture
def modules(monkeypatch, tmp_path):
    loaded = {}
    for name in ("rollout", "bootstrap", "package_artifact", "provision_https"):
        spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded[name] = module
    monkeypatch.setattr(loaded["rollout"], "SCRATCH", tmp_path / "subprocess-scratch")
    monkeypatch.setattr(loaded["rollout"], "RUNTIME_PYTHON", sys.executable)
    return loaded
