"""The six M0 ADRs exist, follow a consistent shape, and each cites a
founder decision (D#).
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


def test_index_present() -> None:
    assert (ADR_DIR / "README.md").is_file()


# ---------------------------------------------------------------------------
# ADR 0011 — response-shape v0.2 coordination ADR.
#
# A breaking `schema_version` bump needs an ADR; it reverses no founder
# decision, so — unlike the parametrized EXPECTED_ADRS above, which each
# require a D# cite — it is checked on its own for shape.
# ---------------------------------------------------------------------------

ADR_0011 = "0011-response-shape-v0.2.md"


def test_adr_0011_present() -> None:
    assert (ADR_DIR / ADR_0011).is_file(), f"missing ADR: {ADR_0011}"


def test_adr_0011_shape() -> None:
    text = (ADR_DIR / ADR_0011).read_text(encoding="utf-8")
    assert "Status:" in text and "Accepted" in text
    assert "## Context" in text
    assert "## Decision" in text
    assert "## Consequences" in text


def test_adr_0011_records_single_schema_version_bump() -> None:
    # Coordinate the breaking M13 wire changes under ONE schema_version bump
    # (0.1 -> 0.2), not one bump per change.
    text = (ADR_DIR / ADR_0011).read_text(encoding="utf-8")
    assert "schema_version" in text.lower()
    assert "0.1" in text and "0.2" in text


def test_adr_0011_indexed_in_readme() -> None:
    readme = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    assert ADR_0011 in readme, "ADR 0011 not linked from the ADR index"


# ---------------------------------------------------------------------------
# ADR 0012 — response-shape v0.2 (continued): fold the M14 breaking reshapes
# into the same v0.2 revision and flip SCHEMA_VERSION "0.1" -> "0.2". Like
# 0011 it is a wire-contract ADR (reverses no founder decision), so it is
# checked on its own for shape.
# ---------------------------------------------------------------------------

ADR_0012 = "0012-response-shape-v0.2-m14-fold.md"


def test_adr_0012_present() -> None:
    assert (ADR_DIR / ADR_0012).is_file(), f"missing ADR: {ADR_0012}"


def test_adr_0012_shape() -> None:
    text = (ADR_DIR / ADR_0012).read_text(encoding="utf-8")
    assert "Status:" in text and "Accepted" in text
    assert "## Context" in text
    assert "## Decision" in text
    assert "## Consequences" in text


def test_adr_0012_folds_into_the_same_v02_bump() -> None:
    # Fold the M14 reshapes into the SAME v0.2 revision and reuse the single
    # 0.1 -> 0.2 bump (never mint 0.3), then flip.
    text = (ADR_DIR / ADR_0012).read_text(encoding="utf-8")
    assert "schema_version" in text.lower()
    assert "0.1" in text and "0.2" in text
    # It must name ADR 0011 as the revision it continues, not supersedes.
    assert "0011" in text


def test_adr_0012_indexed_in_readme() -> None:
    readme = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    assert ADR_0012 in readme, "ADR 0012 not linked from the ADR index"


# ---------------------------------------------------------------------------
# ADR 0017 — response-shape v0.3. The five breaking fixes land together under
# ONE coordinated schema_version flip. Like 0011/0012 it is a wire-contract
# ADR (it reverses no founder decision), so it is checked on its own for shape.
#
# Unlike 0012/0013/0014 it OPENS a version rather than folding into v0.2: those
# folds rested on ADR 0012's "no external release" gate, and that gate is now
# discharged. The ADR has to say so, or the next reader reads the fold
# precedent as a standing rule and mutates a released shape.
# ---------------------------------------------------------------------------

ADR_0017 = "0017-response-shape-v0.3.md"


def test_adr_0017_present() -> None:
    assert (ADR_DIR / ADR_0017).is_file(), f"missing ADR: {ADR_0017}"


def test_adr_0017_shape() -> None:
    text = (ADR_DIR / ADR_0017).read_text(encoding="utf-8")
    assert "Status:" in text and "Accepted" in text
    assert "## Context" in text
    assert "## Decision" in text
    assert "## Consequences" in text


def test_adr_0017_records_the_single_coordinated_bump() -> None:
    # One tracked flip for the whole set, recorded here rather than as a
    # footnote in the last bundle member.
    text = (ADR_DIR / ADR_0017).read_text(encoding="utf-8")
    assert "schema_version" in text.lower()
    assert "0.2" in text and "0.3" in text


def test_adr_0017_justifies_opening_a_version_instead_of_folding() -> None:
    # ADRs 0011-0014 folded into the unreleased v0.2 line on ADR 0012's release
    # gate. Minting 0.3 reverses that precedent, so the reason -- the release
    # gate is discharged -- must be ON the record, not inferred by whoever reads
    # it next.
    text = (ADR_DIR / ADR_0017).read_text(encoding="utf-8")
    assert "0014" in text, "ADR 0017 does not name the fold precedent it departs from"


def test_adr_0017_indexed_in_readme() -> None:
    readme = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    assert ADR_0017 in readme, "ADR 0017 not linked from the ADR index"


# ---------------------------------------------------------------------------
# ADR 0019 — response-shape v0.4. ONE member: `image_refs[].source_id` is
# REMOVED and hoisted to a response-level `image_refs_source_id`. Like
# 0011/0012/0017 it is a wire-contract ADR (reverses no founder decision), so
# it is checked on its own for shape.
#
# It OPENS 0.4 rather than widening 0.3 for the reason ADR 0017 opened 0.3: the
# v0.2 fold precedent rested on ADR 0012's release gate, which is now
# discharged. The same reasoning one version out, so the record has to carry it
# -- otherwise the next reader reads 0017 as a one-off and folds a breaking
# change into a shipped tag.
# ---------------------------------------------------------------------------

ADR_0019 = "0019-response-shape-v0.4.md"


def test_adr_0019_present() -> None:
    assert (ADR_DIR / ADR_0019).is_file(), f"missing ADR: {ADR_0019}"


def test_adr_0019_shape() -> None:
    text = (ADR_DIR / ADR_0019).read_text(encoding="utf-8")
    assert "Status:" in text and "Accepted" in text
    assert "## Context" in text
    assert "## Decision" in text
    assert "## Consequences" in text


def test_adr_0019_records_the_single_coordinated_bump() -> None:
    text = (ADR_DIR / ADR_0019).read_text(encoding="utf-8")
    assert "schema_version" in text.lower()
    assert "0.3" in text and "0.4" in text


def test_adr_0019_records_that_the_removal_is_breaking() -> None:
    # The original filing called this change "additive optional field" and it is not: a
    # client reading `ref["source_id"]` gets a KeyError. The ADR is where that correction
    # lives, because the row's own cell was the thing that was wrong.
    text = (ADR_DIR / ADR_0019).read_text(encoding="utf-8")
    assert "breaking" in text.lower()
    assert "image_refs_source_id" in text


def test_adr_0019_justifies_opening_a_version_instead_of_folding() -> None:
    # Same duty ADR 0017 carried: minting a version rather than widening the last one is a
    # departure from the 0011-0014 fold precedent, so the reason stays on the record.
    text = (ADR_DIR / ADR_0019).read_text(encoding="utf-8")
    assert "0017" in text, "ADR 0019 does not name the version line it continues"


def test_adr_0019_indexed_in_readme() -> None:
    readme = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    assert ADR_0019 in readme, "ADR 0019 not linked from the ADR index"


def test_schema_version_matches_flipped_v04() -> None:
    # The flip is the operative half: the constant and the ADR agree.
    from arknights_mcp.mcp.envelopes import SCHEMA_VERSION

    assert SCHEMA_VERSION == "0.4"
