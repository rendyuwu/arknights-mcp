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
    "0007-banner-archive-carve.md",
]

# Invariants the ADR corpus must cite — each referenced by at least one ADR.
# T110 adds §V62 (banner archive carve, ADR 0007).
REQUIRED_INVARIANT_CITES = ["V1", "V3", "V4", "V9", "V14", "V62"]


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


# ---------------------------------------------------------------------------
# T128: ADR 0011 — response-shape v0.2 coordination ADR.
#
# 0011 is a §V21-mandated wire-contract ADR (a breaking `schema_version` bump
# needs an ADR); it reverses no founder decision, so — unlike the parametrized
# EXPECTED_ADRS above, which each require a D# cite — it is checked on its own
# for shape + the invariants it coordinates (§V21/§V66/§V67/§V71).
# ---------------------------------------------------------------------------

ADR_0011 = "0011-response-shape-v0.2.md"

#: The invariants T128 coordinates under one schema_version bump.
ADR_0011_INVARIANT_CITES = ["V21", "V66", "V67", "V71"]


def test_adr_0011_present() -> None:
    assert (ADR_DIR / ADR_0011).is_file(), f"missing ADR: {ADR_0011}"


def test_adr_0011_shape() -> None:
    text = (ADR_DIR / ADR_0011).read_text(encoding="utf-8")
    assert "Status:" in text and "Accepted" in text
    assert "## Context" in text
    assert "## Decision" in text
    assert "## Consequences" in text


def test_adr_0011_cites_coordinated_invariants() -> None:
    text = (ADR_DIR / ADR_0011).read_text(encoding="utf-8")
    for inv in ADR_0011_INVARIANT_CITES:
        assert f"§{inv}" in text, f"ADR 0011 does not cite §{inv}"


def test_adr_0011_records_single_schema_version_bump() -> None:
    # The whole point of T128: coordinate the breaking M13 wire changes under
    # ONE schema_version bump (0.1 -> 0.2), not one bump per change.
    text = (ADR_DIR / ADR_0011).read_text(encoding="utf-8")
    assert "schema_version" in text.lower()
    assert "0.1" in text and "0.2" in text


def test_adr_0011_indexed_in_readme() -> None:
    readme = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    assert ADR_0011 in readme, "ADR 0011 not linked from the ADR index"


# ---------------------------------------------------------------------------
# T150: ADR 0012 — response-shape v0.2 (continued): fold the M14 breaking
# reshapes (T144/T145/T146) into the same v0.2 revision and flip
# SCHEMA_VERSION "0.1" -> "0.2". Like 0011 it is a §V21-mandated wire-contract
# ADR (reverses no founder decision), so it is checked on its own for shape +
# the invariants it coordinates (§V21/§V66/§V74/§V49).
# ---------------------------------------------------------------------------

ADR_0012 = "0012-response-shape-v0.2-m14-fold.md"

#: The invariants T150 coordinates under the (reused) single schema_version bump.
ADR_0012_INVARIANT_CITES = ["V21", "V66", "V74", "V49"]


def test_adr_0012_present() -> None:
    assert (ADR_DIR / ADR_0012).is_file(), f"missing ADR: {ADR_0012}"


def test_adr_0012_shape() -> None:
    text = (ADR_DIR / ADR_0012).read_text(encoding="utf-8")
    assert "Status:" in text and "Accepted" in text
    assert "## Context" in text
    assert "## Decision" in text
    assert "## Consequences" in text


def test_adr_0012_cites_coordinated_invariants() -> None:
    text = (ADR_DIR / ADR_0012).read_text(encoding="utf-8")
    for inv in ADR_0012_INVARIANT_CITES:
        assert f"§{inv}" in text, f"ADR 0012 does not cite §{inv}"


def test_adr_0012_folds_into_the_same_v02_bump() -> None:
    # The point of T150: fold the M14 reshapes into the SAME v0.2 revision and
    # reuse the single 0.1 -> 0.2 bump (never mint 0.3), then flip.
    text = (ADR_DIR / ADR_0012).read_text(encoding="utf-8")
    assert "schema_version" in text.lower()
    assert "0.1" in text and "0.2" in text
    # It must name ADR 0011 as the revision it continues, not supersedes.
    assert "0011" in text


def test_adr_0012_indexed_in_readme() -> None:
    readme = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    assert ADR_0012 in readme, "ADR 0012 not linked from the ADR index"


def test_schema_version_matches_flipped_v02() -> None:
    # The flip is the operative half of T150: the constant and the ADR agree.
    from arknights_mcp.mcp.envelopes import SCHEMA_VERSION

    assert SCHEMA_VERSION == "0.2"
