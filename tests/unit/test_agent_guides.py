"""T5: agent guardrail files exist and carry the key non-negotiables.

`AGENTS.md` is the canonical tool-agnostic guardrail set; `CLAUDE.md` is the
Claude-Code-specific pointer. Both must instruct agents to read the PRD, keep
one shared core across two transports, preserve legal exclusions, and never add
raw snapshots or generated databases to Git (PRD Section 20).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _norm(name: str) -> str:
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    return " ".join(text.split()).lower()


def test_both_guide_files_exist() -> None:
    for name in ("AGENTS.md", "CLAUDE.md"):
        assert (REPO_ROOT / name).is_file(), f"missing {name}"


def test_claude_points_to_agents() -> None:
    assert "agents.md" in _norm("CLAUDE.md")


def test_agents_covers_core_guardrails() -> None:
    text = _norm("AGENTS.md")
    # Read the PRD and honor binding decisions.
    assert "prd" in text
    assert "adr" in text
    # One shared core, two transports; no duplicated domain logic.
    assert "two transports" in text
    assert "never duplicate domain logic" in text
    # No raw snapshots / generated DB in git.
    assert "raw snapshot" in text
    assert "prebuilt database" in text
    # Read-only, no query-time network.
    assert "read-only" in text
    assert "query time" in text or "query-time" in text
    # Provenance + region on facts.
    assert "provenance" in text
    assert "region" in text


def test_both_files_reference_invariants() -> None:
    for name in ("AGENTS.md", "CLAUDE.md"):
        text = _norm(name)
        assert "cli-only" in text  # admin ops are CLI-only (§V28)
