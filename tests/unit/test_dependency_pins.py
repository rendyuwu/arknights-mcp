"""V25 regression: ``mcp>=1.28.1,<2`` must be declared in pyproject and the
exact resolved version recorded in ``uv.lock`` must fall inside that range.
Migrating to MCP SDK v2 requires an ADR, so an accidental ``>=2`` resolution
must fail the suite.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"

MCP_RANGE = SpecifierSet(">=1.28.1,<2")


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _runtime_requirements() -> dict[str, Requirement]:
    deps = _pyproject()["project"]["dependencies"]
    reqs = [Requirement(d) for d in deps]
    return {r.name.lower(): r for r in reqs}


def _locked_versions() -> dict[str, str]:
    lock = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    return {pkg["name"].lower(): pkg["version"] for pkg in lock.get("package", [])}


def test_uv_lock_exists() -> None:
    assert UV_LOCK.is_file(), "uv.lock must be committed (V25: exact resolved versions)"


def test_pyproject_declares_mcp_v1_bound() -> None:
    reqs = _runtime_requirements()
    assert "mcp" in reqs, "mcp must be a declared runtime dependency"
    # The declared specifier must itself forbid v2 and require >=1.28.1.
    spec = reqs["mcp"].specifier
    assert Version("1.28.1") in spec, "mcp specifier must allow 1.28.1"
    assert Version("2.0.0") not in spec, "mcp specifier must forbid v2 (V25)"


def test_required_runtime_deps_present() -> None:
    reqs = _runtime_requirements()
    for name in ("mcp", "pydantic", "sqlalchemy"):
        assert name in reqs, f"missing runtime dependency: {name}"
    # Pydantic v2 line (V: Pydantic v2).
    assert Version("2.0.0") in reqs["pydantic"].specifier
    assert Version("3.0.0") not in reqs["pydantic"].specifier


def test_dev_deps_present() -> None:
    groups = _pyproject().get("dependency-groups", {})
    dev = {Requirement(d).name.lower() for d in groups.get("dev", [])}
    for name in ("ruff", "mypy", "pytest", "pytest-cov", "hypothesis"):
        assert name in dev, f"missing dev dependency: {name}"


def test_locked_mcp_is_v1() -> None:
    locked = _locked_versions()
    assert "mcp" in locked, "mcp must be resolved in uv.lock"
    resolved = Version(locked["mcp"])
    assert resolved in MCP_RANGE, f"resolved mcp {resolved} violates {MCP_RANGE} (V25)"


def test_locked_pydantic_is_v2() -> None:
    locked = _locked_versions()
    assert "pydantic" in locked
    assert Version(locked["pydantic"]) in SpecifierSet(">=2,<3")
