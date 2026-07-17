"""T14: stage + level/map/wave/spawn parser for the pinned 4-4 fixture.

Verifies zones/stages import, the level parser fills map/tiles/routes/waves/
spawns and derives stage_enemies, prose is dropped (§V18), provenance is
attached (§V17), and unresolved spawn references fail closed (§21.2).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.enemies import ImporterError, import_enemies
from arknights_mcp.importers.stages import import_stages, parse_stages
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

STAGE_PROSE = "Flavorful stage lore that must never be imported."

HANDBOOK = {
    "enemyData": {
        "enemy_1007_slime": {
            "enemyId": "enemy_1007_slime",
            "name": "Originium Slug",
            "enemyLevel": "NORMAL",
            "attackType": "physical",
            "motionType": "WALK",
        },
        "enemy_1105_drone": {
            "enemyId": "enemy_1105_drone",
            "name": "Recon Drone",
            "enemyLevel": "ELITE",
            "attackType": "physical",
            "motionType": "FLY",
        },
    }
}
DATABASE = {
    "enemies": {
        "enemy_1007_slime": {
            "levels": [{"level": 0, "hp": 1650, "atk": 320, "def": 100, "res": 0}]
        },
        "enemy_1105_drone": {"levels": [{"level": 0, "hp": 900, "atk": 260, "def": 0, "res": 10}]},
    }
}
ZONE_TABLE = {
    "zones": {"main_4": {"zoneId": "main_4", "zoneName": "Chapter 4", "type": "MAINLINE"}}
}
STAGE_TABLE = {
    "stages": {
        "main_04-04": {
            "stageId": "main_04-04",
            "code": "4-4",
            "name": "Combustion",
            "zoneId": "main_4",
            "stageType": "MAIN",
            "difficulty": "NORMAL",
            "apCost": 18,
            "recommendedLevel": 45,
            "maxLifePoints": 3,
            "levelId": "gamedata/levels/main/level_main_04-04.json",
            "description": STAGE_PROSE,
        }
    }
}
LEVEL = {
    "mapData": {
        "width": 8,
        "height": 5,
        "mapVersion": "1",
        "environment": {},
        "tiles": [
            {
                "x": 0,
                "y": 0,
                "tileKey": "tile_start",
                "heightType": "LOWLAND",
                "buildableType": "NONE",
                "passable": True,
            },
            {
                "x": 7,
                "y": 4,
                "tileKey": "tile_end",
                "heightType": "LOWLAND",
                "buildableType": "NONE",
                "passable": True,
            },
        ],
    },
    "routes": [
        {
            "routeIndex": 0,
            "startPosition": {"row": 0, "col": 0},
            "endPosition": {"row": 4, "col": 7},
            "checkpoints": [],
        }
    ],
    "waves": [
        {
            "waveIndex": 0,
            "preDelay": 1.0,
            "maxTimeWaiting": 10.0,
            "fragments": [
                {
                    "actions": [
                        {
                            "enemyId": "enemy_1007_slime",
                            "levelVariant": 0,
                            "routeIndex": 0,
                            "spawnTime": 2.0,
                            "count": 3,
                            "interval": 1.5,
                            "spawnGroup": "g0",
                            "hidden": False,
                        },
                        {
                            "enemyId": "enemy_1105_drone",
                            "levelVariant": 0,
                            "routeIndex": 0,
                            "spawnTime": 8.0,
                            "count": 2,
                            "interval": 2.0,
                            "spawnGroup": "g1",
                            "hidden": False,
                        },
                    ]
                }
            ],
        }
    ],
}


def _write(root: Path, rel: str, obj: object) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


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


def _import_all(tmp_path: Path) -> tuple[sqlite3.Connection, LocalSnapshotAdapter]:
    root = tmp_path / "en"
    _write(root, "gamedata/excel/enemy_handbook_table.json", HANDBOOK)
    _write(root, "gamedata/levels/enemydata/enemy_database.json", DATABASE)
    _write(root, "gamedata/excel/zone_table.json", ZONE_TABLE)
    _write(root, "gamedata/excel/stage_table.json", STAGE_TABLE)
    _write(root, "gamedata/levels/main/level_main_04-04.json", LEVEL)
    conn = build_database(tmp_path / "cand.sqlite")
    snapshot_id = _seed_snapshot(conn)
    adapter = LocalSnapshotAdapter(root, server="en")
    import_enemies(conn, adapter, snapshot_id)
    import_stages(conn, adapter, snapshot_id)
    conn.commit()
    return conn, adapter


def test_parse_stages_drops_prose() -> None:
    parsed = parse_stages(STAGE_TABLE)
    stage = parsed[0]
    assert stage.stage_code == "4-4"
    assert stage.sanity_cost == 18
    assert "description" not in stage.provenance_record
    assert STAGE_PROSE not in str(stage.provenance_record)


def test_stage_and_zone_imported(tmp_path: Path) -> None:
    conn, _ = _import_all(tmp_path)
    stage = conn.execute(
        "SELECT stage_code, display_name, sanity_cost, provenance_id, zone_pk "
        "FROM stages WHERE server='en' AND game_id='main_04-04'"
    ).fetchone()
    assert stage[0] == "4-4"
    assert stage[2] == 18
    assert stage[3] is not None  # V17 provenance
    assert stage[4] is not None  # zone resolved


def test_level_map_routes_waves_spawns(tmp_path: Path) -> None:
    conn, _ = _import_all(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM stage_tiles").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM stage_routes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM stage_waves").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM stage_spawns").fetchone()[0] == 2
    wmap = conn.execute("SELECT width, height FROM stage_maps").fetchone()
    assert wmap == (8, 5)


def test_stage_enemies_summary(tmp_path: Path) -> None:
    conn, _ = _import_all(tmp_path)
    rows = conn.execute(
        "SELECT e.game_id, se.total_count, se.first_spawn_time, se.last_spawn_time, se.route_count "
        "FROM stage_enemies se JOIN enemies e ON e.enemy_pk = se.enemy_pk "
        "JOIN stages s ON s.stage_pk = se.stage_pk WHERE s.stage_code='4-4' "
        "ORDER BY e.game_id"
    ).fetchall()
    summary = {r[0]: r for r in rows}
    assert summary["enemy_1007_slime"][1] == 3  # total_count
    assert summary["enemy_1105_drone"][1] == 2
    assert summary["enemy_1105_drone"][2] == 8.0  # first_spawn_time
    assert summary["enemy_1105_drone"][4] == 1  # route_count


def test_no_stage_prose_in_db(tmp_path: Path) -> None:
    conn, _ = _import_all(tmp_path)
    dump = "\n".join(
        str(row)
        for table in ("stages", "zones", "stage_maps", "stage_tiles", "record_provenance")
        for row in conn.execute(f"SELECT * FROM {table}")
    )
    assert STAGE_PROSE not in dump


def test_unresolved_enemy_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "en"
    _write(root, "gamedata/excel/enemy_handbook_table.json", HANDBOOK)
    _write(root, "gamedata/levels/enemydata/enemy_database.json", DATABASE)
    _write(root, "gamedata/excel/zone_table.json", ZONE_TABLE)
    _write(root, "gamedata/excel/stage_table.json", STAGE_TABLE)
    bad_level = json.loads(json.dumps(LEVEL))
    bad_level["waves"][0]["fragments"][0]["actions"][0]["enemyId"] = "enemy_does_not_exist"
    _write(root, "gamedata/levels/main/level_main_04-04.json", bad_level)
    conn = build_database(tmp_path / "cand.sqlite")
    snapshot_id = _seed_snapshot(conn)
    adapter = LocalSnapshotAdapter(root, server="en")
    import_enemies(conn, adapter, snapshot_id)
    with pytest.raises(ImporterError, match="unknown enemy"):
        import_stages(conn, adapter, snapshot_id)
