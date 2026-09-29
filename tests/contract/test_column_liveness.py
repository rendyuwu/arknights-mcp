"""Column-liveness contract: fetched != normalized != STORED.

Six allowlisted enemy columns shipped 100% NULL on the real build
(``enemy_levels.{attack_range,block_behavior,targeting_json,immunities_json,
abilities_json}`` 0/4343 and ``enemies.attack_type`` 0/3879), which silently killed
four of the nine registered threat rules over 3264 en + 1200 cn stages. Nothing
below the build could witness it: a rule reading an always-NULL field takes its own
"field missing -> reduce confidence" arm and returns **clean**, and every
synthetic-fixture unit test stayed green because the fixture hands the parser the
already-normalized key the real bridge never emits.

The guard therefore cannot live in a rule test or a fixture import test. It is an
accounting one: :data:`ENEMY_KEY_HOMES` enumerates, per allowlisted key, the real
upstream home and the counted status of the column it feeds, and this module pins
that declaration against what the bridge actually does. It fails BOTH ways --

* a key added to either enemy allowlist with no declared home (the shape that
  admitted ``blockBehavior``/``abilities``, which exist in no real source at all);
* a declaration that drifts from the bridge, in either direction: a ``live`` key
  the bridge stopped emitting, or a ``bridge_gap``/``no_home`` key it started
  emitting without the declaration being updated (the fix must move the entry, not
  just the code).

Counts inside the declaration are @pinned ``413a81a3`` + build
``2026-07-29T065116Z-en-cn``; this module asserts the STRUCTURE those counts
describe, so it runs offline with no snapshot and no build.
"""

from __future__ import annotations

from typing import Any

import pytest

from arknights_mcp.analyzers.base import EnemyOccurrence, StageThreatContext
from arknights_mcp.analyzers.rules import (
    RETIRED_RULES,
    RULE_DECIDING_FIELDS,
    THREAT_RULES,
)
from arknights_mcp.analyzers.stage import analyze_stage
from arknights_mcp.importers.enemies import parse_enemies
from arknights_mcp.importers.enemy_normalization import normalize_enemy_sources
from arknights_mcp.importers.field_policy import (
    ENEMY_HANDBOOK_ALLOWLIST,
    ENEMY_KEY_HOMES,
    ENEMY_LEVEL_ALLOWLIST,
    LIVE_KEY_STATUSES,
)

_ALL_ENEMY_KEYS = ENEMY_HANDBOOK_ALLOWLIST | ENEMY_LEVEL_ALLOWLIST

#: Every status the declaration may carry.
_STATUSES = frozenset({"live", "bridge_gap", "retired", "no_home", "not_stored"})

#: Occurrence field an analyzer decides from -> the allowlisted source key that
#: fills it. Fields absent from this map come from the stage tables (route/tile/
#: spawn), not the enemy allowlist, and are demonstrably non-degenerate on the
#: build (lane_route 2453, pressure_spike 1124, tiles_deploy 241 en observations).
_DECIDING_FIELD_SOURCE_KEY = {
    "damage_types": "damageType",
    "attack_range": "attackRange",
    "attack_range_declared_none": "attackRangeDeclaredNone",
    "targeting": "targeting",
    "motion_type": "motionType",
    "defense": "def",
    "res": "res",
}


def _real_shaped_sources() -> tuple[dict[str, Any], dict[str, Any]]:
    """A REAL-shaped handbook + enemy_database carrying every declared home.

    Deliberately not ``tests/fixtures/stage_4_4_real``: that fixture was hand-trimmed
    and asserted a value domain upstream does not send (it set
    ``attackType: "physical"`` where all 1585 real entries send ``null``), which is the
    very thing that hid it -- the fixture was corrected, and this input stays
    independent of it so one edit cannot silence both. It mirrors the pinned snapshot --
    every ``m_defined``/``m_value`` cell wrapped as upstream wraps it, and every home
    named in :data:`ENEMY_KEY_HOMES` present with a value a bridge COULD map.

    That last clause is why there are TWO enemies. ``attackRangeDeclaredNone``
    is emitted only where ``rangeRadius`` is the ``-1.0`` no-radius sentinel, so an input
    carrying only a real radius could never witness it -- and a key declared ``live`` that
    the fixture cannot produce is the same shape again, one key over. The second enemy is
    that home: the sentinel AND a ``RANGED`` targeting token, which is the real 13 en /
    16 cn conflict population in miniature.
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
            },
            # The sentinel half of the population -- arts damage, no radius by the
            # source's own answer, and a targeting token that disagrees with it.
            "enemy_1404_msnip": {
                "enemyId": "enemy_1404_msnip",
                "name": "Sniper",
                "enemyLevel": "ELITE",
                "attackType": None,
                "damageType": ["MAGIC"],
            },
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
        ],
        "enemy_1404_msnip": [
            {
                "level": 0,
                "enemyData": {
                    "motion": {"m_defined": True, "m_value": "WALK"},
                    "applyWay": {"m_defined": True, "m_value": "RANGED"},
                    # The ANSWER the distance column strips out and the declared-none flag keeps.
                    "rangeRadius": {"m_defined": True, "m_value": -1.0},
                    "lifePointReduce": {"m_defined": True, "m_value": 1},
                    "attributes": {
                        "maxHp": {"m_defined": True, "m_value": 2400},
                        "atk": {"m_defined": True, "m_value": 500},
                        "def": {"m_defined": True, "m_value": 100},
                        "magicResistance": {"m_defined": True, "m_value": 20},
                        "moveSpeed": {"m_defined": True, "m_value": 0.9},
                        "baseAttackTime": {"m_defined": True, "m_value": 3.0},
                        "massLevel": {"m_defined": True, "m_value": 2},
                    },
                },
            }
        ],
    }
    return handbook, database


def _normalized_level_keys() -> set[str]:
    """Normalized level keys the bridge really emits from a real-shaped input.

    The UNION over every emitted level of every enemy, not one level of one enemy: a key
    can be conditional on the source's value (``attackRangeDeclaredNone`` exists only where
    the radius is the sentinel), and reading a single level would then report a live key as
    unemitted -- or hide one that is.
    """
    _, database_norm = normalize_enemy_sources(*_real_shaped_sources())
    return {
        key
        for entry in database_norm["enemies"].values()
        for level in entry["levels"]
        for key in level
    }


# --- every allowlisted key names its upstream home ----------------------------


def test_v113a_declaration_covers_exactly_the_enemy_allowlists() -> None:
    """No allowlisted enemy key may exist without a declared home, and vice versa."""
    declared = set(ENEMY_KEY_HOMES)
    assert declared - _ALL_ENEMY_KEYS == set(), "stale ENEMY_KEY_HOMES entry (key left allowlist)"
    assert _ALL_ENEMY_KEYS - declared == set(), (
        "allowlisted key with no declared upstream home -- a column NULL by construction "
        "is what the defect was; declare its home or its dead-by-data status"
    )


@pytest.mark.parametrize("key", sorted(_ALL_ENEMY_KEYS))
def test_v113a_home_declaration_is_well_formed(key: str) -> None:
    """``no_home`` <-> no upstream path, and every entry carries a counted status."""
    entry = ENEMY_KEY_HOMES[key]
    assert entry.status in _STATUSES, f"{key}: unknown status {entry.status!r}"
    assert entry.counted.strip(), f"{key}: status is asserted, not counted"
    if entry.status == "no_home":
        assert entry.home is None, f"{key}: declared dead-by-data yet names an upstream home"
    else:
        assert entry.home, f"{key}: status {entry.status!r} must name the upstream field it reads"


def test_v113a_bridge_emits_exactly_the_live_level_keys() -> None:
    """The bridge's real output must match the declaration, in BOTH directions.

    A ``live`` level key the bridge stopped emitting is the defect again; a
    ``bridge_gap``/``no_home`` key it started emitting means the fix landed the code
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
        "the bridge emits a key still declared dead -- update ENEMY_KEY_HOMES"
    )


def test_v113c_retired_key_yields_no_value_from_a_real_shaped_handbook() -> None:
    """``attackType`` passes shape + token validation and still stores nothing.

    It is PRESENT on every real handbook entry and ``null`` on all 1585 of them, so
    only a count over the built column can witness it. The parser must
    surface that as ``None``, never as a fabricated default.
    """
    handbook, database = _real_shaped_sources()
    parsed = parse_enemies(*normalize_enemy_sources(handbook, database))
    assert [enemy.game_id for enemy in parsed] == ["enemy_1007_slime", "enemy_1404_msnip"]
    assert ENEMY_KEY_HOMES["attackType"].status == "retired"
    assert parsed[0].attack_type is None
    # ...and the field that REPLACED it does yield a value from the same entry, which is
    # what makes "retired" the right status rather than "no_home".
    assert ENEMY_KEY_HOMES["damageType"].status == "live"
    assert parsed[0].damage_types == ["MAGIC"]
    # motionType has no handbook home either, but the bridge backfills it from the
    # enemy_database ``motion`` cell -- which is why it is declared live, not dead.
    assert parsed[0].motion_type == "WALK"


def test_v113a_revived_level_keys_reach_the_parsed_level() -> None:
    """The three revived keys travel bridge -> parser, not just bridge -> dict.

    The shape was a key that existed in the allowlist and in the column and nowhere
    in between, so a bridge-only assertion would have passed while the substrate stayed
    empty. This one ends where the INSERT reads.
    """
    handbook, database = _real_shaped_sources()
    parsed = parse_enemies(*normalize_enemy_sources(handbook, database))
    level = parsed[0].levels[0]
    assert level.attack_range == 2.5
    assert level.targeting == "RANGED"
    assert level.immunities == ["STUN"]
    # ...and the enemy whose radius the source DENIED reaches the INSERT carrying that
    # answer rather than an absence indistinguishable from silence.
    denied = parsed[1].levels[0]
    assert ENEMY_KEY_HOMES["attackRangeDeclaredNone"].status == "live"
    assert denied.attack_range is None  # the -1.0 mask is not a distance
    assert denied.attack_range_declared_none is True
    # The pair is what is decidable, so the slime's answer must read the other way.
    assert level.attack_range_declared_none is False


# --- a registered rule that cannot fire is not coverage -----------------------


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
    assert registered & RETIRED_RULES == set(), "a retired rule is registered again"


@pytest.mark.parametrize("rule_id", sorted(RULE_DECIDING_FIELDS))
def test_v113b_every_registered_rule_owns_a_live_deciding_field(rule_id: str) -> None:
    """A registered rule that cannot fire is not coverage.

    There is no exemption list any more. ``ranged_arts``'s substrate was revived from
    a counted upstream home and retired the three rules whose fields had no home at all,
    so every survivor must own a field the real build populates -- if a future rule is
    added against an empty column, this fails on the rule, not four milestones later on
    a zero-observation count.
    """
    assert _live_deciding_fields(rule_id), (
        f"{rule_id} has no live deciding field -- a registered rule that cannot fire is "
        "not coverage; revive its substrate or retire it"
    )


def test_v113b_retired_rules_are_not_registered() -> None:
    """The three dead-by-data rules are GONE, and their fields stay dead.

    Retiring them is only honest while the reason holds. If a source ever fills either
    field the declaration flips to ``live`` and this fails, which is the prompt to bring
    the rule back rather than leave a live column unread.
    """
    registered = {rule.rule_id for rule in THREAT_RULES}
    assert {
        "threat.block_bypass",
        "threat.crowd_control",
        "threat.support_aura",
    } == RETIRED_RULES
    assert not (RETIRED_RULES & registered)
    assert ENEMY_KEY_HOMES["blockBehavior"].status == "no_home"
    assert ENEMY_KEY_HOMES["abilities"].status == "no_home"


def test_v113b_a_build_shaped_occurrence_still_reports_its_live_rules() -> None:
    """The positive half: on an occurrence shaped like a real row, the survivors fire.

    Every value here is one the promoted build really stores for an arts flyer, so this
    is the assertion the defect could not have made before -- ``ranged_arts`` reading a radius
    the column now carries, rather than taking its "field missing" arm and
    returning clean.
    """
    occurrence = EnemyOccurrence(
        game_id="enemy_1105_drone",
        display_name="Recon Drone",
        motion_type="FLY",
        damage_types=("MAGIC",),
        total_count=4,
        defense=0,
        res=10,
        attack_range=2.5,
        targeting="RANGED",
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
    fired = {obs.rule_id for obs in result.observations}
    assert {"threat.aerial", "threat.ranged_arts"} <= fired
