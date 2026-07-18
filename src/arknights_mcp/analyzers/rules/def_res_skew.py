"""Defence/resistance-skew threat rule (§V6, §V26): flags enemies whose armor and
arts resistance are strongly asymmetric, so one damage type is far more effective
against them than the other.

Reads the typed ``defense`` + ``res`` stats only (§V26) -- never a name or
description. Both stats must be present to judge a skew; a partially-typed enemy
(one stat missing) is not concluded from, it is recorded as a limitation (§V26).
The summary states which damage type the enemy resists (a fact), never prescribes
an operator (§V7). One enemy across several level variants counts once (§V35).
"""

from __future__ import annotations

from arknights_mcp.analyzers.base import (
    EvidenceItem,
    Observation,
    RuleResult,
    StageThreatContext,
)
from arknights_mcp.analyzers.rules._common import by_game_id, distinct_refs

RULE_ID = "threat.def_res_skew"

# Thresholds reflect typical Arknights values: early ground enemies sit ~100-300
# def / 0 res, a genuine physical wall is 500+ def, an arts-resistant enemy is 50+
# res. A skew = high in one axis AND low in the other (high in *both* is just tanky,
# not skewed, so it is not flagged).
_HIGH_DEF = 500
_LOW_RES = 20
_HIGH_RES = 50
_LOW_DEF = 200

_CONFIDENCE = 0.8  # both stats are authoritative typed fields


class DefResSkewRule:
    """Flags enemies whose def/res asymmetry favors one damage type (§V6, §V26)."""

    rule_id = RULE_ID

    def evaluate(self, ctx: StageThreatContext) -> RuleResult:
        evidence: list[EvidenceItem] = []
        limitations: list[str] = []
        confidence = 0.0

        for occ in by_game_id(ctx.occurrences):
            d, r = occ.defense, occ.res
            if d is None or r is None:
                if d is None and r is None:
                    continue  # nothing typed to assess for this enemy
                missing = "def" if d is None else "res"
                limitations.append(
                    f"{occ.game_id}: {missing} missing; damage-type skew not assessed"
                )
                continue

            if d >= _HIGH_DEF and r <= _LOW_RES:
                direction = "high armor, low resistance (arts damage far more effective)"
            elif r >= _HIGH_RES and d <= _LOW_DEF:
                direction = "high resistance, low armor (physical damage far more effective)"
            else:
                continue

            evidence.append(
                EvidenceItem(
                    ref=occ.game_id, field="def/res", value=f"def={d},res={r}", note=direction
                )
            )
            confidence = max(confidence, _CONFIDENCE)

        if not evidence:
            return RuleResult()

        count = distinct_refs(evidence)
        types_word = "type" if count == 1 else "types"
        return RuleResult(
            observation=Observation(
                rule_id=RULE_ID,
                category="threat",
                tag="def_res_skew",
                title="Damage-type-skewed enemies present",
                summary=(
                    f"Stage fields {count} enemy {types_word} whose armor and resistance are "
                    "strongly asymmetric; one damage type is far more effective than the other "
                    "against them."
                ),
                confidence=confidence,
                evidence=tuple(evidence),
                limitations=tuple(limitations),
            )
        )
