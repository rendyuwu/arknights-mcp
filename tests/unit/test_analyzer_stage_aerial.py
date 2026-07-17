"""T16: the M0 deterministic aerial threat rule + stage analyzer (§V6, §V26).

Verifies every observation carries the §V6 fields, that the rule decides from
typed fields only (never prose, §V26), and that missing / conflicting fields are
handled per §V26 (reduced confidence + limitation; omit conclusion + warn).
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


def _ctx(*occ: EnemyOccurrence, stage_code: str = "4-4") -> StageThreatContext:
    return StageThreatContext(server="en", stage_code=stage_code, occurrences=tuple(occ))


DRONE = EnemyOccurrence(
    game_id="enemy_1105_drone",
    display_name="Recon Drone",
    motion_type="FLY",
    attack_type="physical",
    abilities=("aerial",),
    total_count=2,
)
SLUG = EnemyOccurrence(
    game_id="enemy_1007_slime",
    display_name="Originium Slug",
    motion_type="WALK",
    attack_type="physical",
    abilities=(),
    total_count=3,
)


def test_registry_has_single_rule_matching_protocol() -> None:
    assert len(THREAT_RULES) == 1
    rule = THREAT_RULES[0]
    assert isinstance(rule, ThreatRule)  # structural: rule_id + evaluate()
    assert isinstance(rule, AerialThreatRule)


def test_aerial_fires_on_flying_enemy_with_v6_fields() -> None:
    result = analyze_stage(_ctx(DRONE, SLUG))
    assert result.analyzer_version == ANALYZER_VERSION
    assert len(result.observations) == 1
    obs = result.observations[0]
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
        attack_type="physical",
        abilities=(),
        total_count=1,
    )
    result = analyze_stage(_ctx(trap))
    assert result.observations == ()


def test_missing_motion_type_reduces_confidence_and_records_limitation() -> None:
    inferred = EnemyOccurrence(
        game_id="enemy_infer",
        display_name=None,
        motion_type=None,  # field absent
        attack_type=None,
        abilities=("aerial",),
        total_count=1,
    )
    result = analyze_stage(_ctx(inferred))
    assert len(result.observations) == 1
    obs = result.observations[0]
    assert obs.confidence < 0.9  # §V26: missing field reduces confidence
    assert any("motion_type missing" in lim for lim in obs.limitations)
    assert obs.evidence[0].field == "abilities"


def test_conflicting_fields_omit_conclusion_and_warn() -> None:
    conflict = EnemyOccurrence(
        game_id="enemy_conflict",
        display_name=None,
        motion_type="WALK",  # ground motion ...
        attack_type=None,
        abilities=("aerial",),  # ... but ability claims aerial
        total_count=1,
    )
    result = analyze_stage(_ctx(conflict))
    assert result.observations == ()  # §V26: omit conclusion
    assert any("conflict" in w for w in result.warnings)  # §V26: warn


def test_output_is_deterministic_regardless_of_input_order() -> None:
    a = analyze_stage(_ctx(DRONE, SLUG))
    b = analyze_stage(_ctx(SLUG, DRONE))
    assert a == b  # frozen dataclasses compare by value; evidence order is stable
