"""T69: kengxxiao CN cross-validator — offline unit coverage (§V29, §V30, §C).

Exercises the two pure pieces of the CN cross-validator without any network:

* the kengxxiao KV-list → normalized bridge
  (:func:`~arknights_mcp.importers.normalization.normalize_kengxxiao_enemy_database`),
  which must produce the same normalized shape the primary bridge does (§V30); and
* the stat comparison
  (:func:`~arknights_mcp.sources.kengxxiao_validator.cross_check_raw_enemy_databases`),
  which reports agreement/mismatch over shared cells (§V29).

The CI-only ``tests/contract/test_kengxxiao_cross_validate.py`` feeds these the
real pinned upstreams; here we drive them with the documented real shapes.
"""

from __future__ import annotations

from arknights_mcp.importers.normalization import normalize_kengxxiao_enemy_database
from arknights_mcp.sources.kengxxiao_validator import cross_check_raw_enemy_databases

# The primary source (arknights_assets_gamedata) enemy DB is a top-level id-keyed
# dict → list of level entries (§V29). Two enemies, one flyer.
PRIMARY_RAW = {
    "enemy_1007_slime": [
        {
            "level": 0,
            "enemyData": {
                "attributes": {
                    "maxHp": {"m_defined": True, "m_value": 1650},
                    "magicResistance": {"m_defined": True, "m_value": 0},
                    "baseAttackTime": {"m_defined": True, "m_value": 2.0},
                    "massLevel": {"m_defined": True, "m_value": 2},
                },
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
                    "baseAttackTime": {"m_defined": True, "m_value": 1.6},
                    "massLevel": {"m_defined": True, "m_value": 0},
                },
                "motion": {"m_defined": True, "m_value": "FLY"},
            },
        }
    ],
}


def _kengxxiao_kv(enemies: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    """Wrap an id-keyed dict into kengxxiao's ``{"enemies": [{"Key", "Value"}]}`` shape."""
    return {"enemies": [{"Key": game_id, "Value": levels} for game_id, levels in enemies.items()]}


# The kengxxiao CN dump: same underlying values, KV-list top level (§T69).
KENGXXIAO_RAW_MATCHING = _kengxxiao_kv(PRIMARY_RAW)


def test_kengxxiao_kv_list_normalizes_like_primary() -> None:
    """§V30: the KV-list bridge yields the same normalized shape + renamed stats."""
    database_norm, motion = normalize_kengxxiao_enemy_database(KENGXXIAO_RAW_MATCHING)
    assert set(database_norm) == {"enemies"}
    slime = database_norm["enemies"]["enemy_1007_slime"]["levels"][0]
    assert slime["hp"] == 1650  # maxHp -> hp
    assert slime["res"] == 0  # magicResistance -> res
    assert slime["attackInterval"] == 2.0  # baseAttackTime -> attackInterval
    assert slime["weight"] == 2  # massLevel -> weight
    # motion is extracted from enemyData.motion.m_value, not the level body.
    assert motion["enemy_1007_slime"] == "WALK"
    assert motion["enemy_1105_drone"] == "FLY"


def test_kengxxiao_bridge_idempotent_on_normalized_input() -> None:
    """§V30: already-normalized input passes through unchanged (empty motion map)."""
    normalized = {"enemies": {"e1": {"levels": [{"level": 0, "hp": 10}]}}}
    database_norm, motion = normalize_kengxxiao_enemy_database(normalized)
    assert database_norm == normalized
    assert motion == {}


def test_kengxxiao_bridge_skips_malformed_pairs() -> None:
    """Non-dict pairs / missing Key|Value are dropped, not crashed on."""
    raw = {
        "enemies": [
            {"Key": "enemy_ok", "Value": [{"level": 0, "enemyData": {"attributes": {}}}]},
            {"Key": "", "Value": []},  # blank id → dropped
            {"Value": [{"level": 0}]},  # no Key → dropped
            "not-a-dict",  # → dropped
        ]
    }
    database_norm, _ = normalize_kengxxiao_enemy_database(raw)
    assert set(database_norm["enemies"]) == {"enemy_ok"}


def test_cross_check_identical_sources_full_agreement() -> None:
    """§V29: identical CN sources agree on every shared cell → rate 1.0."""
    report = cross_check_raw_enemy_databases(PRIMARY_RAW, KENGXXIAO_RAW_MATCHING)
    assert report.compared_enemies == 2
    assert report.compared_cells > 0
    assert report.mismatches == ()
    assert report.agreement_rate == 1.0


def test_cross_check_reports_stat_divergence() -> None:
    """§V29: a differing stat is recorded as a mismatch, dropping the agreement rate."""
    diverged = _kengxxiao_kv(
        {
            "enemy_1007_slime": [
                {
                    "level": 0,
                    "enemyData": {
                        "attributes": {
                            "maxHp": {"m_value": 9999},  # differs from primary 1650
                            "magicResistance": {"m_value": 0},
                            "baseAttackTime": {"m_value": 2.0},
                            "massLevel": {"m_value": 2},
                        },
                        "motion": {"m_value": "WALK"},
                    },
                }
            ],
            "enemy_1105_drone": PRIMARY_RAW["enemy_1105_drone"],
        }
    )
    report = cross_check_raw_enemy_databases(PRIMARY_RAW, diverged)
    assert len(report.mismatches) == 1
    (mismatch,) = report.mismatches
    assert (mismatch.game_id, mismatch.stat) == ("enemy_1007_slime", "hp")
    assert (mismatch.primary, mismatch.kengxxiao) == (1650, 9999)
    assert 0.0 < report.agreement_rate < 1.0


def test_cross_check_reports_motion_divergence() -> None:
    """§V29 (d): motion is cross-checked per enemy; a flip is a mismatch."""
    flipped = _kengxxiao_kv(
        {
            "enemy_1105_drone": [
                {
                    "level": 0,
                    "enemyData": {
                        "attributes": {
                            "maxHp": {"m_value": 900},
                            "baseAttackTime": {"m_value": 1.6},
                            "massLevel": {"m_value": 0},
                        },
                        "motion": {"m_value": "WALK"},  # primary says FLY
                    },
                }
            ]
        }
    )
    report = cross_check_raw_enemy_databases(PRIMARY_RAW, flipped)
    motion_mismatches = [m for m in report.mismatches if m.stat == "motion"]
    assert len(motion_mismatches) == 1
    assert motion_mismatches[0].level_variant is None
    assert (motion_mismatches[0].primary, motion_mismatches[0].kengxxiao) == ("FLY", "WALK")


def test_cross_check_skips_one_sided_stats() -> None:
    """A stat present in only one source is not a mismatch — it is simply not compared."""
    primary = {
        "enemy_x": [
            {"level": 0, "enemyData": {"attributes": {"maxHp": {"m_value": 100}}}},
        ]
    }
    # kengxxiao has the enemy but omits maxHp (only massLevel present).
    kengxxiao = _kengxxiao_kv(
        {"enemy_x": [{"level": 0, "enemyData": {"attributes": {"massLevel": {"m_value": 1}}}}]}
    )
    report = cross_check_raw_enemy_databases(primary, kengxxiao)
    assert report.compared_enemies == 1
    assert report.compared_cells == 0  # no stat present on both sides
    assert report.mismatches == ()
    assert report.agreement_rate == 0.0  # nothing compared is not vacuous success


def test_cross_check_disjoint_sources_compare_nothing() -> None:
    """No shared enemies → nothing compared; agreement is reported as 0.0."""
    other = _kengxxiao_kv(
        {"enemy_zzz": [{"level": 0, "enemyData": {"attributes": {"maxHp": {"m_value": 1}}}}]}
    )
    report = cross_check_raw_enemy_databases(PRIMARY_RAW, other)
    assert report.compared_enemies == 0
    assert report.compared_cells == 0
    assert report.agreement_rate == 0.0
