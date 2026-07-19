"""T78: CI workflow runs lint + type + test on Linux/ubuntu-latest only
(SPEC §C) for Python 3.12, using uv with the locked dependencies. Docker makes
the build OS-portable and there is no macOS device to real-test, so Windows and
macOS runners are intentionally dropped (was T7's three-OS matrix).

Parsed as text to avoid adding a YAML dependency just for this guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

REQUIRED_OS = ["ubuntu-latest"]
FORBIDDEN_OS = ["macos-latest", "windows-latest"]
REQUIRED_STEPS = [
    "uv sync --locked",
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run mypy",
    "uv run pytest",
]


def test_ci_workflow_present() -> None:
    assert CI.is_file(), "missing .github/workflows/ci.yml"


@pytest.mark.parametrize("os_name", REQUIRED_OS)
def test_matrix_covers_required_os(os_name: str) -> None:
    assert os_name in CI.read_text(encoding="utf-8"), f"CI matrix missing {os_name}"


@pytest.mark.parametrize("os_name", FORBIDDEN_OS)
def test_matrix_excludes_other_os(os_name: str) -> None:
    # SPEC §C: Linux-only. Docker -> OS-portable; no macOS device to real-test.
    assert os_name not in CI.read_text(encoding="utf-8"), f"CI matrix should not run {os_name}"


def test_python_312() -> None:
    assert "3.12" in CI.read_text(encoding="utf-8")


@pytest.mark.parametrize("step", REQUIRED_STEPS)
def test_required_step_present(step: str) -> None:
    assert step in CI.read_text(encoding="utf-8"), f"CI missing step: {step}"
