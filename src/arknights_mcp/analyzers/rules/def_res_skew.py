"""Defence/resistance-skew threat rule: flags enemies whose armor and
arts resistance are strongly asymmetric, so one damage type is far more effective
against them than the other.

Each enemy yields two evidence rows -- one per stat, each carrying its own scalar at
its own emitted field path -- with the skew direction stated in the note.

Reads the typed ``defense`` + ``res`` stats only -- never a name or
description. Both stats must be present to judge a skew; a partially-typed enemy
(one stat missing) is not concluded from, it is recorded as a limitation.
The summary states which damage type the enemy resists (a fact), never prescribes
an operator. One enemy across several level variants counts once.
"""

from __future__ import annotations

from arknights_mcp.analyzers.base import (
    EvidenceItem,
    Observation,
    RuleResult,
    StageThreatContext,
)
from arknights_mcp.analyzers.rules._common import by_game_id, declined, distinct_refs

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
    """Flags enemies whose def/res asymmetry favors one damage type."""

    rule_id = RULE_ID

    def evaluate(self, ctx: StageThreatContext) -> RuleResult:
        evidence: list[EvidenceItem] = []
        limitations: list[str] = []
        confidence = 0.0

        for occ in by_game_id(ctx.occurrences):
            d, r = occ.defense, occ.res
            if d is None or r is None:
                if d is None and r is None:
                    # This used to be a bare ``continue``. The enemy the rule
                    # knows LEAST about was the one it said nothing about, so 64 real
                    # occurrences -- one of them a boss -- were indistinguishable from
                    # enemies that were assessed and found unskewed, while the one-stat
                    # case below refused out loud. Absence is not a negative.
                    limitations.append(
                        f"{occ.game_id}: def and res both missing; damage-type skew not assessed"
                    )
                    continue
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

            # One fact per row. This was a single row with an invented path
            # ("def/res") and a packed value ("def=1000,res=0") the client had to split
            # itself; both stats are separately emitted fields, so each gets its own row
            # carrying its own scalar, with the comparison stated in the shared note.
            evidence.append(EvidenceItem(ref=occ.game_id, field="def", value=d, note=direction))
            evidence.append(EvidenceItem(ref=occ.game_id, field="res", value=r, note=direction))
            confidence = max(confidence, _CONFIDENCE)

        if not evidence:
            # The refusals outlive the conclusion that would have carried them.
            return declined(limitations)

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
