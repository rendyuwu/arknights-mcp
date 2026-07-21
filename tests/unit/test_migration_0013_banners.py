"""T112: migration 0013 banner-archive domain schema (§V17/§V62).

``banners`` + ``banner_featured_ops`` back the v0.2 M11 banner ARCHIVE, a historical
gacha-schedule FACT from the primary ``arknights_assets_gamedata`` snapshot (§V62 --
NOT a new source). These tests assert the migration applies cleanly, that both tables
carry the right provenance/FK wiring (§V17), that the ``banners`` column set is
METADATA-ONLY -- there is no place to store gacha prose/summary/detail/html/image
(§V62/§V16 ceiling) -- that ``region`` is NOT NULL (§V5), that the typed featured-op
child soft-resolves (``operator_pk`` nullable, §V62), and that identity/uniqueness
collide as the importer (T113) needs for its §V33 typed-error mapping. Schema only:
the importer (T113) and tool (T114) land separately.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.migrations import build_database

_FIELD_POLICY_VERSION = "test"
_TRANSFORM_VERSION = "test"

#: The complete, metadata-only column set for ``banners`` (§V62). banner_pk +
#: provenance_id are bookkeeping; the rest are exactly the allowed schedule/identity
#: fields. A future summary/detail/html/image/prose column would break this equality
#: (§V16/§V56 ceiling extends to the banner domain).
_BANNER_COLUMNS = {
    "banner_pk",
    "server",
    "game_id",
    "display_name",
    "open_time",
    "end_time",
    "rule_type",
    "region",
    "provenance_id",
}

#: Columns that would smuggle in forbidden gacha prose/summary/detail/image (§V62/§V16).
_FORBIDDEN_SUBSTRINGS = ("summary", "detail", "html", "image", "prose", "body", "content", "desc")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _seed_provenance(conn: sqlite3.Connection) -> int:
    """Insert the minimal source/snapshot/provenance chain; return provenance_id.

    Reuses the primary ``arknights_assets_gamedata`` source -- the banner archive is
    the SAME snapshot as enemy/stage/operator (§V62), not a new registry entry.
    """
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) VALUES "
        "('arknights_assets_gamedata', 'Arknights game data', 'Kengxxiao', "
        "'https://github.com/', 'game_data_repository', '[\"en\",\"cn\"]', '0.0', "
        "'unlicensed_public_repository', 'reviewed', 'derived', "
        "'Game data (c) Hypergryph.', 1, '2026-07-21')"
    )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES "
        "('snap-en-gd', 'arknights_assets_gamedata', 'en', '2026-07-21T00:00:00+00:00', "
        "'h', 'active', ?)",
        (_FIELD_POLICY_VERSION,),
    )
    prov_id = conn.execute(
        "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
        "record_hash, transform_version, field_policy_version) VALUES "
        "('snap-en-gd', 'gamedata/excel/gacha_table.json', 'BOOTER_1', 'rh', ?, ?)",
        (_TRANSFORM_VERSION, _FIELD_POLICY_VERSION),
    ).lastrowid
    conn.commit()
    assert prov_id is not None
    return int(prov_id)


def _seed_operator(conn: sqlite3.Connection, prov_id: int, game_id: str = "char_002_amiya") -> int:
    op_pk = conn.execute(
        "INSERT INTO operators (server, game_id, display_name, provenance_id) "
        "VALUES ('en', ?, 'Amiya', ?)",
        (game_id, prov_id),
    ).lastrowid
    conn.commit()
    assert op_pk is not None
    return int(op_pk)


def test_banner_tables_exist(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        present = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "banners" in present
        assert "banner_featured_ops" in present
    finally:
        conn.close()


def test_integrity_and_foreign_key_checks_pass(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_banners_metadata_only_column_set(tmp_path: Path) -> None:
    # §V62/§V16: banners holds ONLY schedule/identity metadata -- no summary/detail/
    # html/image/prose column exists to store the forbidden gacha promotional copy.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        cols = _columns(conn, "banners")
        assert cols == _BANNER_COLUMNS
        for col in cols:
            assert not any(bad in col.lower() for bad in _FORBIDDEN_SUBSTRINGS)
    finally:
        conn.close()


def test_region_is_not_null(tmp_path: Path) -> None:
    # §V5: every banner is region-attributed; region cannot be NULL.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        region_col = next(
            row for row in conn.execute("PRAGMA table_info(banners)") if row[1] == "region"
        )
        assert region_col[3] == 1  # notnull flag
    finally:
        conn.close()


def test_banner_provenance_fk_present_and_enforced(tmp_path: Path) -> None:
    # §V17: a banner carries provenance; a dangling provenance_id is rejected.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        assert "provenance_id" in _columns(conn, "banners")
        fks = {row[2] for row in conn.execute("PRAGMA foreign_key_list(banners)")}
        assert fks == {"record_provenance"}
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO banners (server, game_id, region, provenance_id) "
                "VALUES ('en', 'BOOTER_1', 'en', 999999)"
            )
            conn.commit()
    finally:
        conn.close()


def test_server_open_index_exists(tmp_path: Path) -> None:
    # The (server, open_time) index serves the get_banners per-region schedule read (T114).
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(banners)")}
        assert "idx_banners_server_open" in indexes
        cols = [row[2] for row in conn.execute("PRAGMA index_info(idx_banners_server_open)")]
        assert cols == ["server", "open_time"]
    finally:
        conn.close()


def test_banner_metadata_roundtrips(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        conn.execute(
            "INSERT INTO banners (server, game_id, display_name, open_time, end_time, "
            "rule_type, region, provenance_id) VALUES ('en', 'BOOTER_1', 'Standard Banner', "
            "'2026-07-01T00:00:00+00:00', '2026-07-15T00:00:00+00:00', 'NORMAL', 'en', ?)",
            (prov,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT server, game_id, display_name, open_time, end_time, rule_type, region "
            "FROM banners"
        ).fetchone()
        assert row == (
            "en",
            "BOOTER_1",
            "Standard Banner",
            "2026-07-01T00:00:00+00:00",
            "2026-07-15T00:00:00+00:00",
            "NORMAL",
            "en",
        )
    finally:
        conn.close()


def test_unique_server_game_id(tmp_path: Path) -> None:
    # UNIQUE(server, game_id): a duplicate (server, gachaPoolId) collides so the
    # importer (T113) can map the anomaly to a typed ImporterError (§V33 pattern);
    # the same game_id in a DIFFERENT server is allowed (§V5 region separation).
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        insert = "INSERT INTO banners (server, game_id, region, provenance_id) VALUES (?, ?, ?, ?)"
        conn.execute(insert, ("en", "BOOTER_1", "en", prov))
        conn.execute(insert, ("cn", "BOOTER_1", "cn", prov))  # different server: allowed
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("en", "BOOTER_1", "en", prov))  # duplicate: collides
        conn.commit()
    finally:
        conn.close()


def test_featured_op_resolved_and_unresolved_roundtrip(tmp_path: Path) -> None:
    # §V62: featured-op SOFT-resolves. A present operator -> operator_pk set, resolved=1;
    # an absent operator (combat-only snapshot, B36) -> operator_pk NULL, resolved=0.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        op_pk = _seed_operator(conn, prov)
        banner_pk = conn.execute(
            "INSERT INTO banners (server, game_id, rule_type, region, provenance_id) "
            "VALUES ('en', 'LIMITED_1', 'LIMITED', 'en', ?)",
            (prov,),
        ).lastrowid
        conn.execute(
            "INSERT INTO banner_featured_ops (banner_pk, operator_pk, char_id, resolved) "
            "VALUES (?, ?, 'char_002_amiya', 1)",
            (banner_pk, op_pk),
        )
        conn.execute(
            "INSERT INTO banner_featured_ops (banner_pk, operator_pk, char_id, resolved) "
            "VALUES (?, NULL, 'char_999_ghost', 0)",
            (banner_pk,),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT operator_pk, char_id, resolved FROM banner_featured_ops ORDER BY char_id"
        ).fetchall()
        assert rows == [(op_pk, "char_002_amiya", 1), (None, "char_999_ghost", 0)]
    finally:
        conn.close()


def test_featured_op_fks_present_and_enforced(tmp_path: Path) -> None:
    # banner_pk -> banners, operator_pk -> operators (nullable soft-resolve, §V62).
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        fks = {row[2] for row in conn.execute("PRAGMA foreign_key_list(banner_featured_ops)")}
        assert fks == {"banners", "operators"}
        prov = _seed_provenance(conn)
        # A dangling operator_pk (non-NULL, no such operator) is rejected.
        banner_pk = conn.execute(
            "INSERT INTO banners (server, game_id, region, provenance_id) "
            "VALUES ('en', 'LIMITED_1', 'en', ?)",
            (prov,),
        ).lastrowid
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO banner_featured_ops (banner_pk, operator_pk, char_id, resolved) "
                "VALUES (?, 999999, 'char_x', 1)",
                (banner_pk,),
            )
            conn.commit()
    finally:
        conn.close()


def test_unique_banner_char_id(tmp_path: Path) -> None:
    # UNIQUE(banner_pk, char_id): a banner lists each featured op once; a duplicate
    # char_id on the same banner collides so the importer maps it to ImporterError (§V33).
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        banner_pk = conn.execute(
            "INSERT INTO banners (server, game_id, rule_type, region, provenance_id) "
            "VALUES ('en', 'CLASSIC_1', 'CLASSIC', 'en', ?)",
            (prov,),
        ).lastrowid
        insert = (
            "INSERT INTO banner_featured_ops (banner_pk, operator_pk, char_id, resolved) "
            "VALUES (?, NULL, ?, 0)"
        )
        conn.execute(insert, (banner_pk, "char_a"))
        conn.execute(insert, (banner_pk, "char_b"))  # different char: allowed
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (banner_pk, "char_a"))  # duplicate on same banner: collides
        conn.commit()
    finally:
        conn.close()


def test_resolved_flag_check_constraint(tmp_path: Path) -> None:
    # resolved is a 0/1 flag (CHECK), mirroring operators.obtainable.
    conn = build_database(tmp_path / "cand.sqlite")
    try:
        prov = _seed_provenance(conn)
        banner_pk = conn.execute(
            "INSERT INTO banners (server, game_id, region, provenance_id) "
            "VALUES ('en', 'B1', 'en', ?)",
            (prov,),
        ).lastrowid
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO banner_featured_ops (banner_pk, operator_pk, char_id, resolved) "
                "VALUES (?, NULL, 'char_x', 2)",  # out of {0,1}
                (banner_pk,),
            )
            conn.commit()
    finally:
        conn.close()
