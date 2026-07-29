"""T39: the M3 deterministic rule engine (§V6, §V7, §V26, §V35).

One test group per rule (def/res skew, ranged-arts, pressure-spike, lane/route,
tiles/deploy) plus cross-cutting guards:

* every emitted observation carries the five §V6 fields;
* rules decide from typed fields only and handle missing / conflicting fields per
  §V26 (reduced confidence + limitation; omit + warn);
* an enemy seen at several level variants is counted once (§V35);
* no observation uses prescriptive "mandatory"/"best"/"must-use" language (§V7);
* the engine is deterministic regardless of input order.

The M0 aerial rule keeps its own suite (``test_analyzer_stage_aerial``); here it
appears only in the combined scenario.

§T210 (c) retired block-bypass, crowd-control and support-aura, so their groups are
gone with them: every field those rules decided from is absent from every real source
(B160 (c)), and the tests that passed were feeding the rules synthetic values no build
could produce. ``tests/contract/test_column_liveness.py`` now pins the retirement.
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
from arknights_mcp.analyzers.rules import RETIRED_RULES, THREAT_RULES
from arknights_mcp.analyzers.rules.def_res_skew import RULE_ID as DEF_RES_SKEW_ID
from arknights_mcp.analyzers.rules.lane_route import RULE_ID as LANE_ROUTE_ID
from arknights_mcp.analyzers.rules.pressure_spike import RULE_ID as PRESSURE_SPIKE_ID
from arknights_mcp.analyzers.rules.ranged_arts import RULE_ID as RANGED_ARTS_ID
from arknights_mcp.analyzers.rules.tiles_deploy import RULE_ID as TILES_DEPLOY_ID


def occ(game_id: str, **kw: Any) -> EnemyOccurrence:
    """An enemy occurrence with inert defaults (ground, physical damage) so a test opts
    *into* exactly the typed fields the rule under test reads.

    ``damage_types`` uses the source's OWN token (``PHYSIC``), not the invented
    ``"physical"`` these tests used to pass: that string appears on 0 of 1585 real
    handbook entries, and a default no build can produce is how B160 stayed invisible.
    """
    base: dict[str, Any] = {
        "display_name": None,
        "motion_type": "WALK",
        "damage_types": ("PHYSIC",),
        "total_count": 1,
    }
    base.update(kw)
    return EnemyOccurrence(game_id=game_id, **base)


def ctx(
    *occurrences: EnemyOccurrence,
    stage_game_id: str = "main_01-01",
    stage_code: str = "1-1",
    route_count: int | None = None,
    tiles: StageTiles | None = None,
) -> StageThreatContext:
    """A stage context whose game_id and stage_code DIFFER on purpose (§V68/B136): a
    stage-level evidence row must ref the unique game_id, and a test where the two
    strings are equal cannot tell the two apart."""
    return StageThreatContext(
        server="en",
        stage_game_id=stage_game_id,
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


# --- def/res skew -------------------------------------------------------------


def test_def_res_skew_fires_on_high_armor_low_res() -> None:
    result = analyze_stage(ctx(occ("enemy_wall", defense=800, res=0)))
    obs = _obs_by_tag(result)["def_res_skew"]
    assert obs.rule_id == DEF_RES_SKEW_ID
    _assert_v6_fields(obs)
    # §V101/B137: one fact per row -- two separately-typed stats, never one row with an
    # invented "def/res" path and a packed "def=800,res=0" string the client must split.
    assert [(e.field, e.value) for e in obs.evidence] == [("def", 800), ("res", 0)]
    assert all(isinstance(e.value, int) for e in obs.evidence)


def test_def_res_skew_fires_on_high_res_low_armor() -> None:
    obs = _obs_by_tag(analyze_stage(ctx(occ("enemy_mystic", defense=100, res=70))))["def_res_skew"]
    assert [(e.field, e.value) for e in obs.evidence] == [("def", 100), ("res", 70)]
    # The comparison stays prose on both rows (§V101): it says which damage type wins,
    # which neither scalar states on its own.
    assert all(e.note is not None and "physical" in e.note for e in obs.evidence)


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
#
# §T210 (a)/(b): the rule reads THREE typed fields in a fixed order of authority --
# damage_types gates it, a measured attack_range decides it, and upstream's own
# targeting token decides it when no radius was stored. The order is the fix: counted
# over the pinned EN snapshot, 77 of the 383 arts-capable enemies carry no radius and
# 55 of THOSE declare applyWay MELEE, so a rule that fell straight from "no radius" to
# the §V26 inference would publish 55 enemies as ranged threats against the source.


def test_ranged_arts_fires_on_arts_at_range() -> None:
    obs = _obs_by_tag(
        analyze_stage(ctx(occ("enemy_caster", damage_types=("MAGIC",), attack_range=2.5)))
    )["ranged_arts"]
    assert obs.rule_id == RANGED_ARTS_ID
    _assert_v6_fields(obs)
    assert obs.confidence >= 0.9
    assert obs.evidence[0].field == "attack_range"


def test_ranged_arts_reads_the_arts_half_of_a_dual_damage_enemy() -> None:
    # 42 real enemies deal PHYSIC *and* MAGIC, and PHYSIC is listed first -- a rule
    # reading damage_types[0] would skip every one of them.
    obs = _obs_by_tag(
        analyze_stage(ctx(occ("enemy_dual", damage_types=("PHYSIC", "MAGIC"), attack_range=4.5)))
    )["ranged_arts"]
    assert obs.evidence[0].field == "attack_range"


def test_ranged_arts_reads_targeting_before_inferring_from_absence() -> None:
    # No radius stored, but the source states the enemy strikes beyond melee: the
    # conclusion rests on that token, at a confidence between measured and inferred.
    obs = _obs_by_tag(
        analyze_stage(
            ctx(
                occ(
                    "enemy_arts",
                    damage_types=("MAGIC",),
                    attack_range=None,
                    targeting="RANGED",
                )
            )
        )
    )["ranged_arts"]
    assert 0.6 < obs.confidence < 0.9
    assert (obs.evidence[0].field, obs.evidence[0].value) == ("targeting", "RANGED")
    assert any("reach read from targeting" in lim for lim in obs.limitations)


def test_ranged_arts_melee_targeting_without_a_radius_does_not_fire() -> None:
    # THE 55-false-positive case (§T210 b): arts damage, no radius, and the source's own
    # token says melee. Inferring reach here would contradict the field that answered.
    result = analyze_stage(
        ctx(
            occ("enemy_melee_caster", damage_types=("MAGIC",), attack_range=None, targeting="MELEE")
        )
    )
    assert result.observations == ()


def test_ranged_arts_no_targeting_at_all_is_a_conflict_not_a_conclusion() -> None:
    # §V26 conflicting typed fields: it deals arts damage yet states no attack reach.
    result = analyze_stage(
        ctx(occ("enemy_inert", damage_types=("MAGIC",), attack_range=None, targeting="NONE"))
    )
    assert result.observations == ()
    assert any("no attack reach" in w for w in result.warnings)


# §T211/§V114 (B161): an absent radius is not always a missing field. Upstream's -1.0
# sentinel is an ANSWER ("no attack radius") that §V103 keeps out of the distance column,
# so the rule reads the flag BEFORE targeting -- otherwise it publishes a true conclusion
# under the false reason "attack_range missing" for the 13 en / 16 cn arts enemies that
# carry the sentinel AND declare RANGED|ALL.


def test_ranged_arts_denied_radius_conflicting_with_targeting_warns_and_omits() -> None:
    result = analyze_stage(
        ctx(
            occ(
                "enemy_1404_msnip",
                damage_types=("MAGIC",),
                attack_range=None,
                attack_range_declared_none=True,
                targeting="RANGED",
            )
        )
    )
    # §V26: two typed source fields disagree -> omit the conclusion, report the conflict.
    assert result.observations == ()
    warning = next(w for w in result.warnings if "enemy_1404_msnip" in w)
    assert "declares no attack radius" in warning
    assert "conflicting source fields" in warning
    # ...and the false reason is gone: nothing calls a cell the source FILLED "missing".
    assert "missing" not in warning


def test_ranged_arts_denied_radius_alone_neither_concludes_nor_warns() -> None:
    # The source answered "no attack radius" and said nothing else. That is an answer, not
    # a conflict and not an absence, so it earns neither an observation nor a warning --
    # and above all not the 0.6 inference arm the old code would have taken.
    result = analyze_stage(
        ctx(
            occ(
                "enemy_denied",
                damage_types=("MAGIC",),
                attack_range=None,
                attack_range_declared_none=True,
                targeting=None,
            )
        )
    )
    assert result.observations == ()
    assert result.warnings == ()


def test_ranged_arts_denied_radius_with_melee_targeting_is_silent() -> None:
    # Both typed fields agree that it has no reach -- nothing to conclude, nothing to warn.
    result = analyze_stage(
        ctx(
            occ(
                "enemy_denied_melee",
                damage_types=("MAGIC",),
                attack_range=None,
                attack_range_declared_none=True,
                targeting="MELEE",
            )
        )
    )
    assert result.observations == ()
    assert result.warnings == ()


def test_ranged_arts_denied_radius_with_no_targeting_keeps_the_no_reach_conflict() -> None:
    # The arts-damage-versus-no-reach conflict is the same finding whether the radius was
    # denied or never stated, so the denial must not silence it (§V37: one wording).
    result = analyze_stage(
        ctx(
            occ(
                "enemy_inert_denied",
                damage_types=("MAGIC",),
                attack_range=None,
                attack_range_declared_none=True,
                targeting="NONE",
            )
        )
    )
    assert result.observations == ()
    assert any("no attack reach" in w for w in result.warnings)


def test_ranged_arts_unstated_radius_still_reads_targeting() -> None:
    # The regression control for the arm above: when the source never stated a radius, the
    # 0.8 targeting conclusion stands and its "missing" limitation is now TRUE by
    # construction -- the denied class can no longer reach it.
    obs = _obs_by_tag(
        analyze_stage(
            ctx(
                occ(
                    "enemy_unstated",
                    damage_types=("MAGIC",),
                    attack_range=None,
                    attack_range_declared_none=False,
                    targeting="RANGED",
                )
            )
        )
    )["ranged_arts"]
    assert 0.6 < obs.confidence < 0.9
    assert any("attack_range missing" in lim for lim in obs.limitations)


def test_ranged_arts_missing_range_and_targeting_infers_at_reduced_confidence() -> None:
    obs = _obs_by_tag(
        analyze_stage(
            ctx(occ("enemy_arts", damage_types=("MAGIC",), attack_range=None, targeting=None))
        )
    )["ranged_arts"]
    assert obs.confidence < 0.8
    assert obs.evidence[0].field == "damage_types"
    assert any("attack_range and targeting both missing" in lim for lim in obs.limitations)


def test_ranged_arts_melee_arts_does_not_fire() -> None:
    # arts damage but no reach -> not a ranged-arts threat. A measured radius wins over
    # the targeting token, which is why this stays silent even declared RANGED.
    assert (
        analyze_stage(
            ctx(
                occ(
                    "enemy_bruiser",
                    damage_types=("MAGIC",),
                    attack_range=0.0,
                    targeting="RANGED",
                )
            )
        ).observations
        == ()
    )


def test_ranged_arts_physical_does_not_fire() -> None:
    assert (
        analyze_stage(
            ctx(occ("enemy_gun", damage_types=("PHYSIC",), attack_range=3.0))
        ).observations
        == ()
    )


def test_ranged_arts_absent_damage_types_does_not_fire() -> None:
    # §V26: the source carried no damage kind at all -> no conclusion, not a guess.
    assert (
        analyze_stage(
            ctx(occ("enemy_unknown", damage_types=None, attack_range=3.0, targeting="RANGED"))
        ).observations
        == ()
    )


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
    # §V101/B137: the note used to pack "8 spawns; computed window 8s is fragment-
    # relative" -- one number restating the typed value, one the client had to parse out.
    # The window's two operands are separately emitted fields, so each is its own row.
    assert [(e.field, e.value) for e in obs.evidence] == [
        ("total_count", 8),
        ("first_spawn_time", 2.0),
        ("last_spawn_time", 10.0),
    ]
    assert all(e.note is None for e in obs.evidence)


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
    # §V26/§V67: an absent window emits no spawn-bound row at all -- never a null or a
    # zero standing in for "unknown". The §V26 limitation is the sole absence signal.
    assert [(e.field, e.value) for e in obs.evidence] == [("total_count", 8)]


# --- lane/route ---------------------------------------------------------------


def test_lane_route_fires_on_multiple_routes() -> None:
    obs = _obs_by_tag(analyze_stage(ctx(occ("enemy_a"), route_count=3)))["lane_route"]
    assert obs.rule_id == LANE_ROUTE_ID
    _assert_v6_fields(obs)
    # §V68/B136: the stage-level row refs the unique game_id, never the shared stage_code.
    # §V101: the count is the typed value and nothing restates it in prose.
    assert obs.evidence[0].ref == "main_01-01"
    assert obs.evidence[0].field == "metrics.route_record_count"
    assert obs.evidence[0].value == 3
    assert obs.evidence[0].note is None


def test_lane_route_raw_count_is_not_labelled_lanes() -> None:
    # §V49/B43: a stage with 26 raw route records must NOT headline "26 lanes" -- the
    # raw route-record count overstates distinct lanes. It fires at reduced confidence
    # with a limitation that the raw count is not the lane count. §V101: "records, not
    # lanes" is carried by the field name + the limitation, never by a number in a note.
    obs = _obs_by_tag(analyze_stage(ctx(occ("enemy_a"), route_count=26)))["lane_route"]
    assert "26 lanes" not in obs.summary
    assert obs.evidence[0].field == "metrics.route_record_count"
    assert obs.confidence < 0.85  # raw count is not an authoritative lane measure
    assert any("distinct lanes" in lim for lim in obs.limitations)


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
    # §V68/B136: B136 named lane_route only; this rule had the same stage_code ref on
    # every row it has ever emitted. §V101: the grid total was prose ("of 24 tiles") and
    # is now its own typed row at its own emitted path.
    assert {e.ref for e in obs.evidence} == {"main_01-01"}
    assert [(e.field, e.value) for e in obs.evidence] == [
        ("metrics.buildable_ranged", 1),
        ("metrics.buildable_melee", 2),
        ("metrics.tile_total", 24),
    ]
    assert all(e.note is None for e in obs.evidence)


def test_tiles_deploy_ample_surface_does_not_fire() -> None:
    tiles = StageTiles(total=40, buildable_melee=12, buildable_ranged=10)
    assert analyze_stage(ctx(occ("enemy_a"), tiles=tiles)).observations == ()


def test_tiles_deploy_below_floor_is_skipped() -> None:
    # A stub grid (too few tiles) is not judged constrained.
    tiles = StageTiles(total=4, buildable_melee=0, buildable_ranged=0)
    assert analyze_stage(ctx(occ("enemy_a"), tiles=tiles)).observations == ()


def test_tiles_deploy_absent_tiles_does_not_fire() -> None:
    assert analyze_stage(ctx(occ("enemy_a"), tiles=None)).observations == ()


# --- cross-cutting guards -----------------------------------------------------

#: Prescriptive language §V7 forbids in an observation (it must state facts, not
#: prescribe a "mandatory"/"best"/"must-use" answer).
_FORBIDDEN = ("mandatory", "must use", "must bring", "best operator", "always bring", "recommended")


def _every_observation() -> list[Observation]:
    """Fire every rule once so the guards see all six observation shapes."""
    scenario = ctx(
        occ("enemy_drone", motion_type="FLY", total_count=2),
        occ("enemy_wall", defense=800, res=0),
        occ("enemy_caster", damage_types=("MAGIC",), attack_range=2.5),
        occ("enemy_swarm", total_count=8, first_spawn_time=2.0, last_spawn_time=10.0),
        route_count=3,
        tiles=StageTiles(total=24, buildable_melee=2, buildable_ranged=1),
    )
    return list(analyze_stage(scenario).observations)


def test_all_rules_can_fire_together() -> None:
    tags = {o.tag for o in _every_observation()}
    assert tags == {
        "aerial",
        "def_res_skew",
        "ranged_arts",
        "pressure_spike",
        "lane_route",
        "tiles_deploy",
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
    b = occ("enemy_caster", damage_types=("MAGIC",), attack_range=2.5)
    assert analyze_stage(ctx(a, b)) == analyze_stage(ctx(b, a))


def test_registry_covers_every_named_rule() -> None:
    # §T39 named nine rules; §T210 (c) retired three whose deciding fields no real
    # source fills, so the registry exposes exactly these six rule_ids.
    ids = {rule.rule_id for rule in THREAT_RULES}
    assert ids == {
        "threat.aerial",
        DEF_RES_SKEW_ID,
        RANGED_ARTS_ID,
        PRESSURE_SPIKE_ID,
        LANE_ROUTE_ID,
        TILES_DEPLOY_ID,
    }
    # A retired rule may not creep back in without its substrate (§V113 b).
    assert not (ids & RETIRED_RULES)
