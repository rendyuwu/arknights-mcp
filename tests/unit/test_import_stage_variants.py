"""T80: stage-scoped inline enemy variant modelling end-to-end (§V29/§V43/§V18).

A ``useDb:false`` wave-action ref is a level-inline enemy variant whose real stats
(``overwrittenData``) differ from its base prefab (B37). These tests drive the real
grid-shape level through ``import_stages`` and assert the variant is persisted as a
stage-scoped ``stage_enemy_variants`` row with a ``prefab_base`` FK + provenance +
region, that its overridden stats are read *over* the base at query time, that two
distinct variants of the same base do not collapse, that prose never lands, that an
unresolvable base fails closed (§V3), and that a purge cascades the variant rows.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.migrations import build_database
from arknights_mcp.db.purge import _purge_source_rows
from arknights_mcp.db.repositories.stages import StageRepository
from arknights_mcp.importers.enemies import ImporterError, import_enemies
from arknights_mcp.importers.stages import import_stages
from arknights_mcp.services.stages import analyze_stage, get_stage
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

_SOURCE_ID = "local_snapshot"

# Base prefab the inline variants derive from; base def/res are deliberately low so
# an overriding variant reads a clearly different value (variant-over-base, §V43).
HANDBOOK = {
    "enemyData": {
        "enemy_1105_tyokai": {
            "enemyId": "enemy_1105_tyokai",
            "name": "Yokai",
            "enemyLevel": "ELITE",
            "attackType": "physical",
            "motionType": "WALK",
        }
    }
}
DATABASE = {
    "enemies": {
        "enemy_1105_tyokai": {
            "levels": [{"level": 0, "hp": 5000, "atk": 300, "def": 100, "res": 10}]
        }
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
            "levelId": "gamedata/levels/main/level_main_04-04.json",
        }
    }
}

_VARIANT_NAME_PROSE = "SECRET_VARIANT_NAME_V18"
_VARIANT_DESC_PROSE = "SECRET_VARIANT_DESCRIPTION_V18"


def _real_level(*, base_prefab: str = "enemy_1105_tyokai") -> dict:
    """A real grid-shape level with a base spawn + two inline variants of one base.

    ``_b`` overrides def/res/motion (+ carries prose to be stripped); ``_c`` overrides
    only hp -- both derive from the same base prefab so the occurrence rows must not
    collapse. The grid ``map`` forces the real-shape normalize branch that collects
    variants (a synthetic level is returned unchanged).
    """
    overwritten_b = {
        "prefabKey": {"m_defined": True, "m_value": base_prefab},
        "attributes": {
            "def": {"m_defined": True, "m_value": 9999},
            "magicResistance": {"m_defined": True, "m_value": 80},
            # An undefined stat is a delta sentinel: it must be dropped so the base
            # value is inherited, not written as 0 (§V44).
            "atk": {"m_defined": False, "m_value": 0},
        },
        "motion": {"m_defined": True, "m_value": "FLY"},
        # Prose that must never be persisted (§V18/§V16).
        "name": {"m_defined": True, "m_value": _VARIANT_NAME_PROSE},
        "description": _VARIANT_DESC_PROSE,
    }
    overwritten_c = {
        "prefabKey": {"m_defined": True, "m_value": base_prefab},
        "attributes": {"maxHp": {"m_defined": True, "m_value": 12345}},
    }

    def _spawn(key: str) -> dict:
        return {"actionType": "SPAWN", "key": key, "routeIndex": 0, "count": 1}

    return {
        "mapData": {"map": [[0]], "tiles": [{"tileKey": "t", "passableMask": "ALL"}]},
        "routes": [{"startPosition": {"row": 0, "col": 0}, "endPosition": {"row": 0, "col": 0}}],
        "enemyDbRefs": [
            {"useDb": True, "id": "enemy_1105_tyokai", "level": 0},
            {
                "useDb": False,
                "id": "enemy_1105_tyokai_b",
                "level": 0,
                "overwrittenData": overwritten_b,
            },
            {
                "useDb": False,
                "id": "enemy_1105_tyokai_c",
                "level": 0,
                "overwrittenData": overwritten_c,
            },
        ],
        "waves": [
            {
                "fragments": [
                    {
                        "actions": [
                            _spawn("enemy_1105_tyokai"),
                            _spawn("enemy_1105_tyokai_b"),
                            _spawn("enemy_1105_tyokai_c"),
                        ]
                    }
                ]
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
        (_SOURCE_ID, "Local", "op", "local://x", "t", '["en"]', "1", "l", "p", "r", "a", 1, "d"),
    )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES (?,?,?,?,?,?,?)",
        (snapshot_id, _SOURCE_ID, "en", "2026-07-20T00:00:00+00:00", "mh", "imported", "1"),
    )
    conn.commit()
    return snapshot_id


def _import(tmp_path: Path, level: dict) -> sqlite3.Connection:
    root = tmp_path / "en"
    _write(root, "gamedata/excel/enemy_handbook_table.json", HANDBOOK)
    _write(root, "gamedata/levels/enemydata/enemy_database.json", DATABASE)
    _write(root, "gamedata/excel/zone_table.json", ZONE_TABLE)
    _write(root, "gamedata/excel/stage_table.json", STAGE_TABLE)
    _write(root, "gamedata/levels/main/level_main_04-04.json", level)
    conn = build_database(tmp_path / "cand.sqlite")
    snapshot_id = _seed_snapshot(conn)
    adapter = LocalSnapshotAdapter(root, server="en")
    import_enemies(conn, adapter, snapshot_id)
    import_stages(conn, adapter, snapshot_id)
    conn.commit()
    return conn


def _stage_pk(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT stage_pk FROM stages WHERE stage_code='4-4'").fetchone()[0])


def test_inline_variant_rows_carry_prefab_base_and_provenance(tmp_path: Path) -> None:
    """§T80/§V17/§V29: each useDb:false variant is a stage-scoped row with a
    prefab_base FK to the base enemy, provenance, and the defined stat overrides."""
    conn = _import(tmp_path, _real_level())
    rows = conn.execute(
        "SELECT v.variant_id, e.game_id, v.def, v.res, v.motion_type, v.hp, v.provenance_id "
        "FROM stage_enemy_variants v JOIN enemies e ON e.enemy_pk = v.prefab_base_enemy_pk "
        "ORDER BY v.variant_id"
    ).fetchall()
    by_id = {r[0]: r for r in rows}
    assert set(by_id) == {"enemy_1105_tyokai_b", "enemy_1105_tyokai_c"}

    var_b = by_id["enemy_1105_tyokai_b"]
    assert var_b[1] == "enemy_1105_tyokai"  # prefab base resolved to the FK
    assert var_b[2] == 9999  # def override
    assert var_b[3] == 80  # res override
    assert var_b[4] == "FLY"  # motion override
    assert var_b[6] is not None  # §V17 provenance

    # §V44: an undefined stat (atk) is not written -- inherit the base at read.
    assert var_b[5] is None  # hp not overridden on _b
    assert (
        conn.execute(
            "SELECT atk FROM stage_enemy_variants WHERE variant_id='enemy_1105_tyokai_b'"
        ).fetchone()[0]
        is None
    )


def test_inline_variant_prose_never_persisted(tmp_path: Path) -> None:
    """§V18/§V16: overwrittenData name/description prose is stripped by the field
    allowlist and never reaches any stored row."""
    conn = _import(tmp_path, _real_level())
    dump = "\n".join(
        str(row)
        for table in ("stage_enemy_variants", "stage_spawns", "stage_enemies", "record_provenance")
        for row in conn.execute(f"SELECT * FROM {table}")  # noqa: S608 - fixed table names
    )
    assert _VARIANT_NAME_PROSE not in dump
    assert _VARIANT_DESC_PROSE not in dump


def test_variants_of_same_base_do_not_collapse(tmp_path: Path) -> None:
    """§T80: two distinct inline variants of the same base prefab + level stay as
    distinct occurrences (each keeps its own stats), plus the base spawn itself."""
    conn = _import(tmp_path, _real_level())
    stage_pk = _stage_pk(conn)
    rows = conn.execute(
        "SELECT variant_pk FROM stage_enemies WHERE stage_pk = ? ORDER BY variant_pk IS NULL, "
        "variant_pk",
        (stage_pk,),
    ).fetchall()
    # Three occurrence rows: base (variant_pk NULL) + the two variants.
    assert len(rows) == 3
    assert sum(1 for r in rows if r[0] is None) == 1  # exactly one base occurrence


def test_spawn_links_variant_pk(tmp_path: Path) -> None:
    """§T80: a variant spawn carries variant_pk; the base spawn carries NULL."""
    conn = _import(tmp_path, _real_level())
    linked = conn.execute(
        "SELECT COUNT(*) FROM stage_spawns WHERE variant_pk IS NOT NULL"
    ).fetchone()
    base = conn.execute("SELECT COUNT(*) FROM stage_spawns WHERE variant_pk IS NULL").fetchone()
    assert linked[0] == 2  # _b and _c
    assert base[0] == 1  # the useDb:true base spawn


def test_occurrence_reads_variant_stats_over_base(tmp_path: Path) -> None:
    """§V43 resolved: the repository occurrence reads the variant's def/res/motion
    over the base prefab; an un-overridden stat inherits the base."""
    conn = _import(tmp_path, _real_level())
    repo = StageRepository(conn)
    occ = {o.variant_id: o for o in repo.stage_enemies(_stage_pk(conn))}

    base = occ[None]
    assert base.def_ == 100 and base.res == 10 and base.motion_type == "WALK"

    var_b = occ["enemy_1105_tyokai_b"]
    assert var_b.def_ == 9999  # variant def over base
    assert var_b.res == 80  # variant res over base
    assert var_b.motion_type == "FLY"  # variant motion over base

    var_c = occ["enemy_1105_tyokai_c"]
    assert var_c.def_ == 100  # _c overrides only hp -> def inherits base
    assert var_c.res == 10


def test_get_stage_and_analyze_surface_variant_id(tmp_path: Path) -> None:
    """§T80/§V14: both stage read paths expose variant_id (analyzer occurrence +
    get_stage spawn), so a client can tell an inline variant from the base."""
    conn = _import(tmp_path, _real_level())
    analysis = analyze_stage(conn, server="en", stage_code="4-4")
    variant_ids = {o.variant_id for o in analysis.occurrences}
    assert variant_ids == {None, "enemy_1105_tyokai_b", "enemy_1105_tyokai_c"}

    detail = get_stage(conn, server="en", stage_code="4-4", include_spawns=True)
    spawn_variants = {s.variant_id for s in detail.spawns}
    assert spawn_variants == {None, "enemy_1105_tyokai_b", "enemy_1105_tyokai_c"}


def test_variant_with_unresolvable_base_fails_closed(tmp_path: Path) -> None:
    """§V3/§V43: an inline variant whose prefab base is not in the region's enemies
    fails closed with a graceful ImporterError, never a fabricated enemy row."""
    level = _real_level(base_prefab="enemy_does_not_exist")
    with pytest.raises(ImporterError, match="unknown base enemy"):
        _import(tmp_path, level)


def test_purge_cascades_variant_rows(tmp_path: Path) -> None:
    """§V32: purging the source removes its stage_enemy_variants rows and leaves no
    dangling foreign key (children-before-parents)."""
    conn = _import(tmp_path, _real_level())
    assert conn.execute("SELECT COUNT(*) FROM stage_enemy_variants").fetchone()[0] == 2
    _purge_source_rows(conn, _SOURCE_ID)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM stage_enemy_variants").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
