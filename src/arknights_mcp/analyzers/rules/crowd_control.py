"""Crowd-control threat rule (§V6, §V26): flags enemies that disable *your*
operators -- stun, silence, freeze, sleep, root, bind, levitate -- through a typed
ability token.

A configured instance of the shared :class:`~arknights_mcp.analyzers.rules._common.AbilityTokenRule`
-- typed tokens only, never prose (§V26); one enemy at several level variants is
counted once (§V35). The summary states a capability fact, not a recommendation
(§V7).
"""

from __future__ import annotations

from arknights_mcp.analyzers.rules._common import AbilityTokenRule

RULE_ID = "threat.crowd_control"

#: Typed ability tokens for crowd-control effects applied to deployed operators.
_CC_ABILITIES: frozenset[str] = frozenset(
    {
        "stun",
        "silence",
        "freeze",
        "frozen",
        "sleep",
        "root",
        "bind",
        "levitate",
        "cold",
        "sluggish",
    }
)

CrowdControlRule = AbilityTokenRule(
    rule_id=RULE_ID,
    category="threat",
    tag="crowd_control",
    title="Crowd-control enemies present",
    tokens=_CC_ABILITIES,
    confidence=0.7,  # inferred from a typed ability token, not an authoritative flag
    summary_template=(
        "Stage fields {n} enemy type(s) that apply crowd control "
        "(stun / silence / freeze / bind); deployed operators can be disabled mid-fight."
    ),
)
