"""Deterministic analyzer rules (typed-field only, no NL match; §V26).

Exposes the immutable :data:`THREAT_RULES` registry that
:func:`arknights_mcp.analyzers.stage.analyze_stage` runs in order. Each rule reads
typed fields only, stamps the five §V6 fields, and states capability/threat facts
without prescriptive "mandatory"/"best" language (§V7). Shared matching logic lives
in :mod:`arknights_mcp.analyzers.rules._common` (§V37 DRY).

M3 (§T39) registered nine rules; §T210 (c) retired three of them (see
:data:`RETIRED_RULES`). The count is now six and every one of them owns a deciding
field a real build populates, counted (§V113 (b)).
"""

from __future__ import annotations

from dataclasses import dataclass

from arknights_mcp.analyzers.base import RuleResult, StageThreatContext, ThreatRule
from arknights_mcp.analyzers.rules.aerial import AerialThreatRule
from arknights_mcp.analyzers.rules.def_res_skew import DefResSkewRule
from arknights_mcp.analyzers.rules.lane_route import LaneRouteRule
from arknights_mcp.analyzers.rules.pressure_spike import PressureSpikeRule
from arknights_mcp.analyzers.rules.ranged_arts import RangedArtsRule
from arknights_mcp.analyzers.rules.tiles_deploy import TilesDeployRule

#: Ordered, immutable set of threat rules run by the stage analyzer (six). Order is
#: stable so observations are emitted deterministically (§V26). Built through an
#: explicitly-typed list so each element is checked against the ``ThreatRule``
#: protocol individually.
_RULES: list[ThreatRule] = [
    AerialThreatRule(),
    DefResSkewRule(),
    RangedArtsRule(),
    PressureSpikeRule(),
    LaneRouteRule(),
    TilesDeployRule(),
]
THREAT_RULES: tuple[ThreatRule, ...] = tuple(_RULES)

#: §V113 (b): every registered rule -> the typed occurrence fields it can decide from.
#: A rule fires only when at least one of these carries real values on the build, so
#: this is the map a liveness guard joins against :data:`ENEMY_KEY_HOMES`.
RULE_DECIDING_FIELDS: dict[str, frozenset[str]] = {
    "threat.aerial": frozenset({"motion_type"}),
    "threat.def_res_skew": frozenset({"defense", "res"}),
    "threat.ranged_arts": frozenset(
        {"damage_types", "attack_range", "attack_range_declared_none", "targeting"}
    ),
    "threat.pressure_spike": frozenset({"total_count", "first_spawn_time", "last_spawn_time"}),
    "threat.lane_route": frozenset({"route_count"}),
    "threat.tiles_deploy": frozenset({"tiles"}),
}

#: §V117: the statuses a declared refusal arm may carry.
ARM_STATUSES = frozenset({"live", "dead_today"})


@dataclass(frozen=True, slots=True)
class LimitationArm:
    """One branch of a rule that REFUSES a judgement, with its counted status (§V117).

    §V113 (b) counts the RULE; this counts its BRANCHES. ``threat.aerial`` fired 1030
    times on the promoted build and emitted zero limitations: both of its §V26 refusal
    arms were dead, and the second was dead *because* of the first's own vocabulary (ten
    declared motion tokens over a real domain of two, so the guessed members answered
    the case the "unrecognized" arm existed to refuse). No rule-level count can see
    that, which is why the unit of accounting has to be the arm.

    ``marker`` is the substring that identifies the arm in the text it emits, and it is
    what ties this declaration to the code in both directions: the guard resolves every
    limitation-emitting site out of the rule module's own AST and requires a marker per
    site (an undeclared arm fails) and a site per marker (a stale declaration fails),
    then sweeps the promoted build and requires a ``live`` arm to fire and a
    ``dead_today`` arm not to.

    ``dead_today`` is not ``retired``. Each arm below is still REACHABLE by construction
    -- a nullable column really can arrive NULL, an unseen token really can appear -- so
    the honest move is to declare it and keep it, not to delete it and let the rule
    conclude by default (§T210 (c) retired arms no input could ever reach; not these).
    """

    rule_id: str
    name: str
    marker: str
    status: str
    counted: str


#: §V117/B164: every refusal arm of every registered rule, with the count over the
#: promoted build ``2026-07-30T010030Z-en-cn`` (6716 stages, 28302 stage-enemy
#: occurrences) that makes its status true. Pinned by
#: ``tests/contract/test_rule_arm_liveness.py``.
RULE_LIMITATION_ARMS: tuple[LimitationArm, ...] = (
    LimitationArm(
        rule_id="threat.aerial",
        name="motion_missing",
        marker="motion_type missing; not judged as aerial",
        status="dead_today",
        counted=(
            "0 fires / 1030 observations. enemies.motion_type is NULL on 0/3879 rows and the "
            "occurrence reads COALESCE(variant, base) over that base, so 0/28302 occurrences "
            "arrive without a motion. Reachable: the column is nullable and the variant's own "
            "motion_type IS NULL on 11 rows, so only the COALESCE keeps this arm quiet. Narrower "
            "than it reads: the limitation is dropped with the observation unless the same stage "
            "also fields a confirmed flyer."
        ),
    ),
    LimitationArm(
        rule_id="threat.aerial",
        name="motion_unrecognized",
        marker="unrecognized motion_type=",
        status="dead_today",
        counted=(
            "0 fires / 1030 observations. The stored domain is exactly WALK 26526 | FLY 1776, and "
            "both are classified, so nothing is left over. Before §T213 this arm was unfirable "
            "rather than merely quiet: the vocabulary declared eight more tokens than the corpus "
            "sends, and each one answered a case this arm exists to refuse."
        ),
    ),
    LimitationArm(
        rule_id="threat.def_res_skew",
        name="def_missing",
        marker="def missing; damage-type skew not assessed",
        status="dead_today",
        counted=(
            "0 fires. def is NULL while res is present on 0/28302 occurrences (the mirror case is "
            "136). Reachable: both columns are nullable and independently populated. Narrower "
            "than it reads, like the aerial arms: the refusal is dropped with the observation "
            "unless another enemy in the same stage produced a skew conclusion to carry it."
        ),
    ),
    LimitationArm(
        rule_id="threat.def_res_skew",
        name="res_missing",
        marker="res missing; damage-type skew not assessed",
        status="live",
        counted="56 fires / 3184 observations (res-only-NULL on 136 occurrences).",
    ),
    LimitationArm(
        rule_id="threat.ranged_arts",
        name="range_missing_reach_from_targeting",
        marker="attack_range missing; reach read from targeting",
        status="live",
        counted="162 fires / 2368 observations.",
    ),
    LimitationArm(
        rule_id="threat.ranged_arts",
        name="range_and_targeting_missing",
        marker="attack_range and targeting both missing",
        status="live",
        counted="52 fires / 2368 observations.",
    ),
    LimitationArm(
        rule_id="threat.pressure_spike",
        name="fragment_window",
        marker="may overstate burst",
        status="live",
        counted="4128 fires / 2273 observations (per enemy).",
    ),
    LimitationArm(
        rule_id="threat.pressure_spike",
        name="spawn_timing_missing",
        marker="spawn timing missing; burst window unconfirmed",
        status="dead_today",
        counted=(
            "0 fires / 6401 limitations. stage_enemies.first_spawn_time and last_spawn_time are "
            "NULL on 0/28302 rows. The stamped confidence cannot witness this either -- the "
            "windowed and window-missing constants are both 0.5 -- so only the limitation text "
            "can. Reachable: both columns are nullable."
        ),
    ),
    LimitationArm(
        rule_id="threat.pressure_spike",
        name="spawn_timeline_route",
        marker="not to elapsed stage time",
        status="live",
        counted="2273 fires (once per observation, §V66).",
    ),
    LimitationArm(
        rule_id="threat.lane_route",
        name="raw_route_records",
        marker="not the number of distinct lanes",
        status="live",
        counted="4937 fires / 4937 observations (unconditional, §V108).",
    ),
    LimitationArm(
        rule_id="threat.tiles_deploy",
        name="tile_counts_not_positions",
        marker="tile counts, not positions",
        status="live",
        counted="488 fires / 488 observations (unconditional, §V108).",
    ),
)

#: §V113 (b)/§T210 (c): rules REMOVED because every field they could decide from is
#: dead by DATA, not by defect (B160 (c); ADR 0016). Recorded rather than deleted
#: silently, because "nine rules" was counted as coverage (§V6) for four milestones
#: while these three emitted zero observations over 3264 en + 1200 cn stages:
#:
#: * ``threat.block_bypass`` -- decided from ``block_behavior``/``abilities``.
#:   Upstream's ``attributes.blockCnt`` is ``m_defined:false`` on 2031/2036 entries,
#:   and the only unblockable statement in the corpus is the PROSE
#:   ``abilityList[].text`` ("Cannot be blocked." x37), which §V26 forbids reading.
#: * ``threat.crowd_control`` and ``threat.support_aura`` -- decided from
#:   ``abilities``. No typed ability vocabulary exists upstream at all; the nearest
#:   thing is the free-form ``skills[].prefabKey`` (626 distinct values over 1378
#:   rows), whose tokens carry no verified meaning, so classifying on them would
#:   invent facts (§V29/§V96) instead of reading them.
#:
#: Re-adding any of these requires a deciding field that is COUNTED on a real build
#: first -- ``tests/contract/test_column_liveness.py`` pins that both ways.
RETIRED_RULES: frozenset[str] = frozenset(
    {"threat.block_bypass", "threat.crowd_control", "threat.support_aura"}
)

__all__ = [
    "ARM_STATUSES",
    "THREAT_RULES",
    "RULE_DECIDING_FIELDS",
    "RULE_LIMITATION_ARMS",
    "RETIRED_RULES",
    "LimitationArm",
    "AerialThreatRule",
    "DefResSkewRule",
    "LaneRouteRule",
    "PressureSpikeRule",
    "RangedArtsRule",
    "TilesDeployRule",
    "RuleResult",
    "StageThreatContext",
    "ThreatRule",
]
