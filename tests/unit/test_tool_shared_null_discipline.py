"""§T130 unit tests for the shared null-discipline / provenance-hoist helpers
(§V66.2/§V67/§V26/B58; §V37 single home).

These pin the pure functions in :mod:`arknights_mcp.mcp.tools._shared` that both
drop tools (provenance hoist) and both entity tools (absent-field limitation) route
through, so the behaviour is fixed once regardless of the caller:

* :func:`hoist_drop_provenance` -- §V66.2: identical per-row provenance collapses to
  one shared block; a row emits only the fields where it deviates, deterministically
  (most common row, first-seen tie break); an empty input yields an empty block.
* :func:`absent_field_limitation` -- §V67/§V26 (B58): names the expected fields the
  source omitted, or emits nothing when none are absent; the client-facing text
  carries no internal spec cites/jargon (§V71).
* :data:`LIST_FIELD_CONVENTION` -- the []-vs-absent convention sentence, cite-free.
"""

from __future__ import annotations

from arknights_mcp.mcp.tools._shared import (
    LIST_FIELD_CONVENTION,
    absent_field_limitation,
    hoist_drop_provenance,
)

# --- §V66.2 hoist -------------------------------------------------------------


def test_hoist_collapses_identical_rows_to_one_block() -> None:
    rows = [
        {"snapshot_id": "pg:en", "fetched_at": "t0", "expires_at": "t1"},
        {"snapshot_id": "pg:en", "fetched_at": "t0", "expires_at": "t1"},
        {"snapshot_id": "pg:en", "fetched_at": "t0", "expires_at": "t1"},
    ]
    shared, deviations = hoist_drop_provenance(rows)
    assert shared == {"snapshot_id": "pg:en", "fetched_at": "t0", "expires_at": "t1"}
    # §V66.2: every row matches the shared block -> no per-row provenance repetition.
    assert deviations == [{}, {}, {}]


def test_hoist_surfaces_only_the_deviant_row() -> None:
    rows = [
        {"snapshot_id": "pg:en", "fetched_at": "t0", "expires_at": "t1"},
        {"snapshot_id": "pg:en", "fetched_at": "t0", "expires_at": "t1"},
        {"snapshot_id": "pg:cn", "fetched_at": "t0", "expires_at": "t9"},  # deviates
    ]
    shared, deviations = hoist_drop_provenance(rows)
    # The most common row is hoisted; the minority row carries ONLY its differing fields.
    assert shared == {"snapshot_id": "pg:en", "fetched_at": "t0", "expires_at": "t1"}
    assert deviations == [{}, {}, {"snapshot_id": "pg:cn", "expires_at": "t9"}]


def test_hoist_tie_breaks_by_first_seen() -> None:
    # Two distinct rows, one each -> the tie breaks to the first-seen row (deterministic
    # + reproducible, §V26), and the later row is the deviation.
    rows = [
        {"snapshot_id": "a", "expires_at": "future"},
        {"snapshot_id": "b", "expires_at": "past"},
    ]
    shared, deviations = hoist_drop_provenance(rows)
    assert shared == {"snapshot_id": "a", "expires_at": "future"}
    assert deviations == [{}, {"snapshot_id": "b", "expires_at": "past"}]


def test_hoist_empty_input() -> None:
    shared, deviations = hoist_drop_provenance([])
    assert shared == {}
    assert deviations == []


# --- §V67/§V26 absent-field limitation ----------------------------------------


def test_absent_field_limitation_names_absent_fields() -> None:
    lim = absent_field_limitation(["immunities", "targeting"])
    assert len(lim) == 1
    text = lim[0]
    assert "immunities" in text and "targeting" in text
    assert "not present" in text.lower()


def test_absent_field_limitation_empty_when_nothing_absent() -> None:
    # §V67: nothing expected is absent -> no limitation emitted at all.
    assert absent_field_limitation([]) == ()


def test_client_facing_null_discipline_text_has_no_internal_cites() -> None:
    # §V71 (b): published client-facing text carries no internal spec cites/jargon.
    for text in (LIST_FIELD_CONVENTION, absent_field_limitation(["immunities"])[0]):
        assert "§V" not in text and "§T" not in text and "B58" not in text
        assert "degenerate" not in text
