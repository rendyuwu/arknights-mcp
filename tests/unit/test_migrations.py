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

# T19 / PRD §12.3 (operator) + §12.6 (analysis). Enemy (§12.4) + stage (§12.5)
# tables are covered by their own migration tests; this file guards the full set.
OPERATOR_TABLES = {
    "operators",
    "operator_aliases",
    "operator_phases",
    "skills",
    "operator_skills",
    "skill_levels",
    "talents",
    "talent_levels",
    "modules",
    "module_levels",
}

ANALYSIS_TABLES = {"analysis_rules", "analysis_findings"}

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


# --- T19: operator (§12.3) + analysis (§12.6) domain migrations -------------


def _seed_provenance(conn: sqlite3.Connection) -> int:
    """Seed a minimal source -> snapshot -> provenance chain; return its pk."""
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("s", "S", "O", "https://x", "t", '["en"]', "1", "l", "p", "r", "a", 1, "2026-07-17"),
    )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES (?,?,?,?,?,?,?)",
        ("en:abc", "s", "en", "2026-07-17", "mh", "imported", "1"),
    )
    cur = conn.execute(
        "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
        "record_hash, transform_version, field_policy_version) VALUES (?,?,?,?,?,?)",
        ("en:abc", "gamedata/excel/character_table.json", "char_002_amiya", "h", "1", "1"),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_applies_operator_and_analysis_tables(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "candidate.sqlite")
    assert _table_names(conn) >= OPERATOR_TABLES | ANALYSIS_TABLES


def test_operator_domain_columns(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "candidate.sqlite")
    assert _column_names(conn, "operators") >= {
        "operator_pk",
        "canonical_entity_id",
        "server",
        "game_id",
        "display_name",
        "rarity",
        "profession",
        "subclass_id",
        "position",
        "tag_json",
        "obtainable",
        "provenance_id",
    }
    assert _column_names(conn, "operator_phases") >= {
        "operator_pk",
        "phase",
        "max_level",
        "max_hp",
        "atk",
        "def",
        "res",
        "redeploy_time",
        "cost",
        "block_count",
        "attack_interval",
        "range_id",
    }
    assert _column_names(conn, "skills") >= {
        "skill_pk",
        "server",
        "game_id",
        "skill_type",
        "sp_type",
        "duration_type",
        "provenance_id",
    }
    assert _column_names(conn, "modules") >= {
        "module_pk",
        "canonical_entity_id",
        "server",
        "game_id",
        "operator_pk",
        "module_type",
        "unlock_phase",
        "unlock_level",
        "provenance_id",
    }


def test_gameplay_description_columns_present_but_optional(tmp_path: Path) -> None:
    # V16: policy-controlled prose columns exist in the schema (importer excludes
    # them by default) and are nullable, never NOT NULL.
    conn = build_database(tmp_path / "candidate.sqlite")
    for table in ("skill_levels", "module_levels", "talent_levels"):
        cols = {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}
        assert "gameplay_description" in cols
        assert cols["gameplay_description"][3] == 0  # notnull flag is 0


def test_analysis_findings_carries_v6_fields(tmp_path: Path) -> None:
    # V6: a stored observation carries rule_id + evidence + confidence +
    # analyzer_version (limitations live inside finding_json per §12.6).
    conn = build_database(tmp_path / "candidate.sqlite")
    assert _column_names(conn, "analysis_findings") >= {
        "rule_id",
        "confidence",
        "evidence_json",
        "finding_json",
        "analyzer_version",
    }
    assert _column_names(conn, "analysis_rules") >= {
        "rule_id",
        "rule_version",
        "domain",
        "name",
        "severity_default",
        "enabled",
    }


def test_operator_provenance_fk_enforced(tmp_path: Path) -> None:
    # V17: operators reference record_provenance; an unknown parent must fail.
    conn = build_database(tmp_path / "candidate.sqlite")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO operators (server, game_id, provenance_id) VALUES (?,?,?)",
            ("en", "char_002_amiya", 999),
        )
        conn.commit()


def test_module_operator_fk_enforced(tmp_path: Path) -> None:
    # §12.1: modules reference operators; unknown operator_pk must fail even with
    # a valid provenance row (isolating the operator FK).
    conn = build_database(tmp_path / "candidate.sqlite")
    provenance_id = _seed_provenance(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO modules (server, game_id, operator_pk, provenance_id) VALUES (?,?,?,?)",
            ("en", "uniequip_001_amiya", 999, provenance_id),
        )
        conn.commit()


def test_analysis_finding_rule_fk_enforced(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "candidate.sqlite")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO analysis_findings (entity_type, entity_pk, rule_id, analyzer_version, "
            "created_at) VALUES (?,?,?,?,?)",
            ("stage", 1, "no_such_rule", "1", "2026-07-17"),
        )
        conn.commit()


def test_operator_and_analysis_round_trip(tmp_path: Path) -> None:
    # A valid operator -> module -> finding chain inserts cleanly and the whole
    # DB still passes integrity + foreign-key checks (§12.1).
    conn = build_database(tmp_path / "candidate.sqlite")
    provenance_id = _seed_provenance(conn)
    cur = conn.execute(
        "INSERT INTO operators (server, game_id, display_name, provenance_id) VALUES (?,?,?,?)",
        ("en", "char_002_amiya", "Amiya", provenance_id),
    )
    operator_pk = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO modules (server, game_id, operator_pk, provenance_id) VALUES (?,?,?,?)",
        ("en", "uniequip_001_amiya", operator_pk, provenance_id),
    )
    conn.execute(
        "INSERT INTO analysis_rules (rule_id, rule_version, domain, name) VALUES (?,?,?,?)",
        ("threat.aerial", "1", "stage", "Aerial threat"),
    )
    conn.execute(
        "INSERT INTO analysis_findings (entity_type, entity_pk, rule_id, confidence, "
        "analyzer_version, created_at) VALUES (?,?,?,?,?,?)",
        ("stage", 1, "threat.aerial", 0.8, "1", "2026-07-17"),
    )
    conn.commit()
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
