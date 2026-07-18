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

__all__ = [
    "THREAT_RULES",
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
