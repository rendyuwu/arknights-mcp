"""T94: migration 0010 announcement-metadata domain schema (§V16/§V17/§V56).

``announcements`` backs the v0.2 M9 official-announcement adapter (D14). These tests
assert the migration applies cleanly, that the table carries a provenance FK (§V17),
that the column set is METADATA-ONLY -- a subset of {announce_id, title, date, url,
category, region} plus the pk/provenance bookkeeping, so there is no place to store
an article body/html/prose/image (§V16/§V56) -- that ``region`` is NOT NULL (§V5), and
that a metadata row round-trips. Schema only: the adapter/importer (T95) and tool
(T96) land separately.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.migrations import build_database

_FIELD_POLICY_VERSION = "test"
_TRANSFORM_VERSION = "test"

#: The complete, metadata-only column set (§V56). announcement_pk + provenance_id are
#: bookkeeping; the rest are exactly the six allowed metadata fields. A future
#: body/html/prose/image column would break the subset assertion below (§V16).
_ALLOWED_COLUMNS = {
    "announcement_pk",
    "region",
    "announce_id",
    "title",
    "date",
    "url",
    "category",
    "provenance_id",
}

#: Columns that would smuggle in the forbidden full-body/prose/image content (§V16).
_FORBIDDEN_SUBSTRINGS = ("body", "html", "prose", "image", "content", "text")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _seed_provenance(conn: sqlite3.Connection) -> int:
    """Insert the minimal source/snapshot/provenance chain; return provenance_id."""
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) VALUES "
        "('arknights_global_official_news', 'Official news', 'Yostar', "
        "'https://www.arknights.global/', 'official_announcement_website', '[\"en\"]', '0.0', "
        "'first_party_copyrighted_website', 'metadata_only_maximum_scope_reviewed', "
        "'metadata_only', 'Announcement dates (c) Hypergryph / Yostar.', 0, '2026-07-21')"
    )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES "
        "('snap-en-ann', 'arknights_global_official_news', 'en', '2026-07-21T00:00:00+00:00', "
        "'h', 'active', ?)",
        (_FIELD_POLICY_VERSION,),
    )
    prov_id = conn.execute(
        "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
        "record_hash, transform_version, field_policy_version) VALUES "
        "('snap-en-ann', 'announcements/en', 'ann-1001', 'rh', ?, ?)",
        (_TRANSFORM_VERSION, _FIELD_POLICY_VERSION),
    ).lastrowid
    conn.commit()
    assert prov_id is not None
    return int(prov_id)


def test_announcements_table_exists(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        present = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "announcements" in present
    finally:
        conn.close()


def test_integrity_and_foreign_key_checks_pass(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_metadata_only_column_set(tmp_path: Path) -> None:
    # §V16/§V56: the schema holds ONLY announce metadata -- no body/html/prose/image
    # column exists to store the forbidden full announcement content.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        cols = _columns(conn, "announcements")
        assert cols == _ALLOWED_COLUMNS
        for col in cols:
            assert not any(bad in col.lower() for bad in _FORBIDDEN_SUBSTRINGS)
    finally:
        conn.close()


def test_region_is_not_null(tmp_path: Path) -> None:
    # §V5: every announcement is region-attributed; region cannot be NULL.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        region_col = next(
            row for row in conn.execute("PRAGMA table_info(announcements)") if row[1] == "region"
        )
        assert region_col[3] == 1  # notnull flag
    finally:
        conn.close()


def test_provenance_fk_present_and_enforced(tmp_path: Path) -> None:
    # §V17: an announcement carries provenance; a dangling provenance_id is rejected.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        assert "provenance_id" in _columns(conn, "announcements")
        fks = {row[2] for row in conn.execute("PRAGMA foreign_key_list(announcements)")}
        assert fks == {"record_provenance"}
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO announcements (region, announce_id, provenance_id) "
                "VALUES ('en', 'ann-1001', 999999)"
            )
            conn.commit()
    finally:
        conn.close()


def test_announcement_metadata_roundtrips(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        conn.execute(
            "INSERT INTO announcements (region, announce_id, title, date, url, category, "
            "provenance_id) VALUES ('en', 'ann-1001', 'Maintenance Notice', "
            "'2026-07-21T00:00:00+00:00', 'https://www.arknights.global/news/ann-1001', "
            "'maintenance', ?)",
            (prov,),
        )
        conn.commit()
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


def test_unique_region_announce_id(tmp_path: Path) -> None:
    # UNIQUE(region, announce_id): a duplicate (region, announce_id) collides so the
    # importer (T95) can map the anomaly to a typed ImporterError (§V33 pattern);
    # the same announce_id in a DIFFERENT region is allowed (§V5 region separation).
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        insert = "INSERT INTO announcements (region, announce_id, provenance_id) VALUES (?, ?, ?)"
        conn.execute(insert, ("en", "ann-1001", prov))
        conn.execute(insert, ("cn", "ann-1001", prov))  # different region: allowed
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("en", "ann-1001", prov))  # duplicate: collides
        conn.commit()
    finally:
        conn.close()
