"""The enemy SUBSTRATE, counted against real upstream.

Six allowlisted enemy columns shipped 100% NULL on every build ever promoted --
``enemy_levels.{attack_range,block_behavior,targeting,immunities_json,abilities_json}``
0/4343 and ``enemies.attack_type`` 0/3879 -- so four of the nine registered threat rules
emitted zero observations over 3264 en + 1200 cn stages. No rule-level test could see it:
a rule reading an always-NULL field takes its own "field missing -> reduce
confidence" arm and returns **clean**.

The three causes are three different questions about the SOURCE, so this module asks the
source (@pinned ``413a81a3``, en + cn)::

    rangeRadius   defined 1170/2036 en  -- of which 420 are the -1.0 no-radius sentinel
    applyWay      defined 1778/2036 en  -- MELEE 858 | RANGED 570 | NONE 251 | ALL 99
    <x>Immune     1194/2036 en levels define >=1 of the nine typed flags
    attackType    null 1585/1585 en     -- present, retired, empty
    damageType    [PHYSIC] 1022 | [MAGIC] 338 | [NO_DAMAGE] 180 | both 42 | [HEAL] 2

and then asserts the bridge really maps what it found. The build-side counterpart --
what the promoted corpus stores and whether the client-facing legends match it -- lives in
``tests/contract/test_enum_domain_coverage.py``, and the offline accounting of key ->
upstream home lives in ``tests/contract/test_column_liveness.py``.

The last group is the real point: the false positives a naive revival would
have shipped. 383 EN enemies are arts-capable, 77 of them carry no radius at all, and 55 of
THOSE declare ``applyWay: MELEE``. A ranged-arts rule that fell from "no radius" straight to
the inference would publish all 55 as ranged threats against the source's own word, which
is why reading ``targeting`` first is a pinned property and not a style choice.

CI-only: needs network, gated behind ``ARKMCP_LIVE_UPSTREAM`` like the other
live-upstream guards. Nothing fetched is persisted (code-only distribution).
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import pytest
from tests.support import (
    LIVE_UPSTREAM_SKIP_REASON,
    arknights_assets_base_url,
    fetch_upstream_bytes,
    live_upstream_disabled,
)

from arknights_mcp.analyzers.base import EnemyOccurrence, StageThreatContext
from arknights_mcp.analyzers.rules.ranged_arts import RULE_ID as RANGED_ARTS_ID
from arknights_mcp.analyzers.rules.ranged_arts import RangedArtsRule
from arknights_mcp.importers.enemy_normalization import normalize_enemy_sources
from arknights_mcp.importers.field_policy import ENEMY_KEY_HOMES
from arknights_mcp.mcp.tools._enum_legend import ENUM_LEGENDS

pytestmark = pytest.mark.skipif(live_upstream_disabled(), reason=LIVE_UPSTREAM_SKIP_REASON)

SERVERS = ("en", "cn")

#: The nine typed immunity flags, as the source names them.
IMMUNE_KEYS = (
    "stunImmune",
    "silenceImmune",
    "sleepImmune",
    "frozenImmune",
    "levitateImmune",
    "disarmedCombatImmune",
    "fearedImmune",
    "palsyImmune",
    "attractImmune",
)

#: Non-degenerate floors. Every one is far under the pinned count (en: 1170 defined
#: radii, 1778 defined applyWay cells, 1194 levels with an immunity flag, 1585 handbook
#: entries, 381 arts-capable). A corpus that stopped carrying a field would otherwise let
#: every assertion below pass vacuously -- the exact failure mode the fix was about.
MIN_DEFINED_RANGE = 400
MIN_DEFINED_TARGETING = 800
MIN_LEVELS_WITH_IMMUNITY = 400
MIN_HANDBOOK_ENTRIES = 500
MIN_ARTS_ENEMIES = 100


@lru_cache(maxsize=len(SERVERS))
def _enemy_database(server: str) -> dict[str, Any]:
    """The pinned ``enemy_database`` for ``server``; fetched once, never written."""
    url = f"{arknights_assets_base_url(server)}/gamedata/levels/enemydata/enemy_database.json"
    table = json.loads(fetch_upstream_bytes(url).decode("utf-8"))
    assert isinstance(table, dict) and table, f"{server} enemy_database is not a populated dict"
    return table


@lru_cache(maxsize=len(SERVERS))
def _enemy_handbook(server: str) -> dict[str, Any]:
    """The pinned ``enemy_handbook_table`` entries for ``server`` (never written)."""
    url = f"{arknights_assets_base_url(server)}/gamedata/excel/enemy_handbook_table.json"
    table = json.loads(fetch_upstream_bytes(url).decode("utf-8"))
    entries = table.get("enemyData") if isinstance(table, dict) else None
    assert isinstance(entries, dict) and entries, f"{server} handbook carries no enemyData"
    return entries


def _cells(server: str, key: str) -> list[Any]:
    """Every ``enemyData.<key>`` cell across every level entry, wrappers intact."""
    out: list[Any] = []
    for levels in _enemy_database(server).values():
        for level in levels if isinstance(levels, list) else []:
            data = level.get("enemyData") if isinstance(level, dict) else None
            if isinstance(data, dict) and key in data:
                out.append(data[key])
    return out


def _defined(cell: Any) -> tuple[bool, Any]:
    """A ``{m_defined, m_value}`` cell -> ``(defined, value)``."""
    if isinstance(cell, dict):
        return bool(cell.get("m_defined", True)), cell.get("m_value")
    return True, cell


# --- (a) the bridge gap: upstream carries all three ----------------------


@pytest.mark.parametrize("server", SERVERS)
def test_upstream_carries_a_range_radius_and_a_negative_sentinel(server: str) -> None:
    """``rangeRadius`` is real AND carries a sentinel -- both halves are load-bearing.

    The defined count is what makes the column worth filling; the negative arm is what
    the distance rule keeps OUT. If upstream ever stopped using ``-1.0`` for "no radius", the
    sentinel strip would be silently dropping real data, so the value itself is pinned,
    not just its sign.
    """
    values = [value for defined, value in map(_defined, _cells(server, "rangeRadius")) if defined]
    assert len(values) >= MIN_DEFINED_RANGE, f"{server}: only {len(values)} defined rangeRadius"
    negatives = {value for value in values if isinstance(value, int | float) and value < 0}
    assert negatives == {-1.0}, f"{server}: the no-radius sentinel moved: {sorted(negatives)}"
    real = [value for value in values if isinstance(value, int | float) and value >= 0]
    assert real, f"{server}: every defined rangeRadius is the sentinel"
    assert ENEMY_KEY_HOMES["attackRange"].status == "live"


@pytest.mark.parametrize("server", SERVERS)
def test_upstream_targeting_token_set_matches_the_published_legend(server: str) -> None:
    """``applyWay``'s real token set is the domain ``targeting``'s legend publishes."""
    tokens = {value for defined, value in map(_defined, _cells(server, "applyWay")) if defined}
    defined_count = sum(1 for defined, _ in map(_defined, _cells(server, "applyWay")) if defined)
    assert defined_count >= MIN_DEFINED_TARGETING, f"{server}: only {defined_count} applyWay cells"
    assert tokens == set(ENUM_LEGENDS["targeting"]), f"{server}: targeting domain moved: {tokens}"
    assert ENEMY_KEY_HOMES["targeting"].status == "live"


@pytest.mark.parametrize("server", SERVERS)
def test_upstream_carries_the_nine_typed_immunity_flags(server: str) -> None:
    """All nine flags exist, are typed booleans, and are SET on real enemies."""
    with_any_defined = 0
    ever_true: set[str] = set()
    for levels in _enemy_database(server).values():
        for level in levels if isinstance(levels, list) else []:
            data = level.get("enemyData") if isinstance(level, dict) else None
            attributes = data.get("attributes") if isinstance(data, dict) else None
            if not isinstance(attributes, dict):
                continue
            defined_here = False
            for key in IMMUNE_KEYS:
                if key not in attributes:
                    continue
                defined, value = _defined(attributes[key])
                if not defined:
                    continue
                assert isinstance(value, bool), f"{server} {key} is not a bool: {value!r}"
                defined_here = True
                if value:
                    ever_true.add(key)
            with_any_defined += int(defined_here)
    assert with_any_defined >= MIN_LEVELS_WITH_IMMUNITY, f"{server}: {with_any_defined} levels"
    # A flag no enemy ever sets would make its legend entry a claim about nothing.
    assert ever_true, f"{server}: no enemy is immune to anything"
    assert ENEMY_KEY_HOMES["immunities"].status == "live"


@pytest.mark.parametrize("server", SERVERS)
def test_the_bridge_emits_all_three_from_a_real_entry(server: str) -> None:
    """End of the substrate chain: upstream -> bridge, on a REAL enemy, not a fixture.

    The gap was three keys that existed upstream and in the allowlist and nowhere in
    between. Counting upstream alone would have reproduced exactly that blind spot, so
    this runs the real records through the real bridge.
    """
    database = _enemy_database(server)
    # An enemy whose level 0 defines a real (non-sentinel) radius, so all three keys are
    # exercised on one record.
    picked = None
    for game_id, levels in database.items():
        data = levels[0].get("enemyData") if levels and isinstance(levels[0], dict) else None
        if not isinstance(data, dict):
            continue
        defined, value = _defined(data.get("rangeRadius"))
        if defined and isinstance(value, int | float) and value >= 1.0:
            picked = {game_id: levels}
            break
    assert picked is not None, f"{server}: no enemy carries a real attack radius"

    _, database_norm = normalize_enemy_sources({"enemyData": {}}, picked)
    level = next(iter(database_norm["enemies"].values()))["levels"][0]
    assert isinstance(level["attackRange"], int | float) and level["attackRange"] >= 1.0
    assert level["targeting"] in ENUM_LEGENDS["targeting"]
    assert set(level.get("immunities", [])) <= set(ENUM_LEGENDS["immunities"])


@pytest.mark.parametrize("server", SERVERS)
def test_the_bridge_never_stores_the_sentinel_as_a_distance(server: str) -> None:
    """``-1.0`` means "no radius"; a distance column must not carry it."""
    sentinel_ids = {
        game_id: levels
        for game_id, levels in _enemy_database(server).items()
        if levels
        and isinstance(levels[0], dict)
        and _defined((levels[0].get("enemyData") or {}).get("rangeRadius")) == (True, -1.0)
    }
    assert sentinel_ids, f"{server}: no enemy carries the sentinel, so nothing is proven"
    _, database_norm = normalize_enemy_sources({"enemyData": {}}, sentinel_ids)
    for entry in database_norm["enemies"].values():
        for level in entry["levels"]:
            assert "attackRange" not in level, "the -1.0 sentinel reached the distance column"


# --- (b) the retired value domain --------------------------------------------


@pytest.mark.parametrize("server", SERVERS)
def test_attack_type_is_present_and_empty_while_damage_type_carries_the_fact(server: str) -> None:
    """A field can pass the shape check and the value check and still be empty.

    ``attackType`` is on every entry and ``null`` on every entry, which is why only a
    count witnesses it. The day upstream fills it again, this fails and the retirement is
    re-decided rather than left as a stale declaration.
    """
    entries = _enemy_handbook(server)
    assert len(entries) >= MIN_HANDBOOK_ENTRIES, f"{server}: only {len(entries)} handbook entries"
    with_key = [entry for entry in entries.values() if "attackType" in entry]
    assert with_key, f"{server}: attackType has left the schema; ENEMY_KEY_HOMES is now stale"
    non_null = [entry for entry in with_key if entry["attackType"] is not None]
    assert not non_null, f"{server}: attackType is populated again on {len(non_null)} entries"
    assert ENEMY_KEY_HOMES["attackType"].status == "retired"

    tokens = {
        token
        for entry in entries.values()
        for token in (entry.get("damageType") or [])
        if isinstance(entry.get("damageType"), list)
    }
    assert tokens, f"{server}: damageType carries nothing either"
    assert tokens <= set(ENUM_LEGENDS["damage_types"]), f"{server}: undocumented token: {tokens}"
    assert ENEMY_KEY_HOMES["damageType"].status == "live"


def test_damage_type_is_a_list_because_real_enemies_deal_two_kinds() -> None:
    """The shape argument for the list column, measured rather than asserted.

    42 EN enemies deal PHYSIC *and* MAGIC. A scalar column could not have carried that
    even when ``attackType`` was populated, so the retirement is not what made the old
    column the wrong shape -- it was always the wrong shape.
    """
    dual = [
        game_id
        for game_id, entry in _enemy_handbook("en").items()
        if len(entry.get("damageType") or []) > 1
    ]
    assert len(dual) >= 10, f"only {len(dual)} multi-damage enemies; the list column is moot"


# --- the false positives a naive revival would have shipped -------------------


def _arts_population(server: str) -> list[tuple[str, float | None, str | None]]:
    """``(game_id, base radius, base targeting)`` for every arts-capable enemy.

    "Arts-capable" is read off ``damageType`` membership, exactly as the rule reads it --
    never ``damageType[0]``, which is ``PHYSIC`` for the dual enemies.
    """
    database = _enemy_database(server)
    out: list[tuple[str, float | None, str | None]] = []
    for game_id, entry in _enemy_handbook(server).items():
        kinds = entry.get("damageType") or []
        if "MAGIC" not in kinds:
            continue
        levels = database.get(game_id) or []
        base = next(
            (lvl for lvl in levels if isinstance(lvl, dict) and lvl.get("level") == 0),
            levels[0] if levels else None,
        )
        data = base.get("enemyData") if isinstance(base, dict) else None
        data = data if isinstance(data, dict) else {}
        range_defined, range_value = _defined(data.get("rangeRadius"))
        targeting_defined, targeting_value = _defined(data.get("applyWay"))
        radius = range_value if range_defined and isinstance(range_value, int | float) else None
        targeting = targeting_value if targeting_defined else None
        out.append((game_id, radius, targeting))
    return out


@pytest.mark.parametrize("server", SERVERS)
def test_arts_enemies_without_a_radius_but_declaring_melee_really_exist(server: str) -> None:
    """The 55-false-positive population is real, and it is not a rounding error.

    This is the census that decided the rule's field ORDER. If it ever empties, the
    ``targeting``-before-inference branch is no longer load-bearing and the guard below
    would be testing nothing -- so the population is asserted before the behaviour is.
    """
    population = _arts_population(server)
    assert len(population) >= MIN_ARTS_ENEMIES, f"{server}: only {len(population)} arts enemies"
    no_radius = [row for row in population if row[1] is None or row[1] < 0]
    melee_declared = [row for row in no_radius if row[2] == "MELEE"]
    assert melee_declared, (
        f"{server}: no arts enemy lacks a radius while declaring MELEE -- the census that "
        "decided the rule's field order no longer holds"
    )
    # ...and the authoritative arm still has a population of its own.
    assert [row for row in population if row[1] is not None and row[1] >= 1.0]


@pytest.mark.parametrize("server", SERVERS)
def test_the_rule_publishes_none_of_the_melee_declared_arts_enemies(server: str) -> None:
    """Behaviour, over the REAL population: every melee-declared arts enemy is skipped.

    Run one at a time so a single misclassification cannot hide inside a stage that other
    enemies made fire anyway.
    """
    rule = RangedArtsRule()
    for game_id, radius, targeting in _arts_population(server):
        if targeting != "MELEE" or (radius is not None and radius >= 0):
            continue
        result = rule.evaluate(
            StageThreatContext(
                server=server,
                stage_game_id="main_04-04",
                stage_code="4-4",
                occurrences=(
                    EnemyOccurrence(
                        game_id=game_id,
                        display_name=None,
                        motion_type="WALK",
                        damage_types=("MAGIC",),
                        total_count=1,
                        attack_range=None,
                        targeting=targeting,
                    ),
                ),
            )
        )
        assert result.observation is None, (
            f"{server} {game_id}: reported as a ranged-arts threat while the source says "
            f"applyWay={targeting} and gives no radius"
        )
    assert rule.rule_id == RANGED_ARTS_ID
