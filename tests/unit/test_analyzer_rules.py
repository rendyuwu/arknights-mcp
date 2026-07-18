"""T39: the M3 deterministic rule engine (§V6, §V7, §V26, §V35).

One test group per rule (block-bypass, def/res skew, ranged-arts, support-aura,
pressure-spike, lane/route, tiles/deploy, crowd-control) plus cross-cutting guards:

* every emitted observation carries the five §V6 fields;
* rules decide from typed fields only and handle missing / conflicting fields per
  §V26 (reduced confidence + limitation; omit + warn);
* an enemy seen at several level variants is counted once (§V35);
* no observation uses prescriptive "mandatory"/"best"/"must-use" language (§V7);
* the engine is deterministic regardless of input order.

The M0 aerial rule keeps its own suite (``test_analyzer_stage_aerial``); here it
only appears where a combined scenario needs it (the block-bypass exclusion).
"""

from __future__ import annotations

from typing import Any

from arknights_mcp.analyzers import (
    EnemyOccurrence,
    Observation,
    StageThreatContext,
    StageTiles,
    analyze_stage,
)
from arknights_mcp.analyzers.rules import THREAT_RULES
from arknights_mcp.analyzers.rules.block_bypass import RULE_ID as BLOCK_BYPASS_ID
from arknights_mcp.analyzers.rules.crowd_control import RULE_ID as CROWD_CONTROL_ID
from arknights_mcp.analyzers.rules.def_res_skew import RULE_ID as DEF_RES_SKEW_ID
from arknights_mcp.analyzers.rules.lane_route import RULE_ID as LANE_ROUTE_ID
from arknights_mcp.analyzers.rules.pressure_spike import RULE_ID as PRESSURE_SPIKE_ID
from arknights_mcp.analyzers.rules.ranged_arts import RULE_ID as RANGED_ARTS_ID
from arknights_mcp.analyzers.rules.support_aura import RULE_ID as SUPPORT_AURA_ID
from arknights_mcp.analyzers.rules.tiles_deploy import RULE_ID as TILES_DEPLOY_ID


def occ(game_id: str, **kw: Any) -> EnemyOccurrence:
    """An enemy occurrence with inert defaults (ground, physical, no abilities) so a
    test opts *into* exactly the typed fields the rule under test reads."""
    base: dict[str, Any] = {
        "display_name": None,
        "motion_type": "WALK",
        "attack_type": "physical",
        "abilities": (),
        "total_count": 1,
    }
    base.update(kw)
    return EnemyOccurrence(game_id=game_id, **base)


def ctx(
    *occurrences: EnemyOccurrence,
    stage_code: str = "1-1",
    route_count: int | None = None,
    tiles: StageTiles | None = None,
) -> StageThreatContext:
    return StageThreatContext(
        server="en",
        stage_code=stage_code,
        occurrences=tuple(occurrences),
        route_count=route_count,
        tiles=tiles,
    )


def _obs_by_tag(result: Any) -> dict[str, Observation]:
    return {o.tag: o for o in result.observations}


def _assert_v6_fields(o: Observation) -> None:
    """§V6: every observation carries rule_id + evidence + confidence + limitations
    + analyzer_version, well-formed."""
    assert o.rule_id
    assert o.analyzer_version
    assert 0.0 <= o.confidence <= 1.0
    assert isinstance(o.evidence, tuple) and o.evidence  # non-empty typed evidence
    assert isinstance(o.limitations, tuple)


# --- block-bypass -------------------------------------------------------------


def test_block_bypass_fires_on_typed_block_behavior() -> None:
    result = analyze_stage(ctx(occ("enemy_burrow", block_behavior="unblockable_ground")))
    obs = _obs_by_tag(result)["block_bypass"]
    assert obs.rule_id == BLOCK_BYPASS_ID
    _assert_v6_fields(obs)
    assert obs.confidence >= 0.85  # authoritative typed block_behavior
    assert obs.evidence[0].field == "block_behavior"


def test_block_bypass_excludes_flyers() -> None:
    # §V26/dedup: a flyer marked unblockable is reported by the aerial rule, not
    # double-counted here -> only the aerial observation appears.
    drone = occ(
        "enemy_drone",
        motion_type="FLY",
        abilities=("aerial",),
        block_behavior="unblockable_flying",
    )
    tags = _obs_by_tag(analyze_stage(ctx(drone)))
    assert "aerial" in tags
    assert "block_bypass" not in tags


def test_block_bypass_ability_only_reduces_confidence_and_limits() -> None:
    result = analyze_stage(ctx(occ("enemy_sneak", abilities=("teleport",), block_behavior=None)))
    obs = _obs_by_tag(result)["block_bypass"]
    assert obs.confidence < 0.85  # inferred from ability, not the typed field
    assert obs.evidence[0].field == "abilities"
    assert any("block_behavior missing" in lim for lim in obs.limitations)


def test_block_bypass_conflict_omits_and_warns() -> None:
    # typed block_behavior says blockable but an ability claims bypass -> §V26 omit + warn.
    result = analyze_stage(
        ctx(occ("enemy_conf", abilities=("unblockable",), block_behavior="blockable"))
    )
    assert "block_bypass" not in _obs_by_tag(result)
    assert any("conflict" in w for w in result.warnings)


def test_block_bypass_silent_on_plain_blockable_enemy() -> None:
    assert analyze_stage(ctx(occ("enemy_plain", block_behavior="blockable"))).observations == ()


# --- def/res skew -------------------------------------------------------------


def test_def_res_skew_fires_on_high_armor_low_res() -> None:
    result = analyze_stage(ctx(occ("enemy_wall", defense=800, res=0)))
    obs = _obs_by_tag(result)["def_res_skew"]
    assert obs.rule_id == DEF_RES_SKEW_ID
    _assert_v6_fields(obs)
    assert "def=800" in obs.evidence[0].value


def test_def_res_skew_fires_on_high_res_low_armor() -> None:
    obs = _obs_by_tag(analyze_stage(ctx(occ("enemy_mystic", defense=100, res=70))))["def_res_skew"]
    assert obs.evidence[0].note is not None and "physical" in obs.evidence[0].note


def test_def_res_skew_balanced_enemy_does_not_fire() -> None:
    # High in both axes is tanky, not skewed -> no conclusion.
    assert analyze_stage(ctx(occ("enemy_tank", defense=800, res=80))).observations == ()


def test_def_res_skew_partial_stats_recorded_as_limitation() -> None:
    # §V26: a fully-typed skewed enemy fires; a coexisting enemy with only one stat
    # typed cannot be concluded from -> its missing stat is surfaced as a limitation
    # on the observation, not silently ignored.
    result = analyze_stage(
        ctx(
            occ("enemy_wall", defense=800, res=0),
            occ("enemy_half", defense=800, res=None),
        )
    )
    obs = _obs_by_tag(result)["def_res_skew"]
    assert {e.ref for e in obs.evidence} == {"enemy_wall"}  # only the fully-typed enemy concluded
    assert any("enemy_half" in lim and "res missing" in lim for lim in obs.limitations)


# --- ranged-arts --------------------------------------------------------------


def test_ranged_arts_fires_on_arts_at_range() -> None:
    obs = _obs_by_tag(
        analyze_stage(ctx(occ("enemy_caster", attack_type="magical", attack_range=2.5)))
    )["ranged_arts"]
    assert obs.rule_id == RANGED_ARTS_ID
    _assert_v6_fields(obs)
    assert obs.confidence >= 0.9
    assert obs.evidence[0].field == "attack_range"


def test_ranged_arts_missing_range_infers_at_reduced_confidence() -> None:
    obs = _obs_by_tag(analyze_stage(ctx(occ("enemy_arts", attack_type="arts", attack_range=None))))[
        "ranged_arts"
    ]
    assert obs.confidence < 0.9
    assert any("attack_range missing" in lim for lim in obs.limitations)


def test_ranged_arts_melee_arts_does_not_fire() -> None:
    # arts damage but no reach -> not a ranged-arts threat.
    assert (
        analyze_stage(ctx(occ("enemy_bruiser", attack_type="arts", attack_range=0.0))).observations
        == ()
    )


def test_ranged_arts_physical_does_not_fire() -> None:
    assert (
        analyze_stage(ctx(occ("enemy_gun", attack_type="physical", attack_range=3.0))).observations
        == ()
    )


# --- support-aura -------------------------------------------------------------


def test_support_aura_fires_on_typed_ability() -> None:
    obs = _obs_by_tag(analyze_stage(ctx(occ("enemy_medic", abilities=("heal_allies",)))))[
        "support_aura"
    ]
    assert obs.rule_id == SUPPORT_AURA_ID
    _assert_v6_fields(obs)


def test_support_aura_typed_only_ignores_name() -> None:
    # Name screams support but no typed aura ability -> §V26 forbids matching prose.
    trap = occ("enemy_named", display_name="Battlefield Support Aura Healer", abilities=())
    assert analyze_stage(ctx(trap)).observations == ()


def test_support_aura_counts_distinct_enemy_once_across_variants() -> None:
    # §V35: one enemy at two variants -> two evidence items, one distinct type.
    v0 = occ("enemy_medic", abilities=("heal_allies",))
    v1 = occ("enemy_medic", abilities=("aura",))
    obs = _obs_by_tag(analyze_stage(ctx(v0, v1)))["support_aura"]
    assert len(obs.evidence) == 2
    assert {e.ref for e in obs.evidence} == {"enemy_medic"}
    assert "1 enemy type" in obs.summary


# --- pressure-spike -----------------------------------------------------------


def test_pressure_spike_fires_on_tight_burst() -> None:
    swarm = occ("enemy_swarm", total_count=8, first_spawn_time=2.0, last_spawn_time=10.0)
    obs = _obs_by_tag(analyze_stage(ctx(swarm)))["pressure_spike"]
    assert obs.rule_id == PRESSURE_SPIKE_ID
    _assert_v6_fields(obs)
    # B30/§V39: first/last_spawn_time is fragment-relative preDelay aggregated across
    # waves, not elapsed time -> the rule no longer asserts a high-confidence burst; it
    # fires at reduced confidence and stamps the fragment-relative limitation.
    assert obs.confidence < 0.8
    assert any("fragment-relative" in lim for lim in obs.limitations)


def test_pressure_spike_fragment_relative_window_reports_with_limitation() -> None:
    # B30/§V39: an enemy trickled across >=6 fragments each at a low per-fragment preDelay
    # collapses to a ~0 computed window (first == last). The rule must NOT conclude a
    # confident burst from that cross-wave min/max; it reports at reduced confidence with
    # a limitation that the window is fragment-relative and may overstate the burst.
    trickle = occ("enemy_frag", total_count=6, first_spawn_time=3.0, last_spawn_time=3.0)
    obs = _obs_by_tag(analyze_stage(ctx(trickle)))["pressure_spike"]
    _assert_v6_fields(obs)
    assert obs.confidence < 0.8  # no confident burst from a fragment-relative window
    assert any("fragment-relative" in lim and "overstate burst" in lim for lim in obs.limitations)


def test_pressure_spike_spread_out_does_not_fire() -> None:
    spread = occ("enemy_trickle", total_count=8, first_spawn_time=0.0, last_spawn_time=90.0)
    assert analyze_stage(ctx(spread)).observations == ()


def test_pressure_spike_low_count_does_not_fire() -> None:
    few = occ("enemy_few", total_count=3, first_spawn_time=0.0, last_spawn_time=2.0)
    assert analyze_stage(ctx(few)).observations == ()


def test_pressure_spike_missing_window_reduces_confidence_and_limits() -> None:
    blind = occ("enemy_blind", total_count=8, first_spawn_time=None, last_spawn_time=None)
    obs = _obs_by_tag(analyze_stage(ctx(blind)))["pressure_spike"]
    assert obs.confidence < 0.8
    assert any("spawn timing missing" in lim for lim in obs.limitations)


# --- lane/route ---------------------------------------------------------------


def test_lane_route_fires_on_multiple_routes() -> None:
    obs = _obs_by_tag(analyze_stage(ctx(occ("enemy_a"), route_count=3)))["lane_route"]
    assert obs.rule_id == LANE_ROUTE_ID
    _assert_v6_fields(obs)
    assert obs.evidence[0].value == 3
    assert "3 distinct enemy routes" in obs.summary


def test_lane_route_single_route_does_not_fire() -> None:
    assert analyze_stage(ctx(occ("enemy_a"), route_count=1)).observations == ()


def test_lane_route_absent_route_data_does_not_fire() -> None:
    assert analyze_stage(ctx(occ("enemy_a"), route_count=None)).observations == ()


# --- tiles/deploy -------------------------------------------------------------


def test_tiles_deploy_fires_on_scarce_surface() -> None:
    tiles = StageTiles(total=24, buildable_melee=2, buildable_ranged=1)
    obs = _obs_by_tag(analyze_stage(ctx(occ("enemy_a"), tiles=tiles)))["tiles_deploy"]
    assert obs.rule_id == TILES_DEPLOY_ID
    _assert_v6_fields(obs)


def test_tiles_deploy_ample_surface_does_not_fire() -> None:
    tiles = StageTiles(total=40, buildable_melee=12, buildable_ranged=10)
    assert analyze_stage(ctx(occ("enemy_a"), tiles=tiles)).observations == ()


def test_tiles_deploy_below_floor_is_skipped() -> None:
    # A stub grid (too few tiles) is not judged constrained.
    tiles = StageTiles(total=4, buildable_melee=0, buildable_ranged=0)
    assert analyze_stage(ctx(occ("enemy_a"), tiles=tiles)).observations == ()


def test_tiles_deploy_absent_tiles_does_not_fire() -> None:
    assert analyze_stage(ctx(occ("enemy_a"), tiles=None)).observations == ()


# --- crowd-control ------------------------------------------------------------


def test_crowd_control_fires_on_typed_ability() -> None:
    obs = _obs_by_tag(analyze_stage(ctx(occ("enemy_stun", abilities=("stun",)))))["crowd_control"]
    assert obs.rule_id == CROWD_CONTROL_ID
    _assert_v6_fields(obs)


def test_crowd_control_silent_without_cc_ability() -> None:
    assert analyze_stage(ctx(occ("enemy_calm", abilities=("aerial",)))).observations == ()


# --- cross-cutting guards -----------------------------------------------------

#: Prescriptive language §V7 forbids in an observation (it must state facts, not
#: prescribe a "mandatory"/"best"/"must-use" answer).
_FORBIDDEN = ("mandatory", "must use", "must bring", "best operator", "always bring", "recommended")


def _every_observation() -> list[Observation]:
    """Fire every rule once so the guards see all nine observation shapes."""
    scenario = ctx(
        occ("enemy_drone", motion_type="FLY", abilities=("aerial",), total_count=2),
        occ("enemy_burrow", block_behavior="unblockable_ground"),
        occ("enemy_wall", defense=800, res=0),
        occ("enemy_caster", attack_type="magical", attack_range=2.5),
        occ("enemy_medic", abilities=("heal_allies",)),
        occ("enemy_swarm", total_count=8, first_spawn_time=2.0, last_spawn_time=10.0),
        occ("enemy_stun", abilities=("stun",)),
        route_count=3,
        tiles=StageTiles(total=24, buildable_melee=2, buildable_ranged=1),
    )
    return list(analyze_stage(scenario).observations)


def test_all_rules_can_fire_together() -> None:
    tags = {o.tag for o in _every_observation()}
    assert tags == {
        "aerial",
        "block_bypass",
        "def_res_skew",
        "ranged_arts",
        "support_aura",
        "pressure_spike",
        "lane_route",
        "tiles_deploy",
        "crowd_control",
    }


def test_every_observation_carries_v6_fields() -> None:
    for obs in _every_observation():
        _assert_v6_fields(obs)


def test_no_observation_uses_prescriptive_language() -> None:
    # §V7: observations state capability/threat facts, never a prescriptive verdict.
    for obs in _every_observation():
        blob = f"{obs.title} {obs.summary}".lower()
        for term in _FORBIDDEN:
            assert term not in blob, f"{obs.rule_id} uses forbidden term {term!r}"


def test_engine_deterministic_regardless_of_input_order() -> None:
    a = occ("enemy_wall", defense=800, res=0)
    b = occ("enemy_caster", attack_type="magical", attack_range=2.5)
    assert analyze_stage(ctx(a, b)) == analyze_stage(ctx(b, a))


def test_registry_covers_every_named_rule() -> None:
    # §T39 names nine rules; the registry exposes exactly those rule_ids.
    ids = {rule.rule_id for rule in THREAT_RULES}
    assert ids == {
        "threat.aerial",
        BLOCK_BYPASS_ID,
        DEF_RES_SKEW_ID,
        RANGED_ARTS_ID,
        SUPPORT_AURA_ID,
        PRESSURE_SPIKE_ID,
        LANE_ROUTE_ID,
        TILES_DEPLOY_ID,
        CROWD_CONTROL_ID,
    }
