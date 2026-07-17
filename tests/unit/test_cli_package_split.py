"""§V38: ``cli.py`` split into a package, one module per command group (T75).

Guards the structural move and the module-size cap so future accretion cannot
silently regrow a single mixed-concern file:

- ``cli`` is a package with one module per command group + shared ``_shared``.
- The public entry points survive the move (``main``/``CliContext``/
  ``is_placeholder`` importable from ``arknights_mcp.cli``); the console-script
  target ``arknights_mcp.cli:main`` and every ``from arknights_mcp.cli import
  main`` test keep resolving.
- No source module in the package exceeds the §V38 800-line hard cap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import arknights_mcp
import arknights_mcp.cli as cli

PKG_ROOT = Path(arknights_mcp.__file__).resolve().parent
CLI_PKG = PKG_ROOT / "cli"

# One module per command group (§T21/§T22/§T23/§T25/§T26) + shared helpers.
EXPECTED_CLI_MODULES = [
    "__init__.py",
    "_shared.py",
    "sync.py",
    "import_.py",
    "validate.py",
    "status.py",
    "source.py",
]

HARD_CAP_LINES = 800


def test_cli_is_a_package() -> None:
    assert CLI_PKG.is_dir(), "cli must be a package after the §V38 split (T75)"
    assert not (PKG_ROOT / "cli.py").exists(), "old single-file cli.py must be gone"


@pytest.mark.parametrize("module", EXPECTED_CLI_MODULES)
def test_command_group_module_exists(module: str) -> None:
    assert (CLI_PKG / module).is_file(), f"missing cli command-group module: {module}"


def test_public_entry_points_survive_the_move() -> None:
    """The console-script + ``from arknights_mcp.cli import main`` surface holds."""
    assert callable(cli.main)
    assert cli.CliContext is not None
    # Re-exported single home for the §V37 placeholder guard (test_text.py).
    assert cli.is_placeholder.__module__ == "arknights_mcp.util.text"


@pytest.mark.parametrize(
    "path",
    sorted(PKG_ROOT.rglob("*.py")),
    ids=lambda p: str(p.relative_to(PKG_ROOT)),
)
def test_no_source_module_exceeds_hard_cap(path: Path) -> None:
    """§V38: no ``.py`` source module exceeds the 800-line hard cap."""
    line_count = len(path.read_text(encoding="utf-8").splitlines())
    rel = path.relative_to(PKG_ROOT)
    assert line_count <= HARD_CAP_LINES, f"{rel} has {line_count} lines (> {HARD_CAP_LINES}, §V38)"
