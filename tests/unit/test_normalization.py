"""T66: the raw ``arknights_assets_gamedata`` → normalized bridge (§V29, §V30, §V36).

Drives :mod:`arknights_mcp.importers.normalization` directly with the *real*
upstream shapes B6/§V29 documents, asserting each transform produces the
normalized shape the parsers consume, and that every transform is idempotent on
already-normalized (synthetic) input so the minimal fixture path is unaffected.
"""

from __future__ import annotations

from arknights_mcp.importers.normalization import (
    normalize_enemy_sources,
    normalize_level,
    normalize_level_id,
)

# --- enemy_database + handbook (§V29 (a)/(d)) ---------------------------------

REAL_HANDBOOK = {
    "enemyData": {
        "enemy_1007_slime": {
            "enemyId": "enemy_1007_slime",
            "name": "Originium Slug",
            "enemyLevel": "NORMAL",
            "attackType": "physical",
        },
        "enemy_1105_drone": {
            "enemyId": "enemy_1105_drone",
            "name": "Recon Drone",
            "enemyLevel": "ELITE",
            "attackType": "physical",
        },
    }
}

REAL_DATABASE = {
    "enemy_1007_slime": [
        {
            "level": 0,
            "enemyData": {
                "attributes": {
                    "maxHp": {"m_defined": True, "m_value": 1650},
                    "atk": {"m_defined": True, "m_value": 320},
                    "def": {"m_defined": True, "m_value": 100},
                    "magicResistance": {"m_defined": True, "m_value": 0},
                    "moveSpeed": {"m_defined": True, "m_value": 0.7},
                    "baseAttackTime": {"m_defined": True, "m_value": 2.0},
                    "massLevel": {"m_defined": True, "m_value": 2},
                },
                "lifePointReduce": {"m_defined": True, "m_value": 1},
                "motion": {"m_defined": True, "m_value": "WALK"},
            },
        }
    ],
    "enemy_1105_drone": [
        {
            "level": 0,
            "enemyData": {
                "attributes": {
                    "maxHp": {"m_defined": True, "m_value": 900},
                    "magicResistance": {"m_defined": True, "m_value": 10},
                    "baseAttackTime": {"m_defined": True, "m_value": 1.6},
                },
                "motion": {"m_defined": True, "m_value": "FLY"},
            },
        }
    ],
}


def test_enemy_database_id_keyed_list_becomes_normalized() -> None:
    """§V29 (a): id-keyed dict → list with ``m_value`` attrs → ``{"enemies": {...}}``
    with the renamed stat keys the parser reads."""
    _, database = normalize_enemy_sources(REAL_HANDBOOK, REAL_DATABASE)
    assert set(database) == {"enemies"}
    slime = database["enemies"]["enemy_1007_slime"]["levels"][0]
    assert slime["level"] == 0
    assert slime["hp"] == 1650  # maxHp -> hp
    assert slime["res"] == 0  # magicResistance -> res
    assert slime["attackInterval"] == 2.0  # baseAttackTime -> attackInterval
    assert slime["weight"] == 2  # massLevel -> weight
    assert slime["lifePointReduction"] == 1  # lifePointReduce -> lifePointReduction
    assert slime["moveSpeed"] == 0.7
    assert slime["atk"] == 320
    assert slime["def"] == 100


def test_motion_injected_into_handbook_from_database() -> None:
    """§V29 (d): the handbook has no ``motionType``; motion is sourced from
    ``enemyData.motion.m_value`` and backfilled into the handbook entry."""
    handbook, _ = normalize_enemy_sources(REAL_HANDBOOK, REAL_DATABASE)
    entries = handbook["enemyData"]
    assert entries["enemy_1007_slime"]["motionType"] == "WALK"
    assert entries["enemy_1105_drone"]["motionType"] == "FLY"
    # The input mapping is not mutated in place.
    assert "motionType" not in REAL_HANDBOOK["enemyData"]["enemy_1007_slime"]


def test_partial_attributes_only_emit_present_keys() -> None:
    """A level that omits an attribute simply omits the normalized key (no crash)."""
    _, database = normalize_enemy_sources(REAL_HANDBOOK, REAL_DATABASE)
    drone = database["enemies"]["enemy_1105_drone"]["levels"][0]
    assert drone["hp"] == 900
    assert "def" not in drone  # absent in the source attributes


def test_enemy_sources_idempotent_on_normalized_input() -> None:
    """§V30: already-normalized input passes through unchanged (the synthetic path)."""
    normalized_db = {
        "enemies": {"e1": {"levels": [{"level": 0, "hp": 10, "res": 1, "attackInterval": 1.0}]}}
    }
    normalized_hb = {"enemyData": {"e1": {"enemyId": "e1", "name": "E", "motionType": "WALK"}}}
    handbook, database = normalize_enemy_sources(normalized_hb, normalized_db)
    assert database == normalized_db
    assert handbook == normalized_hb


# --- levelId → resolvable snapshot path (§V29 (b)/§V36) -----------------------


def test_level_id_title_case_becomes_snapshot_path() -> None:
    assert (
        normalize_level_id("Obt/Main/level_main_04-04")
        == "gamedata/levels/obt/main/level_main_04-04.json"
    )


def test_level_id_already_resolvable_passes_through() -> None:
    resolved = "gamedata/levels/main/level_main_04-04.json"
    assert normalize_level_id(resolved) == resolved


def test_level_id_none_and_blank() -> None:
    assert normalize_level_id(None) is None
    assert normalize_level_id("   ") is None


def test_level_id_always_forced_under_levels_tree() -> None:
    """§V36: the result is always under ``gamedata/levels/`` — a stage can never
    resolve a levelId to an excel table."""
    assert normalize_level_id("gamedata/excel/character_table.json").startswith("gamedata/levels/")


# --- level file: grid → tiles, key → enemy, positional indices (§V29 (c)) -----

REAL_LEVEL = {
    "mapData": {
        "map": [[0, 1], [1, 2]],
        "tiles": [
            {"tileKey": "tile_start", "heightType": "LOWLAND", "passableMask": "ALL"},
            {"tileKey": "tile_road", "heightType": "LOWLAND", "passableMask": "ALL"},
            {"tileKey": "tile_end", "heightType": "LOWLAND", "passableMask": "NONE"},
        ],
    },
    "routes": [{"startPosition": {"row": 0, "col": 0}, "endPosition": {"row": 1, "col": 1}}],
    "enemyDbRefs": [
        {"useDb": True, "id": "enemy_1007_slime", "level": 0},
        {"useDb": True, "id": "enemy_1105_drone", "level": 1},
    ],
    "waves": [
        {
            "preDelay": 1.0,
            "maxTimeWaitingForNextWave": 10.0,
            "fragments": [
                {
                    "actions": [
                        {
                            "actionType": "SPAWN",
                            "key": "enemy_1007_slime",
                            "count": 3,
                            "preDelay": 2.0,
                            "routeIndex": 0,
                        },
                        {
                            "actionType": "SPAWN",
                            "key": "enemy_1105_drone",
                            "count": 2,
                            "preDelay": 8.0,
                            "routeIndex": 0,
                        },
                        # B35: real waves interleave non-spawn actions that also
                        # carry a ``key``. A STORY key is a story-asset path (not an
                        # enemy id — the old code leaked it as a spawn and crashed
                        # the cross-ref check); DISPLAY_ENEMY_INFO/PREVIEW_CURSOR
                        # name a real enemy as a codex/preview cue (the old code
                        # fabricated a phantom count=1 spawn from each). All must
                        # be dropped — only actionType == "SPAWN" is a spawn.
                        {"actionType": "STORY", "key": "activities/a001/tutorial_a001_01_a"},
                        {"actionType": "DISPLAY_ENEMY_INFO", "key": "enemy_1007_slime", "count": 1},
                        {"actionType": "PREVIEW_CURSOR", "key": "enemy_1105_drone", "count": 1},
                    ]
                }
            ],
        }
    ],
}


def test_grid_tiles_get_derived_xy_and_passable() -> None:
    """§V29 (c): grid-indexed tiles (no x/y) → tiles with derived x/y;
    ``passableMask`` → ``passable``."""
    level = normalize_level(REAL_LEVEL)
    tiles = level["mapData"]["tiles"]
    assert len(tiles) == 4  # one per grid cell (2x2)
    assert level["mapData"]["width"] == 2
    assert level["mapData"]["height"] == 2
    by_xy = {(t["x"], t["y"]): t for t in tiles}
    assert by_xy[(0, 0)]["tileKey"] == "tile_start"
    assert by_xy[(0, 0)]["passable"] is True  # "ALL"
    assert by_xy[(1, 1)]["tileKey"] == "tile_end"
    assert by_xy[(1, 1)]["passable"] is False  # "NONE"


def test_positional_route_and_wave_indices_injected() -> None:
    level = normalize_level(REAL_LEVEL)
    assert level["routes"][0]["routeIndex"] == 0
    assert level["waves"][0]["waveIndex"] == 0
    assert level["waves"][0]["maxTimeWaiting"] == 10.0  # from maxTimeWaitingForNextWave


def test_wave_action_key_resolves_to_enemy_and_variant() -> None:
    """§V29 (c): a wave action names its enemy under ``key`` (resolved via
    ``enemyDbRefs``), not ``enemyId``; non-spawn actions are dropped."""
    level = normalize_level(REAL_LEVEL)
    actions = level["waves"][0]["fragments"][0]["actions"]
    assert len(actions) == 2  # the STORY action (no enemy key) is dropped
    by_enemy = {a["enemyId"]: a for a in actions}
    assert by_enemy["enemy_1007_slime"]["levelVariant"] == 0
    assert by_enemy["enemy_1105_drone"]["levelVariant"] == 1  # from enemyDbRefs
    assert by_enemy["enemy_1007_slime"]["spawnTime"] == 2.0  # preDelay -> spawnTime
    assert by_enemy["enemy_1007_slime"]["count"] == 3


# --- useDb:false inline enemy variant → base prefab (§V43, B37) ---------------


def _inline_variant_level(*, with_prefab: bool) -> dict:
    """A minimal real level whose sole spawn references a ``useDb:false`` inline
    enemy variant. ``with_prefab=False`` drops the ``prefabKey`` to exercise the
    preserved fail-closed path (an inline ref that cannot resolve to a base)."""
    overwritten: dict = {"attributes": {"maxHp": {"m_defined": True, "m_value": 5000}}}
    if with_prefab:
        overwritten["prefabKey"] = {"m_defined": True, "m_value": "enemy_1105_tyokai"}
    return {
        "mapData": {"map": [[0]], "tiles": [{"tileKey": "t", "passableMask": "ALL"}]},
        "routes": [{"startPosition": {"row": 0, "col": 0}}],
        "enemyDbRefs": [
            {"useDb": True, "id": "enemy_1105_tyokai", "level": 0},
            {
                "useDb": False,
                "id": "enemy_1105_tyokai_b",
                "level": 0,
                "overwrittenData": overwritten,
            },
        ],
        "waves": [
            {
                "fragments": [
                    {"actions": [{"actionType": "SPAWN", "key": "enemy_1105_tyokai_b", "count": 1}]}
                ]
            }
        ],
    }


def test_inline_variant_spawn_resolves_to_prefab_base() -> None:
    """§V43/B37: a ``useDb:false`` inline variant spawn resolves its ``enemyId`` to
    the ref's base ``prefabKey`` (so the cross-file FK holds) and carries the
    original inline id as ``variantId`` for traceability — it is never left as the
    inline id, which is absent from the enemy tables and would fail the level
    importer's cross-reference check."""
    level = normalize_level(_inline_variant_level(with_prefab=True))
    action = level["waves"][0]["fragments"][0]["actions"][0]
    assert action["enemyId"] == "enemy_1105_tyokai"  # base prefab, resolvable in DB
    assert action["variantId"] == "enemy_1105_tyokai_b"  # inline id preserved
    assert action["count"] == 1


def test_inline_variant_without_prefab_stays_unresolved_fail_closed() -> None:
    """§V43/B37: an inline ref with no ``prefabKey`` cannot resolve to a base, so
    the spawn keeps the inline id (no fabricated resolution) and carries no
    ``variantId`` — the level importer's cross-reference check still fails closed
    on it, preserving §V3/§V4 for genuinely-unresolvable refs."""
    level = normalize_level(_inline_variant_level(with_prefab=False))
    action = level["waves"][0]["fragments"][0]["actions"][0]
    assert action["enemyId"] == "enemy_1105_tyokai_b"  # unresolved → fails closed downstream
    assert "variantId" not in action


def test_usedb_true_ref_carries_no_variant_id() -> None:
    """§V43: a normal ``useDb:true`` spawn resolves directly to the ref id and does
    not emit a ``variantId`` (so real spawns don't carry a null variant field)."""
    level = normalize_level(REAL_LEVEL)
    actions = level["waves"][0]["fragments"][0]["actions"]
    assert all("variantId" not in a for a in actions)


def test_level_idempotent_on_synthetic_shape() -> None:
    """§V30: a synthetic level (tiles already carry x/y, no ``mapData.map`` grid)
    passes through unchanged."""
    synthetic = {
        "mapData": {"width": 1, "height": 1, "tiles": [{"x": 0, "y": 0, "tileKey": "t"}]},
        "routes": [{"routeIndex": 0}],
        "waves": [{"waveIndex": 0, "fragments": [{"actions": [{"enemyId": "e1"}]}]}],
    }
    assert normalize_level(synthetic) == synthetic
