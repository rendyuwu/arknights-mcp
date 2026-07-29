"""§V113 column-liveness contract (B160): fetched != normalized != STORED.

Six allowlisted enemy columns shipped 100% NULL on the real build
(``enemy_levels.{attack_range,block_behavior,targeting_json,immunities_json,
abilities_json}`` 0/4343 and ``enemies.attack_type`` 0/3879), which silently killed
four of the nine registered threat rules over 3264 en + 1200 cn stages. Nothing
below the build could witness it: a rule reading an always-NULL field takes its own
§V26 "field missing -> reduce confidence" arm and returns **clean**, and every
synthetic-fixture unit test stayed green because the fixture hands the parser the
already-normalized key the real §V30 bridge never emits.

The guard therefore cannot live in a rule test or a fixture import test. It is an
accounting one: :data:`ENEMY_KEY_HOMES` enumerates, per allowlisted key, the real
upstream home and the counted status of the column it feeds, and this module pins
that declaration against what the bridge actually does. It fails BOTH ways --

* a key added to either enemy allowlist with no declared home (the shape that
  admitted ``blockBehavior``/``abilities``, which exist in no real source at all);
* a declaration that drifts from the bridge, in either direction: a ``live`` key
  the bridge stopped emitting, or a ``bridge_gap``/``no_home`` key it started
  emitting without the declaration being updated (T210 must move the entry, not
  just the code).

Counts inside the declaration are @pinned ``413a81a3`` + build
``2026-07-28T170428Z-en-cn``; this module asserts the STRUCTURE those counts
describe, so it runs offline with no snapshot and no build.
"""

from __future__ import annotations

from typing import Any

import pytest

from arknights_mcp.analyzers.base import EnemyOccurrence, StageThreatContext
from arknights_mcp.analyzers.rules import (
    DEAD_BY_DATA_RULES,
    DEAD_RULE_ARMS,
    RULE_DECIDING_FIELDS,
    THREAT_RULES,
)
from arknights_mcp.analyzers.stage import analyze_stage
from arknights_mcp.importers.enemies import parse_enemies
from arknights_mcp.importers.field_policy import (
    ENEMY_HANDBOOK_ALLOWLIST,
    ENEMY_KEY_HOMES,
    ENEMY_LEVEL_ALLOWLIST,
    LIVE_KEY_STATUSES,
)
from arknights_mcp.importers.normalization import normalize_enemy_sources

_ALL_ENEMY_KEYS = ENEMY_HANDBOOK_ALLOWLIST | ENEMY_LEVEL_ALLOWLIST

#: Every status the declaration may carry (§V113 (a)).
_STATUSES = frozenset({"live", "bridge_gap", "retired", "no_home", "not_stored"})

#: Occurrence field an analyzer decides from -> the allowlisted source key that
#: fills it. Fields absent from this map come from the stage tables (route/tile/
#: spawn), not the enemy allowlist, and are demonstrably non-degenerate on the
#: build (lane_route 2453, pressure_spike 1124, tiles_deploy 241 en observations).
_DECIDING_FIELD_SOURCE_KEY = {
    "attack_type": "attackType",
    "attack_range": "attackRange",
    "block_behavior": "blockBehavior",
    "abilities": "abilities",
    "motion_type": "motionType",
    "defense": "def",
    "res": "res",
}


def _real_shaped_sources() -> tuple[dict[str, Any], dict[str, Any]]:
    """A REAL-shaped handbook + enemy_database carrying every declared home.

    Deliberately not ``tests/fixtures/stage_4_4_real``: that fixture is hand-trimmed
    and asserts a value domain upstream does not send (it sets
    ``attackType: "physical"`` where all 1585 real entries send ``null``), which is
    the very thing that hid B160. This input mirrors the pinned snapshot instead --
    every ``m_defined``/``m_value`` cell wrapped as upstream wraps it, and every home
    named in :data:`ENEMY_KEY_HOMES` present with a value a bridge COULD map.
    """
    handbook = {
        "levelInfoList": [],
        "enemyData": {
            "enemy_1007_slime": {
                "enemyId": "enemy_1007_slime",
                "name": "Originium Slug",
                "enemyLevel": "NORMAL",
                "attackType": None,  # retired upstream: null on 1585/1585 entries
                "damageType": ["MAGIC"],  # the field that replaced it
                "description": "PROSE that must never be imported.",
                "abilityList": [{"text": "Cannot be blocked.", "textFormat": "NORMAL"}],
            }
        },
    }
    database = {
        "enemy_1007_slime": [
            {
                "level": 0,
                "enemyData": {
                    "name": {"m_defined": True, "m_value": "Originium Slug"},
                    "description": {"m_defined": True, "m_value": "PROSE."},
                    "motion": {"m_defined": True, "m_value": "WALK"},
                    "applyWay": {"m_defined": True, "m_value": "RANGED"},
                    "rangeRadius": {"m_defined": True, "m_value": 2.5},
                    "lifePointReduce": {"m_defined": True, "m_value": 1},
                    "skills": [{"prefabKey": "StunAttack", "priority": 0}],
                    "talentBlackboard": [{"key": "aura.range_radius", "value": 1.5}],
                    "attributes": {
                        "maxHp": {"m_defined": True, "m_value": 900},
                        "atk": {"m_defined": True, "m_value": 100},
                        "def": {"m_defined": True, "m_value": 50},
                        "magicResistance": {"m_defined": True, "m_value": 10},
                        "moveSpeed": {"m_defined": True, "m_value": 0.8},
                        "baseAttackTime": {"m_defined": True, "m_value": 2.0},
                        "massLevel": {"m_defined": True, "m_value": 2},
                        "blockCnt": {"m_defined": False, "m_value": 0},
                        "stunImmune": {"m_defined": True, "m_value": True},
                        "silenceImmune": {"m_defined": True, "m_value": False},
                        "sleepImmune": {"m_defined": True, "m_value": False},
                        "frozenImmune": {"m_defined": True, "m_value": False},
                        "levitateImmune": {"m_defined": True, "m_value": False},
                        "disarmedCombatImmune": {"m_defined": True, "m_value": False},
                        "fearedImmune": {"m_defined": True, "m_value": False},
                        "palsyImmune": {"m_defined": True, "m_value": False},
                        "attractImmune": {"m_defined": True, "m_value": False},
                    },
                },
            }
        ]
    }
    return handbook, database


def _normalized_level_keys() -> set[str]:
    """Normalized level keys the §V30 bridge really emits from a real-shaped input."""
    _, database_norm = normalize_enemy_sources(*_real_shaped_sources())
    levels = database_norm["enemies"]["enemy_1007_slime"]["levels"]
    return set(levels[0])


# --- §V113 (a): every allowlisted key names its upstream home -----------------


def test_v113a_declaration_covers_exactly_the_enemy_allowlists() -> None:
    """No allowlisted enemy key may exist without a declared home, and vice versa."""
    declared = set(ENEMY_KEY_HOMES)
    assert declared - _ALL_ENEMY_KEYS == set(), "stale ENEMY_KEY_HOMES entry (key left allowlist)"
    assert _ALL_ENEMY_KEYS - declared == set(), (
        "allowlisted key with no declared upstream home -- a column NULL by construction "
        "is what B160 was; declare its home or its dead-by-data status (§V113 (a))"
    )


@pytest.mark.parametrize("key", sorted(_ALL_ENEMY_KEYS))
def test_v113a_home_declaration_is_well_formed(key: str) -> None:
    """``no_home`` <-> no upstream path, and every entry carries a counted status."""
    entry = ENEMY_KEY_HOMES[key]
    assert entry.status in _STATUSES, f"{key}: unknown status {entry.status!r}"
    assert entry.counted.strip(), f"{key}: status is asserted, not counted (§V113 (c))"
    if entry.status == "no_home":
        assert entry.home is None, f"{key}: declared dead-by-data yet names an upstream home"
    else:
        assert entry.home, f"{key}: status {entry.status!r} must name the upstream field it reads"


def test_v113a_bridge_emits_exactly_the_live_level_keys() -> None:
    """The §V30 bridge's real output must match the declaration, in BOTH directions.

    A ``live`` level key the bridge stopped emitting is a new B160; a
    ``bridge_gap``/``no_home`` key it started emitting means T210 landed the code
    without moving the declaration, which would leave the count in ``counted`` lying
    about a column that now carries data.
    """
    emitted = _normalized_level_keys()
    declared_live = {
        key for key in ENEMY_LEVEL_ALLOWLIST if ENEMY_KEY_HOMES[key].status in LIVE_KEY_STATUSES
    }
    declared_dead = {
        key for key in ENEMY_LEVEL_ALLOWLIST if ENEMY_KEY_HOMES[key].status not in LIVE_KEY_STATUSES
    }
    assert declared_live - emitted == set(), "declared live but the bridge does not emit it"
    assert emitted & declared_dead == set(), (
        "the bridge emits a key still declared dead -- update ENEMY_KEY_HOMES (§V113 (a))"
    )


def test_v113c_retired_key_yields_no_value_from_a_real_shaped_handbook() -> None:
    """``attackType`` passes §V29 (shape) + §V96 (tokens) and still stores nothing.

    It is PRESENT on every real handbook entry and ``null`` on all 1585 of them, so
    only a count over the built column can witness it -- §V113 (c). The parser must
    surface that as ``None``, never as a fabricated default.
    """
    handbook, database = _real_shaped_sources()
    parsed = parse_enemies(*normalize_enemy_sources(handbook, database))
    assert len(parsed) == 1
    assert ENEMY_KEY_HOMES["attackType"].status == "retired"
    assert parsed[0].attack_type is None
    # motionType has no handbook home either, but the bridge backfills it from the
    # enemy_database ``motion`` cell -- which is why it is declared live, not dead.
    assert parsed[0].motion_type == "WALK"


# --- §V113 (b): a registered rule that cannot fire is not coverage -------------


def _live_deciding_fields(rule_id: str) -> set[str]:
    """The rule's deciding fields whose backing source key is live on the build."""
    live: set[str] = set()
    for field in RULE_DECIDING_FIELDS[rule_id]:
        source_key = _DECIDING_FIELD_SOURCE_KEY.get(field)
        # A field with no source key is stage-table backed, not enemy-allowlist backed.
        if source_key is None or ENEMY_KEY_HOMES[source_key].status in LIVE_KEY_STATUSES:
            live.add(field)
    return live


def test_v113b_deciding_field_map_covers_every_registered_rule() -> None:
    registered = {rule.rule_id for rule in THREAT_RULES}
    assert set(RULE_DECIDING_FIELDS) == registered
    assert registered >= DEAD_BY_DATA_RULES, "stale DEAD_BY_DATA_RULES entry"
    assert set(DEAD_RULE_ARMS) <= registered, "stale DEAD_RULE_ARMS entry"


@pytest.mark.parametrize("rule_id", sorted(RULE_DECIDING_FIELDS))
def test_v113b_rule_is_live_or_declared_dead(rule_id: str) -> None:
    """Every rule either owns a live deciding field or is declared dead-by-data."""
    live = _live_deciding_fields(rule_id)
    if rule_id in DEAD_BY_DATA_RULES:
        assert not live, (
            f"{rule_id} is declared dead-by-data but now owns live deciding field(s) {live} "
            "-- drop it from DEAD_BY_DATA_RULES (§V113 (b))"
        )
        return
    if rule_id == "threat.ranged_arts":
        # B160 (a)+(b): dead today, but both homes are counted upstream, so it is a
        # bridge gap T210 closes -- not a rule to retire. Pinned so the exception
        # cannot quietly become permanent.
        assert ENEMY_KEY_HOMES["attackRange"].status == "bridge_gap"
        assert ENEMY_KEY_HOMES["attackType"].status == "retired"
        return
    assert live, (
        f"{rule_id} has no live deciding field and is not declared dead-by-data -- a "
        "registered rule that cannot fire is not coverage (§V113 (b))"
    )


@pytest.mark.parametrize("rule_id", sorted(DEAD_BY_DATA_RULES))
def test_v113b_declared_dead_rules_really_emit_nothing(rule_id: str) -> None:
    """Prove the declaration: on build-shaped input those rules produce no observation.

    The occurrence mirrors what the build actually stores -- every dead column NULL,
    every live one populated -- so this fails the moment a dead rule starts firing
    (its declaration is then wrong) as well as when it stays dead after being
    re-grounded.
    """
    occurrence = EnemyOccurrence(
        game_id="enemy_1007_slime",
        display_name="Originium Slug",
        motion_type="WALK",
        attack_type=None,  # 0/3879 on the build
        abilities=None,  # 0/4343
        total_count=4,
        defense=50,
        res=10,
        attack_range=None,  # 0/4343
        block_behavior=None,  # 0/4343
        first_spawn_time=1.0,
        last_spawn_time=9.0,
    )
    result = analyze_stage(
        StageThreatContext(
            server="en",
            stage_game_id="main_04-04",
            stage_code="4-4",
            occurrences=(occurrence,),
        )
    )
    assert not [obs for obs in result.observations if obs.rule_id == rule_id]


def test_v113b_aerial_dead_arm_is_declared() -> None:
    """``threat.aerial`` fires 507 en stages purely from ``motion_type`` (§V113 (b)).

    Its ``abilities`` arm cannot contribute while that column is 0/4343, so the arm
    is declared rather than left as an unreachable branch nobody counts.
    """
    assert DEAD_RULE_ARMS["threat.aerial"] == frozenset({"abilities"})
    assert ENEMY_KEY_HOMES["abilities"].status == "no_home"
    assert ENEMY_KEY_HOMES["motionType"].status == "live"
    assert _live_deciding_fields("threat.aerial") == {"motion_type"}
