"""T95: the official-announcement importer (feed -> announcements, metadata-ONLY).

Covers the §T95 contract with an in-memory fake fetcher (no live network, §V1): the
field allowlist that keeps ONLY the §V56 metadata and drops any body/html/prose/image
(§V16/§V18), the recursive string sanitize (§V18), the en/cn region stamp never mixed
(§V5/§V56), per-record provenance + an announcement snapshot row (§V17), the
fail-closed skip of an entry missing an announceId, the §V30 non-empty-or-fail guard,
and the §V33 duplicate-id guard.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.announcements import (
    import_announcements,
    parse_announcements,
)
from arknights_mcp.importers.enemies import ImporterError

_FETCHED = datetime(2026, 7, 21, tzinfo=UTC)


class _FakeFetcher:
    """Returns a preset feed payload; records that it was called."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.calls = 0

    def fetch(self) -> Any:
        self.calls += 1
        return self._payload


def _seed_sources(conn: sqlite3.Connection) -> None:
    for source_id, region in (
        ("arknights_global_official_news", "en"),
        ("arknights_cn_official_news", "cn"),
    ):
        conn.execute(
            "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
            "source_type, regions_json, adapter_version, license_status, permission_status, "
            "redistribution_status, attribution_text, enabled, last_reviewed_at) VALUES "
            "(?, ?, 'owner', 'https://x', 'official_announcement_website', ?, '0.0', 'reviewed', "
            "'metadata_only', 'code_only', 'attribution', 0, '2026-07-21')",
            (source_id, source_id, f'["{region}"]'),
        )


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_sources(conn)
    return conn


def _entry(announce_id: str, **extra: Any) -> dict[str, Any]:
    return {"announceId": announce_id, "title": "Maintenance", "category": "maintenance", **extra}


# --- field allowlist: metadata-only, no body/prose (§V16/§V18/§V56) -----------


def test_parse_keeps_only_metadata_and_drops_body() -> None:
    parsed = parse_announcements(
        [
            {
                "announceId": "ann-1001",
                "title": "Event Start",
                "date": "2026-07-21T00:00:00+00:00",
                "url": "https://www.arknights.global/news/ann-1001",
                "category": "event",
                # forbidden non-metadata (§V16): must never survive the allowlist.
                "body": "the full article body that must never be stored",
                "html": "<p>...</p>",
                "content": "prose",
                "imageUrl": "https://cdn/x.png",
            }
        ],
        fetched_at=_FETCHED,
    )
    assert len(parsed) == 1
    kept = parsed[0].provenance_record
    assert set(kept) == {"announceId", "title", "date", "url", "category"}
    assert "body" not in kept
    assert "html" not in kept
    assert "content" not in kept
    assert "imageUrl" not in kept


def test_end_to_end_stores_no_body_column(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        fetcher = _FakeFetcher([_entry("ann-1001", body="forbidden")])
        import_announcements(conn, fetcher, region="en", fetched_at=_FETCHED)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(announcements)")}
        assert not any(bad in c.lower() for c in cols for bad in ("body", "html", "prose", "image"))
        row = conn.execute(
            "SELECT region, announce_id, title, category FROM announcements"
        ).fetchone()
        assert row == ("en", "ann-1001", "Maintenance", "maintenance")
    finally:
        conn.close()


# --- recursive sanitize (§V18) ------------------------------------------------


def test_title_control_chars_sanitized(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        # U+202E RIGHT-TO-LEFT OVERRIDE (a Cf bidi control) must be stripped. A mapped
        # field (category) is present so the §V30/§V61 all-NULL degradation guard is not
        # the thing under test here.
        fetcher = _FakeFetcher(
            [{"announceId": "ann-1001", "title": "Ann‮ouncement", "category": "event"}]
        )
        import_announcements(conn, fetcher, region="en", fetched_at=_FETCHED)
        title = conn.execute("SELECT title FROM announcements").fetchone()[0]
        assert "‮" not in title
        assert title == "Announcement"
    finally:
        conn.close()


# --- happy path ---------------------------------------------------------------


def test_happy_path_inserts_row(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        fetcher = _FakeFetcher(
            [
                {
                    "announceId": "ann-1001",
                    "title": "Maintenance Notice",
                    "date": "2026-07-21T00:00:00+00:00",
                    "url": "https://www.arknights.global/news/ann-1001",
                    "category": "maintenance",
                }
            ]
        )
        result = import_announcements(conn, fetcher, region="en", fetched_at=_FETCHED)
        assert result.region == "en"
        assert (result.announcements_inserted, result.announcements_skipped) == (1, 0)
        row = conn.execute(
            "SELECT region, announce_id, title, date, url, category FROM announcements"
        ).fetchone()
        assert row == (
            "en",
            "ann-1001",
            "Maintenance Notice",
            "2026-07-21T00:00:00+00:00",
            "https://www.arknights.global/news/ann-1001",
            "maintenance",
        )
    finally:
        conn.close()


def test_feed_wrapped_in_object_is_accepted(tmp_path: Path) -> None:
    # §V56 tolerant shape: a feed may wrap the list under a common key.
    conn = _db(tmp_path)
    try:
        fetcher = _FakeFetcher({"announceList": [_entry("ann-1001")]})
        result = import_announcements(conn, fetcher, region="en", fetched_at=_FETCHED)
        assert result.announcements_inserted == 1
    finally:
        conn.close()


# --- provenance + snapshot (§V17) ---------------------------------------------


def test_row_carries_provenance_and_announcement_snapshot(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        fetcher = _FakeFetcher([_entry("ann-1001")])
        result = import_announcements(conn, fetcher, region="en", fetched_at=_FETCHED)
        snap = conn.execute(
            "SELECT snapshot_id, source_id, server, fetched_at FROM source_snapshots "
            "WHERE source_id = 'arknights_global_official_news'"
        ).fetchone()
        assert snap == (
            result.snapshot_id,
            "arknights_global_official_news",
            "en",
            _FETCHED.isoformat(),
        )
        prov = conn.execute(
            "SELECT p.snapshot_id, p.source_path, p.source_record_key FROM announcements a "
            "JOIN record_provenance p ON p.provenance_id = a.provenance_id"
        ).fetchone()
        assert prov == (result.snapshot_id, "announcements/en", "ann-1001")
    finally:
        conn.close()


# --- region never mixed (§V56/§V5) --------------------------------------------


def test_cn_import_labels_every_row_cn_and_leaves_en_untouched(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        import_announcements(
            conn, _FakeFetcher([_entry("ann-en")]), region="en", fetched_at=_FETCHED
        )
        import_announcements(
            conn, _FakeFetcher([_entry("ann-cn")]), region="cn", fetched_at=_FETCHED
        )
        by_region = {
            region: announce_id
            for region, announce_id in conn.execute("SELECT region, announce_id FROM announcements")
        }
        assert by_region == {"en": "ann-en", "cn": "ann-cn"}
    finally:
        conn.close()


def test_same_announce_id_in_both_regions_is_allowed(tmp_path: Path) -> None:
    # §V5: UNIQUE(region, announce_id) -- the same id in a DIFFERENT region is fine.
    conn = _db(tmp_path)
    try:
        import_announcements(
            conn, _FakeFetcher([_entry("ann-1001")]), region="en", fetched_at=_FETCHED
        )
        import_announcements(
            conn, _FakeFetcher([_entry("ann-1001")]), region="cn", fetched_at=_FETCHED
        )
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 2
    finally:
        conn.close()


def test_bad_region_rejected(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        with pytest.raises(ImporterError, match="region must be en"):
            import_announcements(
                conn, _FakeFetcher([_entry("x")]), region="jp", fetched_at=_FETCHED
            )
    finally:
        conn.close()


# --- fail-closed skip of an entry missing announceId --------------------------


def test_entry_missing_announce_id_skipped_not_fabricated(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        fetcher = _FakeFetcher(
            [
                _entry("ann-1001"),
                {"title": "no id here", "category": "event"},  # missing announceId
            ]
        )
        result = import_announcements(conn, fetcher, region="en", fetched_at=_FETCHED)
        assert (result.announcements_inserted, result.announcements_skipped) == (1, 1)
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 1
    finally:
        conn.close()


# --- §V30 non-empty-or-fail + legitimate empty feed ---------------------------


def test_feed_with_entries_but_no_ids_fails_closed(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        # A shape mismatch: entries present, but none carry announceId -> zero rows.
        fetcher = _FakeFetcher([{"id": "wrong-key-1"}, {"id": "wrong-key-2"}])
        with pytest.raises(ImporterError, match="silent empty announcement build"):
            import_announcements(conn, fetcher, region="en", fetched_at=_FETCHED)
        # Fail closed BEFORE the snapshot write: no announcement snapshot persisted.
        snaps = conn.execute(
            "SELECT COUNT(*) FROM source_snapshots "
            "WHERE source_id = 'arknights_global_official_news'"
        ).fetchone()[0]
        assert snaps == 0
    finally:
        conn.close()


def test_empty_feed_imports_zero_without_error(tmp_path: Path) -> None:
    # An empty feed is a legitimate empty build (announcements not a CRITICAL_TABLE,
    # disabled by default D14/§V56) -- unlike the all-ids-missing shape mismatch above.
    conn = _db(tmp_path)
    try:
        result = import_announcements(conn, _FakeFetcher([]), region="en", fetched_at=_FETCHED)
        assert (result.announcements_inserted, result.announcements_skipped) == (0, 0)
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 0
    finally:
        conn.close()


# --- §V33 duplicate id --------------------------------------------------------


def test_duplicate_announce_id_maps_to_importer_error(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        fetcher = _FakeFetcher([_entry("ann-1001"), _entry("ann-1001", title="dup")])
        with pytest.raises(ImporterError, match="duplicates"):
            import_announcements(conn, fetcher, region="en", fetched_at=_FETCHED)
    finally:
        conn.close()


# --- parse-shape guard --------------------------------------------------------


def test_parse_rejects_non_list_non_wrapped_shape() -> None:
    with pytest.raises(ImporterError, match="must be a JSON array"):
        parse_announcements("not a feed", fetched_at=_FETCHED)


# --- T107/§V61: real official feed field-map (day/month/webUrl/group) ----------


def _real_feed_entry(announce_id: str, **extra: Any) -> dict[str, Any]:
    """A real-official-feed-shaped entry (§V61): day/month ints, webUrl, group."""
    return {
        "announceId": announce_id,
        "title": "Maintenance",
        "day": 15,
        "month": 7,
        "webUrl": f"https://ak-conf.hypergryph.com/news/{announce_id}",
        "group": "SYSTEM",
        **extra,
    }


def test_parse_maps_real_feed_shape_to_canonical_fields() -> None:
    # §V61: the real feed names three fields differently; the field-map normalizes
    # day+month->date (year from fetched_at), webUrl->url, group->category.
    parsed = parse_announcements(
        [
            {
                "announceId": "ann-1001",
                "title": "Event",
                "day": 15,
                "month": 7,
                "webUrl": "https://ak-conf.hypergryph.com/news/ann-1001",
                "group": "ACTIVITY",
                # forbidden non-metadata (§V16): must never survive the allowlist.
                "webBody": "the full article body that must never be stored",
                "imgUrl": "https://cdn/x.png",
            }
        ],
        fetched_at=_FETCHED,
    )
    assert len(parsed) == 1
    ann = parsed[0]
    assert ann.date == "2026-07-15"  # year inferred from _FETCHED (2026-07-21)
    assert ann.url == "https://ak-conf.hypergryph.com/news/ann-1001"
    assert ann.category == "ACTIVITY"
    # The forbidden body/image never survived (§V16/§V18).
    assert "webBody" not in ann.provenance_record
    assert "imgUrl" not in ann.provenance_record


def test_real_feed_shape_stores_non_null_date_url_category(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        fetcher = _FakeFetcher([_real_feed_entry("ann-1001")])
        import_announcements(conn, fetcher, region="en", fetched_at=_FETCHED)
        row = conn.execute("SELECT date, url, category FROM announcements").fetchone()
        assert row == (
            "2026-07-15",
            "https://ak-conf.hypergryph.com/news/ann-1001",
            "SYSTEM",
        )
    finally:
        conn.close()


def test_explicit_canonical_key_wins_over_source_key() -> None:
    # A feed carrying BOTH the canonical and the source key keeps the canonical value
    # (the T95 shape stays valid alongside the §V61 field-map).
    parsed = parse_announcements(
        [
            {
                "announceId": "ann-1001",
                "date": "2026-01-02T00:00:00+00:00",
                "url": "https://canonical/url",
                "category": "canonical",
                "day": 15,
                "month": 7,
                "webUrl": "https://source/webUrl",
                "group": "SOURCE",
            }
        ],
        fetched_at=_FETCHED,
    )
    ann = parsed[0]
    assert ann.date == "2026-01-02T00:00:00+00:00"
    assert ann.url == "https://canonical/url"
    assert ann.category == "canonical"


def test_december_entry_seen_in_january_rolls_year_back() -> None:
    # §V61 Dec->Jan rollover: an entry whose month is AFTER the fetch month belongs to
    # the prior year (a December announcement first seen the following January).
    fetched_january = datetime(2027, 1, 5, tzinfo=UTC)
    parsed = parse_announcements(
        [{"announceId": "ann-1001", "day": 20, "month": 12, "group": "SYSTEM"}],
        fetched_at=fetched_january,
    )
    assert parsed[0].date == "2026-12-20"


def test_numeric_group_enum_maps_to_category() -> None:
    parsed = parse_announcements(
        [{"announceId": "ann-1001", "day": 1, "month": 7, "group": 3}],
        fetched_at=_FETCHED,
    )
    assert parsed[0].category == "3"


def test_out_of_range_day_month_yields_no_date() -> None:
    parsed = parse_announcements(
        [{"announceId": "ann-1001", "day": 99, "month": 13, "group": "SYSTEM"}],
        fetched_at=_FETCHED,
    )
    assert parsed[0].date is None
    # group still maps, so the row is not an all-NULL degradation.
    assert parsed[0].category == "SYSTEM"


# --- §V30/§V61 degradation guard: rows survive but every mapped field is NULL --


def test_feed_with_ids_but_no_mapped_fields_fails_closed(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        # Entries carry an announceId (so the missing-id guard does NOT trip) but none
        # of date/url/category/day/month/webUrl/group -> every row all-NULL mapped.
        fetcher = _FakeFetcher(
            [{"announceId": "ann-1001", "title": "x"}, {"announceId": "ann-1002", "title": "y"}]
        )
        with pytest.raises(ImporterError, match="silent degraded announcement build"):
            import_announcements(conn, fetcher, region="en", fetched_at=_FETCHED)
        # Fail closed BEFORE the snapshot write: no announcement snapshot persisted (§V3).
        snaps = conn.execute(
            "SELECT COUNT(*) FROM source_snapshots "
            "WHERE source_id = 'arknights_global_official_news'"
        ).fetchone()[0]
        assert snaps == 0
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 0
    finally:
        conn.close()


def test_partial_mapped_feed_promotes(tmp_path: Path) -> None:
    # At least one row carries a mapped field -> NOT an all-NULL degradation; the build
    # proceeds (§V61 guards the all-NULL case only, not a single sparse row).
    conn = _db(tmp_path)
    try:
        fetcher = _FakeFetcher(
            [
                {"announceId": "ann-1001", "title": "no mapped fields"},
                _real_feed_entry("ann-1002"),
            ]
        )
        result = import_announcements(conn, fetcher, region="en", fetched_at=_FETCHED)
        assert result.announcements_inserted == 2
    finally:
        conn.close()
