"""T212 since/until bound FORM (§V116/B163; §V105/§V26/§V37).

B48/B49 typed the bound SHAPE and T201/§V105 typed the bound RELATION; neither typed the
NOTATION, and the window compares TEXT. Counted on the promoted build before this task,
all four arms of that gap were live:

1. ``since="20260101"`` (ISO basic format -- accepted by ``fromisoformat``) sorted above
   every stored ``2026-..`` value and returned ZERO rows on both windowed tools in both
   regions, where the extended form returned 14/22 announcements + 44/45 banners;
2. ``until="20260101"`` sorted below nothing, so the bound was silently IGNORED and the
   FULL archive came back -- a false inclusion, which reads as a filtered answer;
3. T201's own guard used a bare ``since > until`` while the banner SQL compares the upper
   bound against ``until || '~'``, so the legitimate intra-day pair
   ``since="<day>T00:00:00", until="<day>"`` was rejected as impossible on every one of
   the 359 en / 424 cn banner-open days, withholding rows the query would have returned;
4. one shared validator serves two stored forms -- a day-granular ``announcements.date``
   and a full ``banners.open_time`` timestamp -- so a sub-day bound compared verbatim
   against the day-granular column dropped that whole day.

These drive the render (canonical notation, then the column's granularity) at every
surface that compares a bound, plus the two behaviours that change as a consequence: a
mixed-notation pair now ANSWERS instead of being rejected (its instants were never
contradictory), and a coarsened bound is disclosed rather than silently applied.

The real-build half is ``tests/contract/test_window_bound_forms.py``: the harm is a
COLLATION against the dates really stored, which a fixture cannot witness (§V116 (d)).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.announcements import import_announcements
from arknights_mcp.importers.banners import ParsedBanner, insert_banners
from arknights_mcp.models.announcements import GetAnnouncementsInput
from arknights_mcp.models.banners import GetBannersInput
from arknights_mcp.models.common import inverted_window_reason
from arknights_mcp.services.announcements import get_announcements
from arknights_mcp.services.banners import get_banners
from arknights_mcp.sources.announcements import source_id_for_region
from arknights_mcp.util.iso_bounds import (
    UNTIL_UPPER_SENTINEL,
    canonical_iso_bound,
    canonical_window,
    coarsened_window_bounds,
)

_SOURCE_ID = "local_snapshot"

#: Notations ``datetime.fromisoformat`` accepts, paired with the canonical rendering each
#: must collapse to. Every pair denotes ONE instant; before T212 each sorted differently
#: against the stored columns, which is the whole defect.
NOTATIONS: tuple[tuple[str, str], ...] = (
    ("2026-07-01", "2026-07-01"),
    ("20260701", "2026-07-01"),
    ("2026-W27-3", "2026-07-01"),
    ("2026-07-01T10:00:00", "2026-07-01T10:00:00"),
    ("20260701T100000", "2026-07-01T10:00:00"),
    ("2026-07-01 10:00:00", "2026-07-01T10:00:00"),
    ("2026-07-01T10:00:00Z", "2026-07-01T10:00:00+00:00"),
    ("2026-07-01T17:00:00+07:00", "2026-07-01T10:00:00+00:00"),
)

#: Text no ISO parse can place: still rejected, at the same gate, with the same message
#: (§V116 (a) -- normalizing is not loosening).
NOT_A_BOUND = ("july", "2026", "2026-01", "2026-13-01", "2026-07-01T99:00:00", "tomorrow")

#: The three announcements seeded below, oldest first.
_EN_FEED: list[dict[str, Any]] = [
    {
        "announceId": "ann-en-1",
        "title": "Older Event",
        "date": "2026-07-01T00:00:00+00:00",
        "url": "https://www.arknights.global/news/ann-en-1",
        "category": "event",
    },
    {
        "announceId": "ann-en-2",
        "title": "Middle Maintenance",
        "date": "2026-07-10T00:00:00+00:00",
        "url": "https://www.arknights.global/news/ann-en-2",
        "category": "maintenance",
    },
    {
        "announceId": "ann-en-3",
        "title": "Newest Banner",
        "date": "2026-07-20T00:00:00+00:00",
        "url": "https://www.arknights.global/news/ann-en-3",
        "category": "banner",
    },
]

#: Three banners whose open_times mirror the announcement dates, so one bound can be
#: compared across BOTH stored forms (day-granular date vs full timestamp) at once. The
#: 11:00 open time is the case a date bound must still include on both sides.
_EN_BANNERS = [
    ParsedBanner(
        game_id="LIMITED_1",
        display_name="Limited Headhunting",
        open_time="2026-07-20T11:00:00+00:00",
        end_time="2026-07-27T00:00:00+00:00",
        rule_type="LIMITED",
        featured_char_ids=[],
        provenance_record={"gachaPoolId": "LIMITED_1"},
    ),
    ParsedBanner(
        game_id="CLASSIC_1",
        display_name="Classic Headhunting",
        open_time="2026-07-10T11:00:00+00:00",
        end_time="2026-07-17T00:00:00+00:00",
        rule_type="CLASSIC",
        featured_char_ids=[],
        provenance_record={"gachaPoolId": "CLASSIC_1"},
    ),
    ParsedBanner(
        game_id="NORMAL_1",
        display_name="Standard Headhunting",
        open_time="2026-07-01T11:00:00+00:00",
        end_time="2026-07-08T00:00:00+00:00",
        rule_type="NORMAL",
        featured_char_ids=[],
        provenance_record={"gachaPoolId": "NORMAL_1"},
    ),
]


class _FakeFetcher:
    """Returns a preset announcement feed payload (no network, §V1)."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def fetch(self) -> Any:
        return self._payload


def _seed_registry(conn: sqlite3.Connection) -> None:
    """The two sources + the en snapshot the banner rows hang their provenance on.

    The announcement importer writes its OWN snapshot row, whose foreign key points at the
    region's news source, so that source is registered here too (:func:`source_id_for_region`
    -- the same constant the service probes, never a second spelling of it).
    """
    for source_id in (_SOURCE_ID, source_id_for_region("en")):
        conn.execute(
            "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
            "source_type, regions_json, adapter_version, license_status, permission_status, "
            "redistribution_status, attribution_text, enabled, last_reviewed_at) VALUES "
            "(?, 'gd', 'o', 'https://x/', 'game_data_repository', '[\"en\",\"cn\"]', '0', "
            "'reviewed', 'reviewed', 'derived', 'a', 1, '2026-07-21')",
            (source_id,),
        )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES "
        "('snap-en', ?, 'en', '2026-07-21T00:00:00+00:00', 'h', 'active', 'test')",
        (_SOURCE_ID,),
    )


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """One build carrying BOTH windowed domains, seeded through their real importers.

    The two stored forms are the point: ``announcements.date`` lands day-granular
    (``2026-07-01``) while ``banners.open_time`` keeps the full ``+00:00`` timestamp, and
    one bound has to be placed correctly against each.
    """
    path = tmp_path / "cand.sqlite"
    db = build_database(path)
    try:
        _seed_registry(db)
        import_announcements(db, _FakeFetcher(_EN_FEED), region="en")
        insert_banners(
            db,
            _EN_BANNERS,
            server="en",
            snapshot_id="snap-en",
            source_path="gamedata/excel/gacha_table.json",
        )
        db.commit()
    finally:
        db.close()
    return open_read_only(path)


def _announce_ids(conn: sqlite3.Connection, **kwargs: Any) -> list[str]:
    return [a.announce_id for a in get_announcements(conn, server="en", **kwargs).announcements]


def _banner_ids(conn: sqlite3.Connection, **kwargs: Any) -> list[str]:
    return [b.game_id for b in get_banners(conn, server="en", **kwargs).banners]


# --- the render itself (§V116 (a), §V37 single home) ---------------------------


@pytest.mark.parametrize(("written", "canonical"), NOTATIONS)
def test_every_accepted_notation_renders_canonically(written: str, canonical: str) -> None:
    assert canonical_iso_bound(written) == canonical


@pytest.mark.parametrize(("written", "canonical"), NOTATIONS)
def test_the_render_is_idempotent(written: str, canonical: str) -> None:
    # The gate renders, then each service renders again (one contract, both places): a
    # non-idempotent render would make the second pass change the window.
    assert canonical_iso_bound(canonical) == canonical


@pytest.mark.parametrize("value", NOT_A_BOUND)
def test_text_that_denotes_no_instant_is_still_rejected(value: str) -> None:
    # Normalizing is not loosening: rejection stays for text no ISO parse can place.
    with pytest.raises(ValueError):
        canonical_iso_bound(value)
    with pytest.raises(ValidationError):
        GetAnnouncementsInput(server="en", since=value)


def test_a_date_bound_stays_a_date_and_a_datetime_stays_a_datetime() -> None:
    # Granularity carries intent, so the render preserves it; only the column's owner
    # coarsens it (§V116 (b)).
    assert canonical_iso_bound("20260701") == "2026-07-01"
    assert canonical_iso_bound("20260701T000000") == "2026-07-01T00:00:00"


def test_day_granular_column_coarsens_inclusively_and_says_which_bound() -> None:
    assert canonical_window("2026-07-01T10:00:00", "2026-07-20", granularity="date") == (
        "2026-07-01",
        "2026-07-20",
    )
    assert coarsened_window_bounds(
        "2026-07-01T10:00:00", "2026-07-20T10:00:00", granularity="date"
    ) == ("since", "until")
    # No coarsening to report when both bounds already fit the column.
    assert coarsened_window_bounds("2026-07-01", "2026-07-20", granularity="date") == ()
    # A timestamp column keeps the caller's precision, so nothing is ever coarsened there.
    assert coarsened_window_bounds("2026-07-01T10:00:00", None, granularity="datetime") == ()


# --- the model gate normalizes (§V116 (a)) -------------------------------------


@pytest.mark.parametrize("model", (GetAnnouncementsInput, GetBannersInput))
@pytest.mark.parametrize(("written", "canonical"), NOTATIONS)
def test_model_gate_hands_the_service_canonical_bounds(
    model: type[GetAnnouncementsInput] | type[GetBannersInput], written: str, canonical: str
) -> None:
    parsed = model(server="en", since=written, until=written)
    assert (parsed.since, parsed.until) == (canonical, canonical)


# --- the predicate mirrors the SQL, sentinel included (§V116 (c)) --------------


@pytest.mark.parametrize(
    ("since", "until"),
    [
        # The intra-day pair B163 arm 3 rejected on every banner-open day of the build.
        ("2026-07-20T00:00:00", "2026-07-20"),
        ("2026-07-20T23:59:59", "2026-07-20"),
        # A mixed-notation pair: chronologically ascending, so it must ANSWER rather than
        # be rejected for its notation (T201's second message, superseded by the render).
        ("20260701", "2026-07-20"),
        ("2026-07-01T10:00:00Z", "2026-07-20T11:00:00+00:00"),
        ("2026-07-01", "2026-07-01"),
        ("2026-07-01", None),
        (None, "2026-07-01"),
        (None, None),
    ],
)
def test_a_window_the_sql_can_answer_is_not_rejected(since: str | None, until: str | None) -> None:
    assert inverted_window_reason(since, until) is None


@pytest.mark.parametrize(
    ("since", "until"),
    [
        ("2026-07-01", "2026-06-01"),
        ("2026-07-21", "2026-07-20T23:59:59"),
        ("2026-07-20T12:00:00", "2026-07-20T10:00:00"),
        # Mixed notations do not hide a genuine inversion either, now that both bounds are
        # rendered before the comparison.
        ("20260721", "2026-07-20"),
        ("2026-07-01T18:00:00+07:00", "2026-07-01T10:00:00Z"),
    ],
)
def test_a_window_nothing_can_satisfy_is_rejected_as_an_inversion(since: str, until: str) -> None:
    reason = inverted_window_reason(since, until)
    assert reason is not None
    assert since in reason and until in reason
    assert "swap" in reason
    # The superseded message must not come back: a notation mismatch is now normalized,
    # so "different ISO forms" would be a false account of what is wrong.
    assert "ISO forms" not in reason


@pytest.mark.parametrize(
    ("since", "until"),
    [
        ("2026-07-01", "2026-06-01"),
        ("2026-07-20T00:00:00", "2026-07-20"),
        ("20260701", "2026-07-20"),
        ("2026-07-21", "2026-07-20"),
        ("2026-07-01T10:00:00Z", "2026-07-01"),
    ],
)
def test_predicate_is_the_comparison_the_window_performs(since: str, until: str) -> None:
    # The guard must fire on exactly the pairs the SQL cannot match: canonical text,
    # upper bound terminated by the sentinel the banner SQL appends. One notation off in
    # either direction is a withheld answer (too wide) or a re-admitted B143 (too narrow).
    impossible = canonical_iso_bound(since) > canonical_iso_bound(until) + UNTIL_UPPER_SENTINEL
    assert (inverted_window_reason(since, until) is not None) is impossible


# --- the services render too (§V19's one-contract-both-places shape) -----------


@pytest.mark.parametrize(("written", "canonical"), NOTATIONS)
def test_announcement_window_is_notation_independent(
    conn: sqlite3.Connection, written: str, canonical: str
) -> None:
    # A direct service caller bypasses the model gate, so the service renders as well.
    assert _announce_ids(conn, since=written) == _announce_ids(conn, since=canonical)
    assert _announce_ids(conn, until=written) == _announce_ids(conn, until=canonical)


@pytest.mark.parametrize(("written", "canonical"), NOTATIONS)
def test_banner_window_is_notation_independent(
    conn: sqlite3.Connection, written: str, canonical: str
) -> None:
    assert _banner_ids(conn, since=written) == _banner_ids(conn, since=canonical)
    assert _banner_ids(conn, until=written) == _banner_ids(conn, until=canonical)


def test_basic_format_lower_bound_no_longer_empties_the_window(
    conn: sqlite3.Connection,
) -> None:
    # B163 arm 1, on both domains: this exact call returned nothing at all.
    assert _announce_ids(conn, since="20260701") == ["ann-en-3", "ann-en-2", "ann-en-1"]
    assert _banner_ids(conn, since="20260701") == ["LIMITED_1", "CLASSIC_1", "NORMAL_1"]


def test_basic_format_upper_bound_is_no_longer_ignored(conn: sqlite3.Connection) -> None:
    # B163 arm 2, the false-inclusion half: this call used to return the FULL archive.
    assert _announce_ids(conn, until="20260710") == ["ann-en-2", "ann-en-1"]
    assert _banner_ids(conn, until="20260710") == ["CLASSIC_1", "NORMAL_1"]


def test_the_intra_day_window_answers_instead_of_being_rejected(
    conn: sqlite3.Connection,
) -> None:
    # B163 arm 3: rejected as "impossible" before, though the banner opening at 11:00 that
    # day satisfies both bounds -- a rejection that withholds a row (§V116 (c)).
    assert _banner_ids(conn, since="2026-07-20T00:00:00", until="2026-07-20") == ["LIMITED_1"]


def test_a_mixed_notation_pair_answers_instead_of_being_rejected(
    conn: sqlite3.Connection,
) -> None:
    # T201 rejected this pair naming the notation; its instants are ordered, so answering
    # it is the truthful outcome (§V105 as amended).
    assert _banner_ids(conn, since="20260701", until="2026-07-10") == ["CLASSIC_1", "NORMAL_1"]
    assert _announce_ids(conn, since="20260701", until="2026-07-10") == ["ann-en-2", "ann-en-1"]


def test_a_genuinely_inverted_window_is_still_rejected_at_both_services(
    conn: sqlite3.Connection,
) -> None:
    for service in (get_announcements, get_banners):
        with pytest.raises(ValueError, match="can match nothing"):
            service(conn, server="en", since="2026-07-20", until="2026-07-01")  # type: ignore[operator]


def test_service_direct_junk_bound_is_rejected_not_silently_empty(
    conn: sqlite3.Connection,
) -> None:
    # The gate's rejection mirrored at the service: "july" used to reach the SQL and empty
    # the window there (B48's own harm, one layer down).
    for service in (get_announcements, get_banners):
        with pytest.raises(ValueError):
            service(conn, server="en", since="july")  # type: ignore[operator]


# --- the day-granular disclosure (§V116 (b)/§V26) ------------------------------


def test_sub_day_bound_keeps_its_day_and_discloses_the_widening(
    conn: sqlite3.Connection,
) -> None:
    # B163 arm 4: comparing "2026-07-01T10:00:00" against the day-granular date column
    # verbatim dropped that whole day, and the day is exactly what the column can place.
    result = get_announcements(conn, server="en", since="2026-07-01T10:00:00")
    assert [a.announce_id for a in result.announcements] == ["ann-en-3", "ann-en-2", "ann-en-1"]
    text = " ".join(result.limitations)
    assert "since bound" in text and "whole day" in text


def test_the_disclosure_names_both_bounds_when_both_are_widened(
    conn: sqlite3.Connection,
) -> None:
    result = get_announcements(
        conn, server="en", since="2026-07-01T10:00:00", until="2026-07-20T10:00:00"
    )
    text = " ".join(result.limitations)
    assert "since and until bounds" in text


def test_no_disclosure_when_no_bound_was_widened(conn: sqlite3.Connection) -> None:
    # A caveat that always fires teaches a client nothing (§V71 (f)): a day-granular bound
    # on a day-granular column loses nothing, and the plain listing carries no window.
    for kwargs in ({"since": "2026-07-01", "until": "2026-07-20"}, {}):
        result = get_announcements(conn, server="en", **kwargs)  # type: ignore[arg-type]
        assert all("whole day" not in limitation for limitation in result.limitations)


def test_banner_window_needs_no_granularity_caveat(conn: sqlite3.Connection) -> None:
    # open_time is a full timestamp, so a sub-day bound applies as written -- the caveat
    # belongs to the day-granular domain only, never as boilerplate on both.
    result = get_banners(conn, server="en", since="2026-07-20T10:00:00")
    assert [b.game_id for b in result.banners] == ["LIMITED_1"]
    assert all("whole day" not in limitation for limitation in result.limitations)


def test_the_disclosure_is_client_safe_text(conn: sqlite3.Connection) -> None:
    # §V71 (b): it reaches a client verbatim, so no internal cite or jargon rides it.
    result = get_announcements(conn, server="en", since="2026-07-01T10:00:00")
    text = " ".join(result.limitations)
    for token in ("§V", "§T", "B163", "B48", "granular", "lexicograph", "collat"):
        assert token not in text


def test_the_windowed_empty_advice_is_only_given_when_widening_can_help(
    conn: sqlite3.Connection,
) -> None:
    # T206's "widen or drop the since/until bounds" was FALSE advice under B163 arm 1: the
    # window was empty because of the notation, and no widening could have fixed it. With
    # the render in place, an empty window really is a window question again.
    empty = get_announcements(conn, server="en", since="2026-08-01", until="2026-08-02")
    assert empty.announcements == ()
    assert any("widen or drop" in limitation for limitation in empty.limitations)
    answered = get_announcements(conn, server="en", since="20260701")
    assert answered.announcements
    assert all("widen or drop" not in limitation for limitation in answered.limitations)
