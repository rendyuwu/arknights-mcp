"""T88: migration 0009 drop-rate domain schema (§V17/§V53).

``items`` + ``stage_drops`` back the v0.2 M8 penguin drop-rate cache. These tests
assert the migration applies cleanly, that both tables carry a provenance FK (§V17),
that ``stage_drops`` carries the §V53 stale/attribution columns (fetched_at,
expires_at, penguin snapshot_id, region), that the foreign keys are enforced, and
that a drop row round-trips its rate/sample/expiry. Schema only -- the importer
(T89), analyzer (T90), and tool (T91) land separately.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.migrations import build_database
from arknights_mcp.db.promotion import read_schema_version

_FIELD_POLICY_VERSION = "test"
_TRANSFORM_VERSION = "test"


def _seed(conn: sqlite3.Connection) -> tuple[int, int, str]:
    """Insert the minimal source/snapshot/provenance/stage/item chain.

    Returns ``(stage_pk, item_pk, snapshot_id)`` for a drop-row insert.
    """
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) VALUES "
        "('penguin_statistics', 'Penguin', 'PS', 'https://penguin-stats.io', 'drop_rate', "
        "'[\"en\"]', '1', 'reviewed', 'permitted', 'code_only', 'Penguin Stats', 1, '2026-07-20')"
    )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES "
        "('snap-en-1', 'penguin_statistics', 'en', '2026-07-20T00:00:00+00:00', 'h', "
        "'active', ?)",
        (_FIELD_POLICY_VERSION,),
    )
    prov_id = conn.execute(
        "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
        "record_hash, transform_version, field_policy_version) VALUES "
        "('snap-en-1', 'result/matrix', 'main_04-04/30012', 'rh', ?, ?)",
        (_TRANSFORM_VERSION, _FIELD_POLICY_VERSION),
    ).lastrowid
    stage_pk = conn.execute(
        "INSERT INTO stages (server, game_id, provenance_id) VALUES ('en', 'main_04-04', ?)",
        (prov_id,),
    ).lastrowid
    item_pk = conn.execute(
        "INSERT INTO items (server, game_id, display_name, provenance_id) VALUES "
        "('en', '30012', 'Orirock', ?)",
        (prov_id,),
    ).lastrowid
    conn.commit()
    assert stage_pk is not None and item_pk is not None
    return stage_pk, item_pk, "snap-en-1"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_0009_is_latest_schema_version(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        assert read_schema_version(conn) == "0009_drops_domain"
    finally:
        conn.close()


def test_items_and_stage_drops_tables_exist(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        present = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"items", "stage_drops"} <= present
    finally:
        conn.close()


def test_both_tables_carry_provenance_fk(tmp_path: Path) -> None:
    # §V17: every imported record carries provenance.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        assert "provenance_id" in _columns(conn, "items")
        assert "provenance_id" in _columns(conn, "stage_drops")
        item_fks = {row[2] for row in conn.execute("PRAGMA foreign_key_list(items)")}
        assert "record_provenance" in item_fks
        drop_fks = {row[2] for row in conn.execute("PRAGMA foreign_key_list(stage_drops)")}
        assert {"record_provenance", "source_snapshots", "stages", "items"} <= drop_fks
    finally:
        conn.close()


def test_stage_drops_carries_v53_stale_and_attribution_columns(tmp_path: Path) -> None:
    # §V53: drop fact carries fetched_at + expires_at + penguin snapshot_id + region.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        cols = _columns(conn, "stage_drops")
        assert {
            "stage_pk",
            "item_pk",
            "region",
            "quantity",
            "times",
            "drop_rate",
            "snapshot_id",
            "fetched_at",
            "expires_at",
            "provenance_id",
        } <= cols
    finally:
        conn.close()


def test_stage_drop_roundtrips_rate_sample_and_expiry(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        stage_pk, item_pk, snap = _seed(conn)
        prov = conn.execute("SELECT provenance_id FROM record_provenance").fetchone()[0]
        conn.execute(
            "INSERT INTO stage_drops (stage_pk, item_pk, region, quantity, times, drop_rate, "
            "snapshot_id, fetched_at, expires_at, provenance_id) VALUES "
            "(?, ?, 'en', 123, 1000, 0.123, ?, '2026-07-20T00:00:00+00:00', "
            "'2026-07-27T00:00:00+00:00', ?)",
            (stage_pk, item_pk, snap, prov),
        )
        conn.commit()
        row = conn.execute(
            "SELECT region, quantity, times, drop_rate, snapshot_id, expires_at FROM stage_drops"
        ).fetchone()
        assert row == ("en", 123, 1000, 0.123, "snap-en-1", "2026-07-27T00:00:00+00:00")
    finally:
        conn.close()


def test_stage_drop_provenance_fk_enforced(tmp_path: Path) -> None:
    # A drop with a dangling provenance_id is rejected (FKs on, §V17 fail-closed).
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        stage_pk, item_pk, snap = _seed(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO stage_drops (stage_pk, item_pk, region, snapshot_id, "
                "fetched_at, expires_at, provenance_id) VALUES "
                "(?, ?, 'en', ?, 'f', 'e', 999999)",
                (stage_pk, item_pk, snap),
            )
            conn.commit()
    finally:
        conn.close()


def test_stage_drop_unique_stage_item(tmp_path: Path) -> None:
    # UNIQUE(stage_pk, item_pk): a duplicate (stage, item) drop collides so the
    # importer (T89) can map the anomaly to a typed ImporterError (§V33 pattern).
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        stage_pk, item_pk, snap = _seed(conn)
        prov = conn.execute("SELECT provenance_id FROM record_provenance").fetchone()[0]
        insert = (
            "INSERT INTO stage_drops (stage_pk, item_pk, region, snapshot_id, "
            "fetched_at, expires_at, provenance_id) VALUES (?, ?, 'en', ?, 'f', 'e', ?)"
        )
        conn.execute(insert, (stage_pk, item_pk, snap, prov))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (stage_pk, item_pk, snap, prov))
        conn.commit()
    finally:
        conn.close()
