"""T2 scaffold smoke test: the ``arknights_mcp`` package imports and its layout
matches PRD Section 20. This is a structural guard, not a behavioural one --
the leaf modules are stubs until their owning §T tasks fill them in.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import arknights_mcp

PKG_ROOT = Path(arknights_mcp.__file__).resolve().parent

EXPECTED_SUBPACKAGES = [
    "models",
    "db",
    "db.repositories",
    "sources",
    "importers",
    "analyzers",
    "analyzers.rules",
    "services",
    "mcp",
    "transports",
    "auth",
    "middleware",
    "util",
]

# A representative slice of the PRD Section 20 module tree (relative to the package root).
# ``cli`` is a package (one module per command group) after the §V38 split (T75).
EXPECTED_MODULE_FILES = [
    "cli/__init__.py",
    "config.py",
    "instructions.py",
    "app.py",
    "models/common.py",
    "db/connection.py",
    "db/migrations.py",
    "sources/base.py",
    "sources/registry.py",
    "sources/local_snapshot.py",
    "importers/manifest.py",
    "importers/field_policy.py",
    "analyzers/stage.py",
    "analyzers/module.py",
    "services/search.py",
    "services/source_status.py",
    "mcp/tool_registry.py",
    "mcp/envelopes.py",
    "transports/stdio.py",
    "transports/streamable_http.py",
    "auth/oidc.py",
    "middleware/rate_limit.py",
    "util/hashing.py",
    "util/text.py",
    "util/atomic.py",
]


def test_package_version() -> None:
    assert arknights_mcp.__version__ == "0.1.0"


def test_cli_entry_point_target_is_callable() -> None:
    from arknights_mcp import cli

    assert callable(cli.main), "arknights-mcp console-script target cli:main must be callable"


@pytest.mark.parametrize("subpkg", EXPECTED_SUBPACKAGES)
def test_subpackages_importable(subpkg: str) -> None:
    module = importlib.import_module(f"arknights_mcp.{subpkg}")
    assert module is not None


@pytest.mark.parametrize("rel", EXPECTED_MODULE_FILES)
def test_expected_module_files_exist(rel: str) -> None:
    assert (PKG_ROOT / rel).is_file(), f"missing PRD Section 20 module: {rel}"
