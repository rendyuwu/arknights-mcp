"""T12: the migration runner applies the core-metadata schema, records versions
with checksums, is idempotent, detects drift, and produces a DB that passes
integrity + foreign-key checks. The record_provenance table carries the §V17
provenance columns.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.migrations import (
    MigrationError,
    apply_migrations,
    build_database,
    default_migrations_dir,
)

CORE_TABLES = {
    "schema_migrations",
    "data_sources",
    "source_snapshots",
    "record_provenance",
    "source_policy_events",
}

PROVENANCE_COLUMNS = {
    "provenance_id",
    "snapshot_id",
    "source_path",
    "source_record_key",
    "record_hash",
    "transform_version",
    "field_policy_version",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_applies_core_tables(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "candidate.sqlite")
    assert _table_names(conn) >= CORE_TABLES


def test_records_version_and_checksum(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "candidate.sqlite")
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    versions = {v for v, _ in rows}
    assert "0001_core_metadata" in versions
    assert all(checksum for _, checksum in rows)


def test_idempotent_reapply(tmp_path: Path) -> None:
    db = tmp_path / "candidate.sqlite"
    conn = build_database(db)
    newly = apply_migrations(conn)  # re-run against the same DB
    assert newly == []


def test_integrity_and_foreign_key_checks_pass(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "candidate.sqlite")
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_provenance_columns_v17(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "candidate.sqlite")
    assert _column_names(conn, "record_provenance") >= PROVENANCE_COLUMNS


def test_foreign_keys_enforced(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "candidate.sqlite")
    # source_snapshots.source_id references data_sources; unknown parent must fail.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_snapshots "
            "(snapshot_id, source_id, server, imported_at, manifest_hash, status, "
            "field_policy_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("en:abc", "nonexistent_source", "en", "2026-07-17", "hash", "imported", "1"),
        )
        conn.commit()


def test_policy_event_type_check(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "candidate.sqlite")
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("s", "S", "O", "https://x", "t", "[]", "1", "l", "p", "r", "a", 1, "2026-07-17"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_policy_events (source_id, event_type, created_at) VALUES (?,?,?)",
            ("s", "not_a_valid_event", "2026-07-17"),
        )
        conn.commit()


def test_checksum_drift_detected(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "candidate.sqlite")
    # Tamper the recorded checksum, then re-run: drift must raise.
    conn.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version LIKE '0001%'")
    conn.commit()
    with pytest.raises(MigrationError, match="checksum drift"):
        apply_migrations(conn)


def test_default_migrations_dir_exists() -> None:
    assert (default_migrations_dir() / "0001_core_metadata.sql").is_file()
