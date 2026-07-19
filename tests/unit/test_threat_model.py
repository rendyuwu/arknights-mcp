"""T58: M7 threat-model review — the review deliverable exists and stays honest.

`THREAT_MODEL.md` is the design-of-record security review (SPEC §T58). Like the
policy-file (§T4) and release-audit (§T49) deliverables, it ships as a root
document guarded by a completeness test so a future edit cannot quietly drop a
trust boundary or stop citing a security invariant.

This test does not re-verify the controls themselves — the adversarial suites
(T57, T61–T64) and the per-invariant unit tests do that. It asserts the document
maps the surface it claims to: the trust boundaries, both transports, the
admin/read-only split, and every security-substrate invariant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
THREAT_MODEL = REPO_ROOT / "THREAT_MODEL.md"

# Security-substrate invariants the model must map a threat to. Chosen as the
# controls that sit *on* a trust boundary (auth, injection, disclosure,
# admin-surface, supply-chain) — the ones a threat model exists to enumerate.
# A control silently dropped from the doc trips this list.
REQUIRED_INVARIANT_CITES = [
    "V1",  # no query-time source network
    "V2",  # read-only, parameterized SQL
    "V3",  # fail-closed sync
    "V4",  # validated atomic promotion
    "V5",  # region + provenance, no silent mix
    "V9",  # remote HTTPS + OAuth startup gate
    "V10",  # bearer validation
    "V11",  # rate / concurrency / timeout / caps
    "V12",  # redacted logging
    "V15",  # no game credentials
    "V16",  # release artifact excludes raw data / content
    "V17",  # per-record provenance
    "V18",  # untrusted imported data / prompt injection
    "V19",  # no bulk reconstruction
    "V22",  # response size cap
    "V27",  # source registry completeness
    "V28",  # admin ops CLI-only
    "V32",  # purge cascade + FTS rebuild
    "V36",  # level-id path-traversal confinement
    "V40",  # auth enforcement independent of bind address
]

# Section headings the model must carry so its shape stays legible.
REQUIRED_SECTION_MARKERS = [
    "Assets",
    "Trust boundaries",
    "Threats and mitigations",
    "Residual risks",
    "out of scope",
]


def _text() -> str:
    return THREAT_MODEL.read_text(encoding="utf-8")


def _norm(text: str) -> str:
    """Lower-case, whitespace-collapsed so hard-wrapped prose still matches."""
    return " ".join(text.split()).lower()


def test_threat_model_present_and_nonempty() -> None:
    assert THREAT_MODEL.is_file(), "missing THREAT_MODEL.md (SPEC §T58)"
    assert _text().strip(), "THREAT_MODEL.md is empty"


def test_threat_model_has_required_sections() -> None:
    norm = _norm(_text())
    missing = [m for m in REQUIRED_SECTION_MARKERS if m.lower() not in norm]
    assert not missing, f"THREAT_MODEL.md missing sections: {missing}"


@pytest.mark.parametrize("inv", REQUIRED_INVARIANT_CITES)
def test_threat_model_cites_security_invariant(inv: str) -> None:
    # Cited as "§V10" — the SPEC addressing form.
    assert f"§{inv}" in _text(), f"THREAT_MODEL.md maps no threat to §{inv}"


def test_threat_model_covers_both_transports() -> None:
    # V14: one core, two transports — both boundaries must appear in the model.
    norm = _norm(_text())
    assert "stdio" in norm, "THREAT_MODEL.md omits the local stdio transport"
    assert "streamable http" in norm or "streamable-http" in norm, (
        "THREAT_MODEL.md omits the remote Streamable HTTP transport"
    )


def test_threat_model_names_admin_readonly_boundary() -> None:
    # V28: the admin-CLI vs read-only-MCP split is the elevation boundary.
    norm = _norm(_text())
    assert "cli-only" in norm or "cli only" in norm, (
        "THREAT_MODEL.md omits the admin-CLI-only boundary (§V28)"
    )
    assert "read-only" in norm, "THREAT_MODEL.md omits the read-only data plane"


def test_threat_model_records_review_date() -> None:
    # A stale threat model is a lie; the review cadence + date must be present.
    norm = _norm(_text())
    assert "last reviewed" in norm, "THREAT_MODEL.md omits a last-reviewed date"
    assert "review cadence" in norm or "cadence" in norm, "THREAT_MODEL.md omits a review cadence"
