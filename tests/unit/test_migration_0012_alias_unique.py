"""T109: migration 0012 alias uniqueness for idempotent locale-alias re-import (§V57).

Adds a ``UNIQUE(entity_pk, alias, locale)`` index to each of the two alias tables so
the extra-locale ride-along's ``INSERT OR IGNORE`` (T109) is idempotent: a re-run /
backfill of the same jp/kr NAME must not double-insert a row (which would then surface
twice in the FTS ``GROUP_CONCAT``, §V37/B22). These tests assert the indexes exist,
that ``integrity_check`` + ``foreign_key_check`` still pass, that a duplicate
``(pk, alias, locale)`` collides on a plain INSERT but is silently suppressed by
``INSERT OR IGNORE``, and that the uniqueness key is (pk, alias, locale) -- so the SAME
string under two DIFFERENT locale tags is two distinct rows (an entity's own-region name
plus a matching extra-locale alias must coexist).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.migrations import build_database


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


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


def _seed_enemy(conn: sqlite3.Connection, prov: int) -> int:
    pk = conn.execute(
        "INSERT INTO enemies (server, game_id, provenance_id) VALUES ('en','enemy_x',?)",
        (prov,),
    ).lastrowid
    conn.commit()
    assert pk is not None
    return int(pk)


def _seed_operator(conn: sqlite3.Connection, prov: int) -> int:
    pk = conn.execute(
        "INSERT INTO operators (server, game_id, provenance_id) VALUES ('en','char_x',?)",
        (prov,),
    ).lastrowid
    conn.commit()
    assert pk is not None
    return int(pk)


def _insert_enemy_alias(
    conn: sqlite3.Connection, enemy_pk: int, alias: str, locale: str, *, or_ignore: bool = False
) -> None:
    verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
    conn.execute(
        f"{verb} INTO enemy_aliases "
        "(enemy_pk, alias, language, normalized_alias, alias_type, locale) "
        "VALUES (?,?,?,?,?,?)",
        (enemy_pk, alias, None, alias.casefold(), "locale_name", locale),
    )


def test_unique_indexes_present_on_both_alias_tables(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        assert "idx_operator_aliases_unique" in _indexes(conn, "operator_aliases")
        assert "idx_enemy_aliases_unique" in _indexes(conn, "enemy_aliases")
    finally:
        conn.close()


def test_integrity_and_foreign_key_checks_pass(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_duplicate_alias_row_collides_on_plain_insert(tmp_path: Path) -> None:
    # A second (enemy_pk, alias, locale) row on a bare INSERT hits the new UNIQUE index
    # -- proving the constraint the OR IGNORE idempotency relies on actually exists.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        enemy_pk = _seed_enemy(conn, prov)
        _insert_enemy_alias(conn, enemy_pk, "ドローン", "ja")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_enemy_alias(conn, enemy_pk, "ドローン", "ja")
    finally:
        conn.close()


def test_insert_or_ignore_suppresses_duplicate_alias_row(tmp_path: Path) -> None:
    # §T109 idempotency: re-inserting the same (enemy_pk, alias, locale) via OR IGNORE is
    # a no-op -- exactly one row survives, so a re-run/backfill never doubles the FTS token.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        enemy_pk = _seed_enemy(conn, prov)
        _insert_enemy_alias(conn, enemy_pk, "ドローン", "ja", or_ignore=True)
        _insert_enemy_alias(conn, enemy_pk, "ドローン", "ja", or_ignore=True)
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM enemy_aliases WHERE enemy_pk=? AND alias=? AND locale=?",
            (enemy_pk, "ドローン", "ja"),
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_same_alias_different_locale_is_two_rows(tmp_path: Path) -> None:
    # The uniqueness key is (pk, alias, locale), NOT (pk, alias): the SAME string tagged
    # with two different locales is two distinct rows. An entity's own-region canonical
    # name (locale en/zh) must be able to coexist with a matching extra-locale alias.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        enemy_pk = _seed_enemy(conn, prov)
        _insert_enemy_alias(conn, enemy_pk, "Originium Slug", "en")
        _insert_enemy_alias(conn, enemy_pk, "Originium Slug", "ko")  # must NOT collide
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM enemy_aliases WHERE enemy_pk=? AND alias=?",
            (enemy_pk, "Originium Slug"),
        ).fetchone()[0]
        assert count == 2
    finally:
        conn.close()


def test_operator_self_alias_pair_unaffected(tmp_path: Path) -> None:
    # The operator self-alias importer (plain INSERT) writes name + appellation, both
    # tagged the same region locale but with DISTINCT alias strings -- so the new index
    # never trips it. Guards the migration against breaking the fresh-build operator path.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        op_pk = _seed_operator(conn, prov)
        for alias, alias_type in (("Amiya", "name"), ("Rhodes Island Leader", "appellation")):
            conn.execute(
                "INSERT INTO operator_aliases "
                "(operator_pk, alias, language, normalized_alias, alias_type, locale) "
                "VALUES (?,?,?,?,?,?)",
                (op_pk, alias, None, alias.casefold(), alias_type, "en"),
            )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM operator_aliases WHERE operator_pk=?", (op_pk,)
        ).fetchone()[0]
        assert count == 2
    finally:
        conn.close()
