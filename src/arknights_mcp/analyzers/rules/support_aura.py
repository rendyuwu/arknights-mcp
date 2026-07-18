"""Support-aura threat rule (§V6, §V26): flags enemies that heal, shield, or buff
*other* enemies through a typed ability token, raising the effective durability of
the enemies around them.

A configured instance of the shared :class:`~arknights_mcp.analyzers.rules._common.AbilityTokenRule`
-- typed tokens only, never prose (§V26); one enemy at several level variants is
counted once (§V35). The summary states a capability fact, not a recommendation
(§V7): it never prescribes a counter or calls anything "mandatory"/"best".
"""

from __future__ import annotations

from arknights_mcp.analyzers.rules._common import AbilityTokenRule

RULE_ID = "threat.support_aura"

#: Typed ability tokens that mean the enemy supports other enemies (not itself).
_AURA_ABILITIES: frozenset[str] = frozenset(
    {
        "heal",
        "healer",
        "heal_allies",
        "healing_aura",
        "buff",
        "buff_allies",
        "aura",
        "shield",
        "shield_allies",
        "support",
        "damage_aura",
        "enrage_aura",
    }
)

SupportAuraRule = AbilityTokenRule(
    rule_id=RULE_ID,
    category="threat",
    tag="support_aura",
    title="Enemy support auras present",
    tokens=_AURA_ABILITIES,
    confidence=0.7,  # inferred from a typed ability token, not an authoritative flag
    summary_template=(
        "Stage fields {n} enemy type(s) that heal, shield, or buff other enemies; "
        "their aura raises the effective durability of nearby enemies."
    ),
)
