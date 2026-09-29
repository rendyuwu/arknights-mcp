"""M7 threat-model review — the review deliverable exists and stays honest.

`THREAT_MODEL.md` is the design-of-record security review. Like the
policy-file and release-audit deliverables, it ships as a root
document guarded by a completeness test so a future edit cannot quietly drop a
trust boundary or a required section.

This test does not re-verify the controls themselves — the adversarial suites
and the per-invariant unit tests do that. It asserts the document
maps the surface it claims to: the trust boundaries, both transports, the
admin/read-only split, and the required sections.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
THREAT_MODEL = REPO_ROOT / "THREAT_MODEL.md"

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
    assert THREAT_MODEL.is_file(), "missing THREAT_MODEL.md"
    assert _text().strip(), "THREAT_MODEL.md is empty"


def test_threat_model_has_required_sections() -> None:
    norm = _norm(_text())
    missing = [m for m in REQUIRED_SECTION_MARKERS if m.lower() not in norm]
    assert not missing, f"THREAT_MODEL.md missing sections: {missing}"


def test_threat_model_covers_both_transports() -> None:
    # One core, two transports — both boundaries must appear in the model.
    norm = _norm(_text())
    assert "stdio" in norm, "THREAT_MODEL.md omits the local stdio transport"
    assert "streamable http" in norm or "streamable-http" in norm, (
        "THREAT_MODEL.md omits the remote Streamable HTTP transport"
    )


def test_threat_model_names_admin_readonly_boundary() -> None:
    # The admin-CLI vs read-only-MCP split is the elevation boundary.
    norm = _norm(_text())
    assert "cli-only" in norm or "cli only" in norm, (
        "THREAT_MODEL.md omits the admin-CLI-only boundary"
    )
    assert "read-only" in norm, "THREAT_MODEL.md omits the read-only data plane"


def test_threat_model_records_review_date() -> None:
    # A stale threat model is a lie; the review cadence + date must be present.
    norm = _norm(_text())
    assert "last reviewed" in norm, "THREAT_MODEL.md omits a last-reviewed date"
    assert "review cadence" in norm or "cadence" in norm, "THREAT_MODEL.md omits a review cadence"
