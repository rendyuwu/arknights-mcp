"""T182: migration 0014 skin-gallery domain schema (§V17/§V88; ADR 0015).

``operator_skins`` backs the named skin gallery, metadata from the primary
``arknights_assets_gamedata`` snapshot's ``skin_table.json`` (NOT a new source).
These tests assert the migration applies cleanly, that the table carries the right
provenance/FK wiring (§V17), that the column set is METADATA-ONLY -- there is no
place to store displaySkin prose/credit (§V18/§V16 ceiling) and no URL column
(§V63 derive-not-store) -- that ``region`` is NOT NULL (§V5), that ``operator_pk``
soft-resolves (nullable, B36 class), that identity collides as the importer needs
for its §V33 typed-error mapping, and that the domain stays OUT of the §V4
CRITICAL_TABLES (optional, fail-open like banners). Schema only: the importer and
tool wiring are tested separately.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.migrations import build_database
from arknights_mcp.db.validate import CRITICAL_TABLES

_FIELD_POLICY_VERSION = "test"
_TRANSFORM_VERSION = "test"

#: The complete, metadata-only column set for ``operator_skins`` (ADR 0015). skin_pk +
#: provenance_id are bookkeeping; the rest are exactly the allowed identity/label
#: fields. A future prose/url/bytes column would break this equality (§V16/§V18/§V63).
_SKIN_COLUMNS = {
    "skin_pk",
    "server",
    "skin_id",
    "char_id",
    "tmpl_id",
    "operator_pk",
    "resolved",
    "display_name",
    "skin_group_id",
    "skin_group_name",
    "portrait_id",
    "is_buy_skin",
    "region",
    "provenance_id",
}

#: Columns that would smuggle in forbidden outfit prose/credit/art (§V18/§V16/§V63).
_FORBIDDEN_SUBSTRINGS = ("content", "dialog", "usage", "desc", "drawer", "html", "url", "image")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _seed_provenance(conn: sqlite3.Connection) -> int:
    """Insert the minimal source/snapshot/provenance chain; return provenance_id."""
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) VALUES "
        "('arknights_assets_gamedata', 'Arknights game data', 'ArknightsAssets', "
        "'https://github.com/', 'game_data_repository', '[\"en\",\"cn\"]', '0.0', "
        "'unlicensed_public_repository', 'reviewed', 'derived', "
        "'Game data (c) Hypergryph.', 1, '2026-07-26')"
    )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES "
        "('snap-en-gd', 'arknights_assets_gamedata', 'en', '2026-07-26T00:00:00+00:00', "
        "'h', 'active', ?)",
        (_FIELD_POLICY_VERSION,),
    )
    prov_id = conn.execute(
        "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
        "record_hash, transform_version, field_policy_version) VALUES "
        "('snap-en-gd', 'gamedata/excel/skin_table.json', 'char_002_amiya#1', 'rh', ?, ?)",
        (_TRANSFORM_VERSION, _FIELD_POLICY_VERSION),
    ).lastrowid
    conn.commit()
    assert prov_id is not None
    return int(prov_id)


def _insert_skin(
    conn: sqlite3.Connection,
    prov_id: int,
    *,
    skin_id: str = "char_002_amiya#1",
    server: str = "en",
) -> None:
    conn.execute(
        "INSERT INTO operator_skins (server, skin_id, char_id, operator_pk, resolved, "
        "portrait_id, region, provenance_id) VALUES (?, ?, 'char_002_amiya', NULL, 0, "
        "'char_002_amiya_1', ?, ?)",
        (server, skin_id, server, prov_id),
    )


def test_table_and_indexes_present(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "operator_skins" in tables
        indexes = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "idx_operator_skins_operator" in indexes
        assert "idx_operator_skins_char" in indexes
    finally:
        conn.close()


def test_column_set_is_metadata_only(tmp_path: Path) -> None:
    # §V18/§V16/§V63 ceiling: the exact column set -- no prose, no url, no bytes home.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        cols = _columns(conn, "operator_skins")
        assert cols == _SKIN_COLUMNS
        for col in cols:
            for forbidden in _FORBIDDEN_SUBSTRINGS:
                assert forbidden not in col.lower(), f"column {col!r} smells of prose/art"
    finally:
        conn.close()


def test_operator_skins_not_in_critical_tables() -> None:
    # §V4: the skin domain is optional + fail-open (like banners); an empty gallery is
    # a legitimate build, so validation must not require rows here.
    assert "operator_skins" not in CRITICAL_TABLES


def test_soft_resolve_operator_pk_nullable(tmp_path: Path) -> None:
    # B36 class: a combat-only snapshot has no operators; the skin row still inserts
    # with operator_pk NULL + resolved = 0.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        _insert_skin(conn, prov)
        conn.commit()
        assert conn.execute("SELECT operator_pk, resolved FROM operator_skins").fetchone() == (
            None,
            0,
        )
    finally:
        conn.close()


def test_duplicate_skin_id_collides(tmp_path: Path) -> None:
    # §V33 substrate: UNIQUE(server, skin_id) collides so the importer can map the
    # anomaly to a typed ImporterError.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        _insert_skin(conn, prov)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            _insert_skin(conn, prov)
    finally:
        conn.close()


def test_same_skin_id_across_servers_allowed(tmp_path: Path) -> None:
    # Identity is (server, skin_id): the same skin id may exist for en AND cn (§V5).
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        _insert_skin(conn, prov, server="en")
        _insert_skin(conn, prov, server="cn")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM operator_skins").fetchone()[0] == 2
    finally:
        conn.close()


def test_region_and_portrait_id_not_null(tmp_path: Path) -> None:
    # §V5: region NOT NULL; §V63: portrait_id is the sole URL input, NOT NULL.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO operator_skins (server, skin_id, char_id, portrait_id, "
                "region, provenance_id) VALUES ('en', 's1', 'c1', NULL, 'en', ?)",
                (prov,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO operator_skins (server, skin_id, char_id, portrait_id, "
                "region, provenance_id) VALUES ('en', 's2', 'c1', 'p1', NULL, ?)",
                (prov,),
            )
    finally:
        conn.close()


def test_provenance_fk_enforced(tmp_path: Path) -> None:
    # §V17: every skin row must trace to a record_provenance row.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO operator_skins (server, skin_id, char_id, portrait_id, "
                "region, provenance_id) VALUES ('en', 's1', 'c1', 'p1', 'en', 999999)"
            )
    finally:
        conn.close()
