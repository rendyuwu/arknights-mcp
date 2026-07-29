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
    "THREAT_RULES",
    "RULE_DECIDING_FIELDS",
    "RETIRED_RULES",
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
