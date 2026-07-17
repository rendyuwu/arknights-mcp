"""T6: the six M0 ADRs exist, follow a consistent shape, and each cites a
founder decision (D#) and at least one invariant.

Cites §V1, §V3, §V4, §V9, §V14 (the decisions these ADRs record).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = REPO_ROOT / "docs" / "adr"

EXPECTED_ADRS = [
    "0001-dual-transport-one-core.md",
    "0002-immutable-promotion.md",
    "0003-no-query-time-source-network.md",
    "0004-code-only-distribution.md",
    "0005-source-registry-and-takedown.md",
    "0006-oauth-oidc-remote-auth.md",
]

# Invariants T6 cites — each must be referenced by at least one ADR.
REQUIRED_INVARIANT_CITES = ["V1", "V3", "V4", "V9", "V14"]


@pytest.mark.parametrize("name", EXPECTED_ADRS)
def test_adr_present(name: str) -> None:
    assert (ADR_DIR / name).is_file(), f"missing ADR: {name}"


@pytest.mark.parametrize("name", EXPECTED_ADRS)
def test_adr_shape_and_citations(name: str) -> None:
    text = (ADR_DIR / name).read_text(encoding="utf-8")
    assert "Status:" in text and "Accepted" in text
    assert "## Decision" in text
    assert "## Consequences" in text
    # Cites a founder decision like "D3" / "D15".
    assert re.search(r"\bD1[0-5]\b|\bD[1-9]\b", text), f"{name} cites no founder decision"
    # Cites at least one invariant like "§V4".
    assert re.search(r"§V\d+", text), f"{name} cites no invariant"


def test_index_present() -> None:
    assert (ADR_DIR / "README.md").is_file()


def test_cited_invariants_covered() -> None:
    corpus = "\n".join((ADR_DIR / name).read_text(encoding="utf-8") for name in EXPECTED_ADRS)
    for inv in REQUIRED_INVARIANT_CITES:
        assert f"§{inv}" in corpus, f"no ADR references §{inv}"
