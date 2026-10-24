"""Shared test harness.

Loads the integration's ``api`` layer through a synthetic package so tests run
without Home Assistant installed (the api modules only depend on ``..const``).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CCM_ROOT = REPO_ROOT / "custom_components" / "cn_carrier_monitor"
FIXTURES = Path(__file__).parent / "fixtures"


def _load_pkg(name: str, path: Path, attrs: dict | None = None) -> types.ModuleType:
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    for k, v in (attrs or {}).items():
        setattr(pkg, k, v)
    sys.modules[name] = pkg
    return pkg


def _load_module(pkg_name: str, rel: str) -> types.ModuleType:
    file = CCM_ROOT / rel
    spec = importlib.util.spec_from_file_location(f"{pkg_name}.{rel.replace('/', '.').removesuffix('.py')}", file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def ccm():
    """Load const + api modules under synthetic package ``ccm``."""
    const_spec = importlib.util.spec_from_file_location("ccm.const", CCM_ROOT / "const.py")
    const = importlib.util.module_from_spec(const_spec)
    sys.modules["ccm.const"] = const
    const_spec.loader.exec_module(const)

    pkg = _load_pkg("ccm", CCM_ROOT, {"const": const})
    _load_pkg("ccm.api", CCM_ROOT / "api")
    modules = {}
    for name in ("crypto", "base", "telecom", "unicom", "mobile", "broadnet"):
        full = f"ccm.api.{name}"
        spec = importlib.util.spec_from_file_location(full, CCM_ROOT / "api" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
        modules[name] = mod
    pkg.modules = modules
    return pkg


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
