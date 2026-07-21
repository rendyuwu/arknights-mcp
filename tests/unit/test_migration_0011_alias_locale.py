"""T98: migration 0011 alias `locale` tag (§V17/§V57).

Adds a ``locale`` column (+ index) to the two near-identical alias tables so each
stored alias carries the language-locale of its string, unblocking the v0.2
extra-locale alias work (T99/T100). These tests assert the migration applies cleanly
on both ``operator_aliases`` and ``enemy_aliases``, that the index exists, that
``integrity_check`` + ``foreign_key_check`` still pass, and that the SQL backfill tags
a pre-0011 alias with its region's locale (en->``en``, cn->``zh``) via the same
cn->zh coupling the importer uses (``REGION_TO_NAME_LOCALE``, §V37).

The fresh-build locale stamp is done by the importer (tested in
``test_import_operators``); this migration's UPDATE only matters for a populated /
re-run DB, so the backfill test seeds rows and runs the migration's own UPDATE
statements against them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from arknights_mcp.db.migrations import build_database, default_migrations_dir


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def _backfill_statements() -> list[str]:
    """The migration's own UPDATE statements, so the test binds to the real SQL."""
    sql = (default_migrations_dir() / "0011_alias_locale.sql").read_text(encoding="utf-8")
    return [s.strip() for s in sql.split(";") if s.strip().upper().startswith("UPDATE")]


def _seed_provenance(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) "
        "VALUES ('local_snapshot','Local','op','local://x','t','[\"en\",\"cn\"]','1','l','p','r',"
        "'a',1,'2026-07-21')"
    )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES "
        "('snap','local_snapshot','en','2026-07-21T00:00:00+00:00','mh','imported','1')"
    )
    prov = conn.execute(
        "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
        "record_hash, transform_version, field_policy_version) VALUES "
        "('snap','p','k','rh','1','1')"
    ).lastrowid
    conn.commit()
    assert prov is not None
    return int(prov)


def test_locale_column_present_on_both_alias_tables(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        assert "locale" in _columns(conn, "operator_aliases")
        assert "locale" in _columns(conn, "enemy_aliases")
    finally:
        conn.close()


def test_locale_indexes_present(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        assert "idx_operator_aliases_locale" in _indexes(conn, "operator_aliases")
        assert "idx_enemy_aliases_locale" in _indexes(conn, "enemy_aliases")
    finally:
        conn.close()


def test_integrity_and_foreign_key_checks_pass(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_backfill_tags_existing_operator_aliases_by_region(tmp_path: Path) -> None:
    # §V57: a pre-0011 alias with a NULL locale is tagged from its operator's region
    # (en->en, cn->zh). The migration's own UPDATE is exercised so a code/SQL drift
    # is caught. On a real fresh build this matches nothing (importer stamps instead).
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        for pk, (server, game_id) in enumerate([("en", "char_en"), ("cn", "char_cn")], start=1):
            conn.execute(
                "INSERT INTO operators (operator_pk, server, game_id, provenance_id) "
                "VALUES (?,?,?,?)",
                (pk, server, game_id, prov),
            )
            conn.execute(
                "INSERT INTO operator_aliases (operator_pk, alias, normalized_alias, "
                "alias_type, locale) VALUES (?,?,?,?,NULL)",
                (pk, f"name_{server}", f"name_{server}", "name"),
            )
        conn.commit()

        for stmt in _backfill_statements():
            conn.execute(stmt)
        conn.commit()

        locales = dict(
            conn.execute(
                "SELECT o.server, a.locale FROM operator_aliases a "
                "JOIN operators o ON o.operator_pk = a.operator_pk"
            )
        )
        assert locales == {"en": "en", "cn": "zh"}
    finally:
        conn.close()


def test_backfill_tags_existing_enemy_aliases_by_region(tmp_path: Path) -> None:
    # §V57 symmetric path: enemy_aliases (kept symmetric with operator_aliases per
    # §V37) is tagged the same way, even though the enemy importer does not currently
    # populate aliases -- the column + backfill must still be correct for T99/T100.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        for pk, server in enumerate(["en", "cn"], start=1):
            conn.execute(
                "INSERT INTO enemies (enemy_pk, server, game_id, provenance_id) VALUES (?,?,?,?)",
                (pk, server, f"enemy_{server}", prov),
            )
            conn.execute(
                "INSERT INTO enemy_aliases (enemy_pk, alias, normalized_alias, alias_type, "
                "locale) VALUES (?,?,?,?,NULL)",
                (pk, f"e_{server}", f"e_{server}", "name"),
            )
        conn.commit()

        for stmt in _backfill_statements():
            conn.execute(stmt)
        conn.commit()

        locales = dict(
            conn.execute(
                "SELECT e.server, a.locale FROM enemy_aliases a "
                "JOIN enemies e ON e.enemy_pk = a.enemy_pk"
            )
        )
        assert locales == {"en": "en", "cn": "zh"}
    finally:
        conn.close()
