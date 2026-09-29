"""Deterministic analyzer rules (typed-field only, no NL match).

Exposes the immutable :data:`THREAT_RULES` registry that
:func:`arknights_mcp.analyzers.stage.analyze_stage` runs in order. Each rule reads
typed fields only, stamps the five identity fields, and states capability/threat facts
without prescriptive "mandatory"/"best" language. Shared matching logic lives
in :mod:`arknights_mcp.analyzers.rules._common`.

M3 registered nine rules; three of them were later retired (see
:data:`RETIRED_RULES`). The count is now six and every one of them owns a deciding
field a real build populates, counted.
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
#: stable so observations are emitted deterministically. Built through an
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

#: Every registered rule -> the typed occurrence fields it can decide from.
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

#: Every registered rule -> the typed occurrence fields whose ABSENCE forces
#: it to refuse. This is NOT :data:`RULE_DECIDING_FIELDS` narrowed: that map asks whether
#: a rule has anything at all to decide from on a build and is satisfied by any one
#: member, so ``threat.ranged_arts`` passes it on ``targeting`` alone while an occurrence
#: with no ``damage_types`` is one it cannot start judging. ANY member absent is enough:
#: a rule may not skip such an occurrence in silence -- absence is not a negative.
#:
#: An empty set means the rule decides from stage-level inputs only; the stage-level
#: substrate disclosure covers those, so they own no per-occurrence gate.
RULE_GATE_FIELDS: dict[str, frozenset[str]] = {
    "threat.aerial": frozenset({"motion_type"}),
    "threat.def_res_skew": frozenset({"defense", "res"}),
    "threat.ranged_arts": frozenset({"damage_types"}),
    "threat.pressure_spike": frozenset({"total_count"}),
    "threat.lane_route": frozenset(),
    "threat.tiles_deploy": frozenset(),
}

#: The statuses a declared refusal arm may carry.
ARM_STATUSES = frozenset({"live", "dead_today"})


@dataclass(frozen=True, slots=True)
class LimitationArm:
    """One branch of a rule that REFUSES a judgement, with its counted status.

    This counts a rule's BRANCHES, not the rule. ``threat.aerial`` fired 1030
    times on the promoted build and emitted zero limitations: both of its refusal
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
    conclude by default (retired arms no input could ever reach; not these).
    """

    rule_id: str
    name: str
    marker: str
    status: str
    counted: str


#: Every refusal arm of every registered rule, with the count over the
#: promoted build ``2026-07-30T092427Z-en-cn`` (6716 stages, 28302 stage-enemy
#: occurrences) that makes its status true. Pinned by
#: ``tests/contract/test_rule_arm_liveness.py``.
#:
#: Three arms were added that did not exist because the branch emitted nothing
#: at all -- a silent ``continue`` has no site for the AST guard to demand a
#: declaration for, which is why that guard read complete at eleven arms while three
#: refusals were missing entirely. It also removed the "dropped with the observation"
#: precondition several of these carried: a rule that concludes nothing now sends its
#: refusals out as warnings (:func:`._common.declined`).
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
            "motion_type IS NULL on 11 rows, so only the COALESCE keeps this arm quiet. The "
            "flyer-in-the-same-stage precondition it used to carry is gone."
        ),
    ),
    LimitationArm(
        rule_id="threat.aerial",
        name="motion_unrecognized",
        marker="unrecognized motion_type=",
        status="dead_today",
        counted=(
            "0 fires / 1030 observations. The stored domain is exactly WALK 26526 | FLY 1776, and "
            "both are classified, so nothing is left over. Before, this arm was unfirable "
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
            "136). Reachable: both columns are nullable and independently populated. No longer "
            "narrowed by the rule's own gate -- a refusal routes to warnings when the rule "
            "concludes nothing, so this arm no longer waits on another enemy skewing."
        ),
    ),
    LimitationArm(
        rule_id="threat.def_res_skew",
        name="res_missing",
        marker="res missing; damage-type skew not assessed",
        status="live",
        counted="136 fires (res-only-NULL on 136 occurrences over 128 stages).",
    ),
    LimitationArm(
        rule_id="threat.def_res_skew",
        name="def_and_res_missing",
        marker="def and res both missing; damage-type skew not assessed",
        status="live",
        counted=(
            "64 fires (33 en / 31 cn) over 56 stages and 4 enemies per server, one of them the "
            "boss enemy_1544_cledub. This branch was silent until refusal routing -- the "
            "enemy the rule knew least about was the one it stayed quiet on, while the one-stat "
            "arm above refused out loud."
        ),
    ),
    LimitationArm(
        rule_id="threat.ranged_arts",
        name="damage_types_missing",
        marker="damage_types missing; not judged as ranged arts",
        status="live",
        counted=(
            "227 fires (107 en / 120 cn) over 181 stages and 19 enemies per server. These "
            "used to fall into the same silent continue as an enemy whose damage kinds were "
            "PRESENT and simply held no arts token; every one of them is an enemy the handbook "
            "never described (display_name is NULL too), so the rule was quietest about the "
            "enemies it knew nothing about."
        ),
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
        rule_id="threat.ranged_arts",
        name="conflict_declared_none_vs_targeting",
        marker="conflicting source fields, omitted from ranged-arts conclusion",
        status="live",
        counted=(
            "98 fires / 2368 observations. The other half -- two typed fields disagreeing, so "
            "the conclusion is omitted and the disagreement stated. It rides "
            "warnings rather than limitations, and the guard only read the limitation "
            "channel, so this arm and the next were refusals nothing was counting."
        ),
    ),
    LimitationArm(
        rule_id="threat.ranged_arts",
        name="conflict_no_attack_reach",
        marker="states no attack reach; omitted from ranged-arts conclusion",
        status="live",
        counted=(
            "60 fires / 2368 observations. One shared wording reached from two arms (radius never "
            "stated / radius DENIED by the sentinel), counted together because the finding "
            "is the same: the enemy deals arts damage and targeting says it reaches nothing."
        ),
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
        name="total_count_missing",
        marker="total_count missing; spawn pressure not assessed",
        status="dead_today",
        counted=(
            "0 fires. stage_enemies.total_count is NULL on 0/28302 rows. Reachable: the column is "
            "nullable. Absence used to be folded in with a genuinely low count (`count is "
            "None or count < _SPIKE_MIN_COUNT`), so an unknown arrival read as a small one."
        ),
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
        counted="2273 fires (once per observation).",
    ),
    LimitationArm(
        rule_id="threat.lane_route",
        name="raw_route_records",
        marker="not the number of distinct lanes",
        status="live",
        counted="4937 fires / 4937 observations (unconditional).",
    ),
    LimitationArm(
        rule_id="threat.tiles_deploy",
        name="tile_counts_not_positions",
        marker="tile counts, not positions",
        status="live",
        counted="488 fires / 488 observations (unconditional).",
    ),
)

#: Rules REMOVED because every field they could decide from is
#: dead by DATA, not by defect (ADR 0016). Recorded rather than deleted
#: silently, because "nine rules" was counted as coverage for four milestones
#: while these three emitted zero observations over 3264 en + 1200 cn stages:
#:
#: * ``threat.block_bypass`` -- decided from ``block_behavior``/``abilities``.
#:   Upstream's ``attributes.blockCnt`` is ``m_defined:false`` on 2031/2036 entries,
#:   and the only unblockable statement in the corpus is the PROSE
#:   ``abilityList[].text`` ("Cannot be blocked." x37), which typed-field-only
#:   matching forbids reading.
#: * ``threat.crowd_control`` and ``threat.support_aura`` -- decided from
#:   ``abilities``. No typed ability vocabulary exists upstream at all; the nearest
#:   thing is the free-form ``skills[].prefabKey`` (626 distinct values over 1378
#:   rows), whose tokens carry no verified meaning, so classifying on them would
#:   invent facts instead of reading them.
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
    "RULE_GATE_FIELDS",
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
