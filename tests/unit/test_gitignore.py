"""V16 regression: release artifacts (raw snapshots / built DBs) must never be
committed. The .gitignore is the first line of defence; assert its exclusions
hold both textually and via `git check-ignore`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GITIGNORE = REPO_ROOT / ".gitignore"

# Paths that V16 forbids from ever being tracked.
FORBIDDEN_PATHS = [
    "data/builds/20260717-en-cn.sqlite",
    "data/builds/anything.sqlite",
    "something.sqlite",
    "nested/dir/build.sqlite3",
    "snapshot/en/character_table.json",
    "snapshots/cn/stage_table.json",
    "data/current.json",
    ".venv/bin/python",
    "src/arknights_mcp/__pycache__/cli.cpython-312.pyc",
]


def test_gitignore_exists() -> None:
    assert GITIGNORE.is_file(), ".gitignore must exist at repo root"


def test_gitignore_declares_required_patterns() -> None:
    text = GITIGNORE.read_text(encoding="utf-8")
    for required in ("*.sqlite", "data/builds/", "snapshot", ".venv/", "__pycache__/"):
        assert required in text, f".gitignore missing required exclusion: {required!r}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_git_check_ignore_excludes_forbidden_paths() -> None:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git repository")
    for rel in FORBIDDEN_PATHS:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", rel],
            cwd=REPO_ROOT,
            check=False,
        )
        assert result.returncode == 0, f"path should be git-ignored but is not: {rel}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_git_check_ignore_keeps_data_gitkeep_tracked() -> None:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git repository")
    # data/.gitkeep must remain trackable despite the broad data/* ignore.
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", "data/.gitkeep"],
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 1, "data/.gitkeep must NOT be ignored"
