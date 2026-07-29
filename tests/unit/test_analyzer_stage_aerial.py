"""T16: the M0 deterministic aerial threat rule + stage analyzer (§V6, §V26).

Verifies every observation carries the §V6 fields, that the rule decides from
typed fields only (never prose, §V26), and that an absent / unrecognized
``motion_type`` yields no conclusion plus a §V26 limitation.

§T210 (c)/B160: the rule's second arm -- infer flight from an ``aerial`` ability
token, and warn when a ground motion contradicted one -- is gone with the
``abilities`` field. No source has ever carried a typed ability vocabulary, so that
arm decided 0 of the 507 EN observations this rule really makes while its tests
passed on synthetic tokens: a guard that cannot fail on real data is not a guard.
"""

from __future__ import annotations

from arknights_mcp.analyzers import (
    ANALYZER_VERSION,
    EnemyOccurrence,
    StageThreatContext,
    ThreatRule,
    analyze_stage,
)
from arknights_mcp.analyzers.rules import THREAT_RULES
from arknights_mcp.analyzers.rules.aerial import RULE_ID, AerialThreatRule


def _ctx(
    *occ: EnemyOccurrence, stage_game_id: str = "main_04-04", stage_code: str = "4-4"
) -> StageThreatContext:
    # §V68/B136: game_id and stage_code differ on purpose -- "4-4" is shared by
    # main_04-04 and main_04-04#f#, which is exactly why a ref may not be the code.
    return StageThreatContext(
        server="en",
        stage_game_id=stage_game_id,
        stage_code=stage_code,
        occurrences=tuple(occ),
    )


# Both carry the source's OWN damage token (the real handbook sends ``damageType``
# ["MAGIC"] for this drone and ["PHYSIC"] for this slug); the invented "physical"
# string these fixtures used to pass appears on 0 of 1585 real entries (B160 (b)).
DRONE = EnemyOccurrence(
    game_id="enemy_1105_drone",
    display_name="Recon Drone",
    motion_type="FLY",
    damage_types=("MAGIC",),
    attack_range=2.5,
    targeting="RANGED",
    total_count=2,
)
SLUG = EnemyOccurrence(
    game_id="enemy_1007_slime",
    display_name="Originium Slug",
    motion_type="WALK",
    damage_types=("PHYSIC",),
    total_count=3,
)


def test_registry_rules_match_protocol_with_unique_ids() -> None:
    # M3 (§T39) grew the engine from one rule to nine; §T210 (c) retired the three
    # that no real column could feed. Each survivor still satisfies the ThreatRule
    # protocol (rule_id + evaluate) and every rule_id is unique.
    assert len(THREAT_RULES) == 6
    for rule in THREAT_RULES:
        assert isinstance(rule, ThreatRule)  # structural: rule_id + evaluate()
    assert any(isinstance(rule, AerialThreatRule) for rule in THREAT_RULES)
    ids = [rule.rule_id for rule in THREAT_RULES]
    assert len(set(ids)) == len(ids)


def test_aerial_fires_on_flying_enemy_with_v6_fields() -> None:
    result = analyze_stage(_ctx(DRONE, SLUG))
    assert result.analyzer_version == ANALYZER_VERSION
    # The drone is an arts flyer, so the ranged-arts rule fires on it too (that is the
    # §T210 revival working); this suite is about the aerial one.
    obs = next(o for o in result.observations if o.rule_id == RULE_ID)
    # §V6: every mandated field present + well-formed.
    assert obs.rule_id == RULE_ID
    assert obs.analyzer_version == ANALYZER_VERSION
    assert 0.0 <= obs.confidence <= 1.0
    assert obs.confidence >= 0.9  # authoritative motion_type=FLY
    assert obs.evidence  # non-empty evidence
    assert isinstance(obs.limitations, tuple)
    # Only the flyer contributes evidence, not the ground slug.
    assert {e.ref for e in obs.evidence} == {"enemy_1105_drone"}
    ev = obs.evidence[0]
    assert ev.field == "motion_type"
    assert ev.value == "FLY"
    # §V101/B137: how many drones the stage fields used to ride the deciding row as a
    # "total_count=2" note -- a second number buried in a string the client had to parse.
    # It is a fact with its own emitted field path, so it is its own row with no prose.
    assert [(e.field, e.value) for e in obs.evidence] == [
        ("motion_type", "FLY"),
        ("total_count", 2),
    ]
    assert all(e.note is None for e in obs.evidence)


def test_no_observation_when_only_ground_enemies() -> None:
    result = analyze_stage(_ctx(SLUG))
    assert result.observations == ()
    assert result.warnings == ()


def test_typed_field_only_no_nl_keyword_match() -> None:
    # Name screams "aerial/flying" but typed fields say ground with no aerial
    # ability -> §V26 forbids matching on the natural-language name.
    trap = EnemyOccurrence(
        game_id="enemy_nl_trap",
        display_name="Aerial Flying Skyborne Terror",
        motion_type="WALK",
        damage_types=("PHYSIC",),
        total_count=1,
    )
    result = analyze_stage(_ctx(trap))
    assert result.observations == ()


def test_missing_motion_type_yields_no_conclusion_and_a_limitation() -> None:
    # §V26: the one field that decides is absent -> the enemy is not judged, and the
    # response says which enemy went unjudged rather than defaulting it to ground.
    unknown = EnemyOccurrence(
        game_id="enemy_infer",
        display_name=None,
        motion_type=None,  # field absent
        damage_types=None,
        total_count=1,
    )
    result = analyze_stage(_ctx(DRONE, unknown))
    obs = next(o for o in result.observations if o.rule_id == RULE_ID)
    assert {e.ref for e in obs.evidence} == {"enemy_1105_drone"}
    assert any("enemy_infer" in lim and "motion_type missing" in lim for lim in obs.limitations)


def test_unrecognized_motion_type_yields_no_conclusion_and_a_limitation() -> None:
    # §V96: a token outside the known vocabulary is not silently bucketed as ground.
    odd = EnemyOccurrence(
        game_id="enemy_odd",
        display_name=None,
        motion_type="HOVER_UNKNOWN",
        damage_types=None,
        total_count=1,
    )
    result = analyze_stage(_ctx(DRONE, odd))
    obs = next(o for o in result.observations if o.rule_id == RULE_ID)
    assert {e.ref for e in obs.evidence} == {"enemy_1105_drone"}
    assert any("unrecognized motion_type" in lim for lim in obs.limitations)


def test_same_flyer_at_two_variants_counts_as_one_type() -> None:
    # M3/§V6: one enemy appearing at two level variants yields two evidence items
    # with the same ref; the headline must count distinct enemies, not evidence.
    drone_v1 = EnemyOccurrence(
        game_id="enemy_1105_drone",
        display_name="Recon Drone",
        motion_type="FLY",
        damage_types=("MAGIC",),
        attack_range=2.5,
        targeting="RANGED",
        total_count=1,
    )
    result = analyze_stage(_ctx(DRONE, drone_v1))
    obs = next(o for o in result.observations if o.rule_id == RULE_ID)
    assert {e.ref for e in obs.evidence} == {"enemy_1105_drone"}
    assert "1 aerial enemy type" in obs.summary  # not "2 ... types"


def test_output_is_deterministic_regardless_of_input_order() -> None:
    a = analyze_stage(_ctx(DRONE, SLUG))
    b = analyze_stage(_ctx(SLUG, DRONE))
    assert a == b  # frozen dataclasses compare by value; evidence order is stable
