"""Ranged-arts threat rule (§V6, §V26): flags enemies that deal arts damage from
range -- damage that ignores physical armor and lands from a distance.

Reads the typed ``attack_type`` (must be an arts/magical type) and ``attack_range``
(must reach beyond melee) only (§V26). Arts with a *missing* range is inferred at
reduced confidence + a limitation (§V26); arts at melee range (range present but
short) is not a ranged-arts threat and is skipped. One enemy across several level
variants counts once (§V35). The summary states a fact, not a counter (§V7).
"""

from __future__ import annotations

from typing import Any

from arknights_mcp.analyzers.base import (
    EvidenceItem,
    Observation,
    RuleResult,
    StageThreatContext,
)
from arknights_mcp.analyzers.rules._common import by_game_id, count_note, distinct_refs

RULE_ID = "threat.ranged_arts"

#: Typed ``attack_type`` values (lowercased) that mean arts / magical damage.
_ARTS_TYPES: frozenset[str] = frozenset({"magic", "magical", "arts"})
#: ``attack_range`` at or beyond this reaches past a melee blocker.
_RANGED_MIN = 1.0

_CONF_RANGED = 0.9  # authoritative: arts type + typed range
_CONF_RANGE_MISSING = 0.6  # arts confirmed, range unknown -> inferred reach


class RangedArtsRule:
    """Flags enemies dealing ranged arts damage (§V6, §V26)."""

    rule_id = RULE_ID

    def evaluate(self, ctx: StageThreatContext) -> RuleResult:
        evidence: list[EvidenceItem] = []
        limitations: list[str] = []
        confidence = 0.0

        for occ in by_game_id(ctx.occurrences):
            at = occ.attack_type.lower() if occ.attack_type else None
            if at is None or at not in _ARTS_TYPES:
                continue
            rng = occ.attack_range

            deciding_field: str
            deciding_value: Any
            note: str
            if rng is not None and rng >= _RANGED_MIN:
                deciding_field, deciding_value, note = "attack_range", rng, "arts damage at range"
                conf = _CONF_RANGED
            elif rng is None:
                deciding_field, deciding_value = "attack_type", occ.attack_type
                note = "arts damage; range unknown"
                conf = _CONF_RANGE_MISSING
                limitations.append(f"{occ.game_id}: attack_range missing; ranged reach unconfirmed")
            else:
                continue  # arts but melee range -> not a ranged-arts threat

            cnote = count_note(occ.total_count)
            evidence.append(
                EvidenceItem(
                    ref=occ.game_id,
                    field=deciding_field,
                    value=deciding_value,
                    note=f"{note} ({cnote})" if cnote else note,
                )
            )
            confidence = max(confidence, conf)

        if not evidence:
            return RuleResult()

        count = distinct_refs(evidence)
        types_word = "type" if count == 1 else "types"
        return RuleResult(
            observation=Observation(
                rule_id=RULE_ID,
                category="threat",
                tag="ranged_arts",
                title="Ranged arts damage present",
                summary=(
                    f"Stage fields {count} enemy {types_word} dealing ranged arts damage, "
                    "which ignores physical armor and strikes from a distance."
                ),
                confidence=confidence,
                evidence=tuple(evidence),
                limitations=tuple(limitations),
            )
        )
