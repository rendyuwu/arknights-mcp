"""T13: enemy parser -> enemies + enemy_levels, with field allowlist + string
sanitization (§V18) and per-record provenance (§V17).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.enemies import (
    ImporterError,
    import_enemies,
    insert_enemies,
    parse_enemies,
)
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

DESCRIPTION_PROSE = "A long lore blurb that must never be imported into the database."

HANDBOOK = {
    "enemyData": {
        "enemy_1007_slime": {
            "enemyId": "enemy_1007_slime",
            "name": "Originium Slug",
            "enemyLevel": "NORMAL",
            "attackType": "physical",
            "motionType": "WALK",
            "description": DESCRIPTION_PROSE,
        },
        "enemy_1105_drone": {
            "enemyId": "enemy_1105_drone",
            "name": "Recon Drone\x00\t",  # control chars to be sanitized
            "enemyLevel": "ELITE",
            "attackType": "physical",
            "motionType": "FLY",
            "description": DESCRIPTION_PROSE,
        },
    }
}

DATABASE = {
    "enemies": {
        "enemy_1007_slime": {
            "levels": [
                {
                    "level": 0,
                    "hp": 1650,
                    "atk": 320,
                    "def": 100,
                    "res": 0,
                    "attackInterval": 2.0,
                    "moveSpeed": 0.7,
                    "weight": 2,
                    "lifePointReduction": 1,
                    "blockBehavior": "blockable",
                    "abilities": [],
                }
            ]
        },
        "enemy_1105_drone": {
            "levels": [
                {
                    "level": 0,
                    "hp": 900,
                    "atk": 260,
                    "def": 0,
                    "res": 10,
                    "attackInterval": 1.6,
                    "attackRange": 1.2,
                    "moveSpeed": 1.0,
                    "weight": 1,
                    "lifePointReduction": 1,
                    "blockBehavior": "unblockable_flying",
                    "abilities": ["aerial"],
                }
            ]
        },
    }
}


def _seed_snapshot(conn: sqlite3.Connection, snapshot_id: str = "en:test000000") -> str:
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "local_snapshot",
            "Local",
            "op",
            "local://x",
            "t",
            '["en"]',
            "1",
            "l",
            "p",
            "r",
            "a",
            1,
            "2026-07-17",
        ),
    )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES (?,?,?,?,?,?,?)",
        (snapshot_id, "local_snapshot", "en", "2026-07-17T00:00:00+00:00", "mh", "imported", "1"),
    )
    conn.commit()
    return snapshot_id


def test_parse_drops_prose_and_reads_typed_fields() -> None:
    parsed = {e.game_id: e for e in parse_enemies(HANDBOOK, DATABASE)}
    drone = parsed["enemy_1105_drone"]
    assert drone.motion_type == "FLY"
    assert drone.is_elite is True
    assert drone.levels[0].hp == 900
    # V18: prose field never survives parsing.
    assert "description" not in drone.provenance_record["handbook"]
    assert DESCRIPTION_PROSE not in str(drone.provenance_record)


def test_insert_enemies_and_levels(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    snapshot_id = _seed_snapshot(conn)
    result = insert_enemies(
        conn,
        parse_enemies(HANDBOOK, DATABASE),
        server="en",
        snapshot_id=snapshot_id,
        handbook_source_path="gamedata/excel/enemy_handbook_table.json",
    )
    conn.commit()
    assert result.enemies_inserted == 2
    assert result.levels_inserted == 2
    rows = conn.execute(
        "SELECT game_id, motion_type, is_elite, provenance_id FROM enemies ORDER BY game_id"
    ).fetchall()
    assert rows[0][0] == "enemy_1007_slime"
    drone = conn.execute(
        "SELECT motion_type, is_elite, provenance_id FROM enemies WHERE game_id='enemy_1105_drone'"
    ).fetchone()
    assert drone[0] == "FLY"
    assert drone[1] == 1
    assert drone[2] is not None  # V17: provenance attached


def test_name_is_sanitized(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    snapshot_id = _seed_snapshot(conn)
    insert_enemies(
        conn,
        parse_enemies(HANDBOOK, DATABASE),
        server="en",
        snapshot_id=snapshot_id,
        handbook_source_path="gamedata/excel/enemy_handbook_table.json",
    )
    conn.commit()
    name = conn.execute(
        "SELECT display_name FROM enemies WHERE game_id='enemy_1105_drone'"
    ).fetchone()[0]
    assert name == "Recon Drone"  # control chars stripped + trimmed


def test_no_prose_anywhere_in_db(tmp_path: Path) -> None:
    conn = build_database(tmp_path / "cand.sqlite")
    snapshot_id = _seed_snapshot(conn)
    insert_enemies(
        conn,
        parse_enemies(HANDBOOK, DATABASE),
        server="en",
        snapshot_id=snapshot_id,
        handbook_source_path="gamedata/excel/enemy_handbook_table.json",
    )
    conn.commit()
    # V18: the excluded prose must not appear in any text column of the DB.
    dump = "\n".join(
        str(row)
        for table in ("enemies", "enemy_levels", "record_provenance")
        for row in conn.execute(f"SELECT * FROM {table}")
    )
    assert DESCRIPTION_PROSE not in dump


def test_duplicate_level_variant_fails_gracefully(tmp_path: Path) -> None:
    # M2: a repeated level index collides on UNIQUE(enemy_pk, level_variant) and
    # must raise a graceful ImporterError, not an uncaught sqlite3.IntegrityError.
    conn = build_database(tmp_path / "cand.sqlite")
    snapshot_id = _seed_snapshot(conn)
    handbook = {"enemyData": {"e1": {"enemyId": "e1", "name": "Dup"}}}
    database = {"enemies": {"e1": {"levels": [{"level": 0, "hp": 1}, {"level": 0, "hp": 2}]}}}
    with pytest.raises(ImporterError, match="duplicate level_variant"):
        insert_enemies(
            conn,
            parse_enemies(handbook, database),
            server="en",
            snapshot_id=snapshot_id,
            handbook_source_path="gamedata/excel/enemy_handbook_table.json",
        )


def test_database_only_enemy_is_imported() -> None:
    # L3: an enemy present only in the stats database (no handbook entry) is still
    # imported with its levels so a stage spawn referencing it does not fail closed.
    handbook = {"enemyData": {"enemy_1007_slime": HANDBOOK["enemyData"]["enemy_1007_slime"]}}
    database = {
        "enemies": {
            "enemy_1007_slime": DATABASE["enemies"]["enemy_1007_slime"],
            "enemy_db_only": {"levels": [{"level": 0, "hp": 500}]},
        }
    }
    parsed = {e.game_id: e for e in parse_enemies(handbook, database)}
    assert "enemy_db_only" in parsed
    assert parsed["enemy_db_only"].display_name is None  # no handbook fields
    assert parsed["enemy_db_only"].levels[0].hp == 500


def test_import_via_adapter(tmp_path: Path) -> None:
    import json as _json

    root = tmp_path / "en"
    (root / "gamedata" / "excel").mkdir(parents=True)
    (root / "gamedata" / "levels" / "enemydata").mkdir(parents=True)
    (root / "gamedata" / "excel" / "enemy_handbook_table.json").write_text(
        _json.dumps(HANDBOOK), encoding="utf-8"
    )
    (root / "gamedata" / "levels" / "enemydata" / "enemy_database.json").write_text(
        _json.dumps(DATABASE), encoding="utf-8"
    )
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    adapter = LocalSnapshotAdapter(root, server="en")
    result = import_enemies(conn, adapter, "en:test000000")
    conn.commit()
    assert result.enemies_inserted == 2
