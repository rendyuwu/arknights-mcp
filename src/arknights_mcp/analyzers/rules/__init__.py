"""Deterministic analyzer rules (typed-field only, no NL match; §V26).

Exposes the immutable :data:`THREAT_RULES` registry that
:func:`arknights_mcp.analyzers.stage.analyze_stage` runs in order. M3 (§T39) grows
the M0 single-rule engine to nine deterministic, evidence-backed rules; each reads
typed fields only, stamps the five §V6 fields, and states capability/threat facts
without prescriptive "mandatory"/"best" language (§V7). Shared matching logic lives
in :mod:`arknights_mcp.analyzers.rules._common` (§V37 DRY).
"""

from __future__ import annotations

from arknights_mcp.analyzers.base import RuleResult, StageThreatContext, ThreatRule
from arknights_mcp.analyzers.rules.aerial import AerialThreatRule
from arknights_mcp.analyzers.rules.block_bypass import BlockBypassRule
from arknights_mcp.analyzers.rules.crowd_control import CrowdControlRule
from arknights_mcp.analyzers.rules.def_res_skew import DefResSkewRule
from arknights_mcp.analyzers.rules.lane_route import LaneRouteRule
from arknights_mcp.analyzers.rules.pressure_spike import PressureSpikeRule
from arknights_mcp.analyzers.rules.ranged_arts import RangedArtsRule
from arknights_mcp.analyzers.rules.support_aura import SupportAuraRule
from arknights_mcp.analyzers.rules.tiles_deploy import TilesDeployRule

#: Ordered, immutable set of threat rules run by the stage analyzer (M3: nine).
#: The aura + crowd-control rules are pre-built ``AbilityTokenRule`` instances; the
#: rest are instantiated rule classes. Order is stable so observations are emitted
#: deterministically (§V26). Built through an explicitly-typed list so each element
#: is checked against the ``ThreatRule`` protocol individually.
_RULES: list[ThreatRule] = [
    AerialThreatRule(),
    BlockBypassRule(),
    DefResSkewRule(),
    RangedArtsRule(),
    SupportAuraRule,
    PressureSpikeRule(),
    LaneRouteRule(),
    TilesDeployRule(),
    CrowdControlRule,
]
THREAT_RULES: tuple[ThreatRule, ...] = tuple(_RULES)

#: §V113 (b): every registered rule -> the typed occurrence fields it can decide from.
#: A rule fires only when at least one of these carries real values on the build, so
#: this is the map a liveness guard joins against :data:`ENEMY_KEY_HOMES`.
RULE_DECIDING_FIELDS: dict[str, frozenset[str]] = {
    "threat.aerial": frozenset({"motion_type", "abilities"}),
    "threat.block_bypass": frozenset({"block_behavior", "abilities"}),
    "threat.def_res_skew": frozenset({"defense", "res"}),
    "threat.ranged_arts": frozenset({"attack_type", "attack_range"}),
    "threat.support_aura": frozenset({"abilities"}),
    "threat.pressure_spike": frozenset({"total_count", "first_spawn_time", "last_spawn_time"}),
    "threat.lane_route": frozenset({"route_count"}),
    "threat.tiles_deploy": frozenset({"tiles"}),
    "threat.crowd_control": frozenset({"abilities"}),
}

#: §V113 (b): rules that CANNOT fire on the real build because every deciding field
#: above is dead-by-data, and rules with a dead ARM that the live arm masks. A
#: registered rule is not coverage (§V6) -- listing it here is the declaration §V113
#: demands in place of a silent no-op. Retiring or re-grounding them is T210 (c);
#: until then this set is what keeps the "nine rules" count honest.
#:
#: ``threat.ranged_arts`` is deliberately ABSENT: both of its deciding fields are
#: dead today, but T210 (a)+(b) revive them from a counted upstream home, so it is a
#: bridge gap, not a dead rule. The three below have no upstream home at all (B160).
DEAD_BY_DATA_RULES: frozenset[str] = frozenset(
    {"threat.block_bypass", "threat.crowd_control", "threat.support_aura"}
)

#: §V113 (b): a rule whose only live arm is one of several must DECLARE the dead
#: ones -- ``threat.aerial`` reports 507 en stages purely from ``motion_type``; its
#: ``abilities`` arm ("aerial" token) has been unreachable since the first build.
DEAD_RULE_ARMS: dict[str, frozenset[str]] = {"threat.aerial": frozenset({"abilities"})}

__all__ = [
    "THREAT_RULES",
    "RULE_DECIDING_FIELDS",
    "DEAD_BY_DATA_RULES",
    "DEAD_RULE_ARMS",
    "AerialThreatRule",
    "BlockBypassRule",
    "CrowdControlRule",
    "DefResSkewRule",
    "LaneRouteRule",
    "PressureSpikeRule",
    "RangedArtsRule",
    "SupportAuraRule",
    "TilesDeployRule",
    "RuleResult",
    "StageThreatContext",
    "ThreatRule",
]
