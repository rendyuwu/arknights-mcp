"""T113: banner-archive importer (gacha_table.json -> banners + banner_featured_ops).

Parses the primary ``gacha_table`` ``gachaPoolClient`` into the metadata-only banner
archive (§V62): typed schedule/identity fields only (no gacha prose, §V16/§V18), unix
epochs normalized to ISO, typed featured ops per rule type (LIMITED single /
CLASSIC-family array / NORMAL-family none), soft-resolved to an ``operator_pk`` when the
operator is present else the raw char id with ``resolved = 0`` (§V62 — an unresolvable
featured-op never fails the build, §V3). A non-empty pool list yielding zero banners
fails closed (§V30); an absent gacha_table is a legitimate empty build (B36). A duplicate
pool id maps to a typed ImporterError (§V33). Purge cascades the banner rows (§V32).
"""

from __future__ import annotations

import json as _json
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.migrations import build_database
from arknights_mcp.db.purge import _purge_source_rows
from arknights_mcp.importers.banners import (
    ParsedBanner,
    import_banners,
    insert_banners,
    parse_banners,
)
from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

_SOURCE_ID = "local_snapshot"

# Prose that must never survive the metadata-only allowlist (§V16/§V62).
_PROSE = "gacha promotional copy that must never be imported into the database"

_LIMITED = {
    "gachaPoolId": "LIMITED_1",
    "gachaPoolName": "Limited Headhunting",
    "openTime": 1700000000,
    "endTime": 1701209600,
    "gachaRuleType": "LIMITED",
    "gachaPoolSummary": _PROSE,
    "gachaPoolDetail": _PROSE,
    "limitParam": {"limitedCharId": "char_002_amiya", "freeCount": 0},
}
_CLASSIC = {
    "gachaPoolId": "CLASSIC_1",
    "gachaPoolName": "Classic Headhunting",
    "openTime": 1702000000,
    "endTime": 1703209600,
    "gachaRuleType": "CLASSIC",
    "dynMeta": {
        "attainRare6CharList": ["char_002_amiya", "char_999_ghost"],
        "rateUpHtml": "<@ga.up>" + _PROSE + "</>",
    },
}
_NORMAL = {
    "gachaPoolId": "NORMAL_1",
    "gachaPoolName": "Standard Headhunting",
    "openTime": 1704000000,
    "endTime": 1705209600,
    "gachaRuleType": "NORMAL",
}


def _gacha(*pools: dict) -> dict:
    return {"gachaPoolClient": list(pools)}


# --- pure parsing (no DB) ----------------------------------------------------


def test_limited_reads_limited_char_id() -> None:
    # §V62: a LIMITED banner names one featured op under limitParam.limitedCharId.
    [banner] = parse_banners(_gacha(_LIMITED))
    assert banner.game_id == "LIMITED_1"
    assert banner.rule_type == "LIMITED"
    assert banner.featured_char_ids == ["char_002_amiya"]


def test_classic_family_reads_attain_rare6_list() -> None:
    # §V62: a CLASSIC-family banner names an array under dynMeta.attainRare6CharList.
    [banner] = parse_banners(_gacha(_CLASSIC))
    assert banner.featured_char_ids == ["char_002_amiya", "char_999_ghost"]


@pytest.mark.parametrize(
    "rule_type", ["ATTAIN", "CLASSIC", "CLASSIC_ATTAIN", "CLASSIC_DOUBLE", "FESCLASSIC", "SPECIAL"]
)
def test_every_classic_family_rule_type_reads_the_array(rule_type: str) -> None:
    # §V62: the full CLASSIC-family set resolves featured ops from dynMeta.
    pool = {**_CLASSIC, "gachaRuleType": rule_type}
    [banner] = parse_banners(_gacha(pool))
    assert banner.featured_char_ids == ["char_002_amiya", "char_999_ghost"]


@pytest.mark.parametrize("rule_type", ["NORMAL", "SINGLE", "DOUBLE", "LINKAGE"])
def test_standard_banner_carries_no_typed_featured_op(rule_type: str) -> None:
    # §V62: NORMAL/SINGLE/DOUBLE/LINKAGE carry no typed featured-op (rate-up is prose
    # only, §V18-forbidden); the read tool surfaces that as a limitation (§V26/T114).
    pool = {**_CLASSIC, "gachaRuleType": rule_type}
    [banner] = parse_banners(_gacha(pool))
    assert banner.featured_char_ids == []


def test_epoch_times_normalized_to_iso() -> None:
    # §V62: unix-epoch openTime/endTime are normalized to ISO UTC timestamps.
    [banner] = parse_banners(_gacha(_LIMITED))
    assert banner.open_time == "2023-11-14T22:13:20+00:00"
    assert banner.end_time == "2023-11-28T22:13:20+00:00"  # +14 days exactly


def test_prose_is_never_kept() -> None:
    # §V16/§V18: gacha summary/detail/html prose never survives the allowlist, so it
    # appears in no provenance record (the only place a raw fragment could ride in).
    all_blob = _json.dumps([b.provenance_record for b in parse_banners(_gacha(_LIMITED, _CLASSIC))])
    assert _PROSE not in all_blob
    assert "gachaPoolSummary" not in all_blob
    assert "gachaPoolDetail" not in all_blob
    assert "rateUpHtml" not in all_blob


def test_entry_missing_pool_id_is_skipped() -> None:
    # A pool entry with no gachaPoolId is skipped, never fabricated (fail-closed).
    no_id = {"gachaPoolName": "Nameless", "gachaRuleType": "NORMAL"}
    assert parse_banners(_gacha(no_id, _NORMAL)) == parse_banners(_gacha(_NORMAL))


def test_entry_with_blank_pool_id_is_skipped() -> None:
    # A blank (empty-string) gachaPoolId is treated like a missing one: skipped, never a
    # fabricated row with an empty game_id (which would also collide on UNIQUE / stamp a
    # keyless provenance record).
    blank_id = {"gachaPoolId": "", "gachaPoolName": "Blank", "gachaRuleType": "NORMAL"}
    assert parse_banners(_gacha(blank_id, _NORMAL)) == parse_banners(_gacha(_NORMAL))


def test_invalid_epoch_yields_none_not_fabricated() -> None:
    # §V26: a non-int / out-of-range epoch yields None, never a fabricated timestamp.
    pool = {**_NORMAL, "openTime": "not-an-epoch", "endTime": None}
    [banner] = parse_banners(_gacha(pool))
    assert banner.open_time is None
    assert banner.end_time is None


def test_gacha_table_without_pool_list_parses_empty() -> None:
    # A present-but-shapeless gacha_table (no gachaPoolClient list) yields no banners.
    assert parse_banners({"gachaPoolClient": {}}) == []
    assert parse_banners({}) == []


def test_non_object_gacha_table_raises() -> None:
    with pytest.raises(ImporterError, match="not a JSON object"):
        parse_banners([1, 2, 3])


# --- DB insertion + soft-resolve ---------------------------------------------


def _conn_with_operator(tmp_path: Path, *, seed_operator: bool) -> tuple[sqlite3.Connection, str]:
    """Build a candidate; seed the primary source snapshot and optionally an operator.

    Returns the connection and the snapshot_id to import banners under.
    """
    conn = build_database(tmp_path / "cand.sqlite")
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) VALUES "
        "(?, 'gd', 'o', 'https://x/', 'game_data_repository', '[\"en\"]', '0', 'reviewed', "
        "'reviewed', 'derived', 'a', 1, '2026-07-21')",
        (_SOURCE_ID,),
    )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES "
        "('snap-en', ?, 'en', '2026-07-21T00:00:00+00:00', 'h', 'active', 'test')",
        (_SOURCE_ID,),
    )
    if seed_operator:
        prov = conn.execute(
            "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
            "record_hash, transform_version, field_policy_version) VALUES "
            "('snap-en', 'gamedata/excel/character_table.json', 'char_002_amiya', 'rh', "
            "'test', 'test')"
        ).lastrowid
        conn.execute(
            "INSERT INTO operators (server, game_id, display_name, provenance_id) "
            "VALUES ('en', 'char_002_amiya', 'Amiya', ?)",
            (prov,),
        )
    conn.commit()
    return conn, "snap-en"


def test_featured_op_soft_resolves_present_operator(tmp_path: Path) -> None:
    # §V62: a featured char present as an operator resolves to operator_pk, resolved=1.
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        result = insert_banners(
            conn,
            parse_banners(_gacha(_LIMITED)),
            server="en",
            snapshot_id=snap,
            source_path="gamedata/excel/gacha_table.json",
        )
        assert result.banners_inserted == 1
        assert result.featured_ops_resolved == 1
        row = conn.execute(
            "SELECT operator_pk, char_id, resolved FROM banner_featured_ops"
        ).fetchone()
        assert row[0] is not None and row[1] == "char_002_amiya" and row[2] == 1
    finally:
        conn.close()


def test_featured_op_stays_raw_when_operator_absent(tmp_path: Path) -> None:
    # §V62/B36: a combat-only snapshot (no operators) keeps the raw char id, resolved=0;
    # the unresolvable featured-op never fails the build (§V3).
    conn, snap = _conn_with_operator(tmp_path, seed_operator=False)
    try:
        result = insert_banners(
            conn,
            parse_banners(_gacha(_CLASSIC)),
            server="en",
            snapshot_id=snap,
            source_path="gamedata/excel/gacha_table.json",
        )
        assert result.banners_inserted == 1
        assert result.featured_ops_resolved == 0
        rows = conn.execute(
            "SELECT operator_pk, char_id, resolved FROM banner_featured_ops ORDER BY char_id"
        ).fetchall()
        assert rows == [(None, "char_002_amiya", 0), (None, "char_999_ghost", 0)]
    finally:
        conn.close()


def test_region_equals_server(tmp_path: Path) -> None:
    # §V5: region is the fact region; en and cn are never mixed.
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        insert_banners(
            conn,
            parse_banners(_gacha(_LIMITED)),
            server="en",
            snapshot_id=snap,
            source_path="gamedata/excel/gacha_table.json",
        )
        assert conn.execute("SELECT DISTINCT region FROM banners").fetchall() == [("en",)]
    finally:
        conn.close()


def test_duplicate_pool_id_fails_closed(tmp_path: Path) -> None:
    # §V33: a duplicate gachaPoolId collides on UNIQUE(server, game_id); the anomaly
    # maps to a typed ImporterError, not an uncaught IntegrityError.
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        dup = [ParsedBanner("DUP", None, None, None, "NORMAL", [], {}) for _ in range(2)]
        with pytest.raises(ImporterError, match="UNIQUE constraint"):
            insert_banners(conn, dup, server="en", snapshot_id=snap, source_path="gacha_table.json")
    finally:
        conn.close()


# --- adapter-driven import (tolerant-absent + §V30 guard) --------------------


def _adapter_with_gacha(tmp_path: Path, payload: object | None) -> LocalSnapshotAdapter:
    root = tmp_path / "snap"
    excel = root / "gamedata" / "excel"
    excel.mkdir(parents=True)
    if payload is not None:
        (excel / "gacha_table.json").write_text(_json.dumps(payload), encoding="utf-8")
    return LocalSnapshotAdapter(root, "en")


def test_import_tolerates_absent_gacha_table(tmp_path: Path) -> None:
    # B36/§V41: a snapshot without gacha_table.json imports zero banners, not a failure.
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        adapter = _adapter_with_gacha(tmp_path, None)
        result = import_banners(conn, adapter, snap)
        assert result.banners_inserted == 0
    finally:
        conn.close()


def test_import_end_to_end(tmp_path: Path) -> None:
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        adapter = _adapter_with_gacha(tmp_path, _gacha(_LIMITED, _CLASSIC, _NORMAL))
        result = import_banners(conn, adapter, snap)
        assert result.banners_inserted == 3
        # LIMITED (char_002 present) resolves; CLASSIC has one present + one ghost.
        assert result.featured_ops_resolved == 2
        conn.commit()
    finally:
        conn.close()


def test_non_empty_pool_yielding_zero_banners_fails_closed(tmp_path: Path) -> None:
    # §V30: a non-empty gachaPoolClient whose entries all lack a gachaPoolId resolves to
    # zero banners -> fail closed (a shape/id mismatch is never a silent empty build).
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        idless = {"gachaPoolClient": [{"gachaRuleType": "NORMAL"}, {"gachaPoolName": "x"}]}
        adapter = _adapter_with_gacha(tmp_path, idless)
        with pytest.raises(ImporterError, match="silent empty banner build"):
            import_banners(conn, adapter, snap)
    finally:
        conn.close()


def test_empty_pool_list_is_legitimate(tmp_path: Path) -> None:
    # An empty gachaPoolClient (no candidate entries) imports zero without error.
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        adapter = _adapter_with_gacha(tmp_path, {"gachaPoolClient": []})
        assert import_banners(conn, adapter, snap).banners_inserted == 0
    finally:
        conn.close()


# --- purge cascade -----------------------------------------------------------


def test_purge_cascades_banner_rows(tmp_path: Path) -> None:
    # §V32: purging the source removes its banners + banner_featured_ops, leaving no
    # dangling foreign key (children-before-parents).
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        adapter = _adapter_with_gacha(tmp_path, _gacha(_LIMITED, _CLASSIC))
        import_banners(conn, adapter, snap)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM banners").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM banner_featured_ops").fetchone()[0] == 3
        _purge_source_rows(conn, _SOURCE_ID)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM banners").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM banner_featured_ops").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
