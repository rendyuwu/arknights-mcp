"""Deterministic analyzer rules (typed-field only, no NL match; §V26).

Exposes the immutable :data:`THREAT_RULES` registry that
:func:`arknights_mcp.analyzers.stage.analyze_stage` runs in order.
"""

from __future__ import annotations

from arknights_mcp.analyzers.base import RuleResult, StageThreatContext, ThreatRule
from arknights_mcp.analyzers.rules.aerial import AerialThreatRule

#: Ordered, immutable set of threat rules run by the stage analyzer (M0: one).
THREAT_RULES: tuple[ThreatRule, ...] = (AerialThreatRule(),)

__all__ = [
    "THREAT_RULES",
    "AerialThreatRule",
    "RuleResult",
    "StageThreatContext",
    "ThreatRule",
]
