"""Ranged-arts threat rule (§V6, §V26): flags enemies that deal arts damage from
range -- damage that ignores physical armor and lands from a distance.

Reads three typed fields, in a fixed order of authority (§V26, §T210/B160):

* ``damage_types`` -- must contain an arts/magical token, else the enemy is skipped.
  This replaced the retired ``attack_type`` scalar, which upstream stopped filling
  (``null`` on 1585/1585 handbook entries) and which could not have expressed the 42
  enemies that deal PHYSIC *and* MAGIC anyway.
* ``attack_range`` -- a measured radius. At or beyond :data:`_RANGED_MIN` it is the
  authoritative answer; a shorter one means the enemy has arts damage but no reach,
  which is not a ranged-arts threat and is skipped.
* ``targeting`` -- upstream's own ``applyWay`` token, read when the radius is absent
  and BEFORE any inference. This ordering is the rule (§T210 (b)): counted over the
  pinned EN snapshot, 383 enemies are arts-capable -- 259 carry a radius >= 1, 47 are
  at melee range, and 77 carry no radius at all. **55 of those 77 declare
  ``applyWay: MELEE``**, so falling straight from "no radius" to the §V26 inference
  would have published 55 enemies as ranged threats against the source's own word.

Only when all three leave reach undecided does the rule infer it at reduced
confidence plus a limitation (§V26). One enemy across several level variants counts
once (§V35). The summary states a fact, not a counter (§V7).
"""

from __future__ import annotations

from typing import Any

from arknights_mcp.analyzers.base import (
    EvidenceItem,
    Observation,
    RuleResult,
    StageThreatContext,
)
from arknights_mcp.analyzers.rules._common import by_game_id, count_evidence, distinct_refs

RULE_ID = "threat.ranged_arts"

#: Typed ``damage_types`` tokens (lowercased) that mean arts / magical damage. The
#: real EN handbook ships ``PHYSIC`` 1022, ``MAGIC`` 338, ``NO_DAMAGE`` 180, both 42,
#: ``HEAL`` 2 and ``MAGIC``+``HEAL`` 1 -- so ``magic`` is the live token and the rest
#: are kept for a shape upstream has used or may use again (§V96 counted, not guessed).
_ARTS_TYPES: frozenset[str] = frozenset({"magic", "magical", "arts"})
#: ``attack_range`` at or beyond this reaches past a melee blocker.
_RANGED_MIN = 1.0
#: ``targeting`` tokens (uppercased) that state the enemy strikes beyond melee.
_RANGED_TARGETING: frozenset[str] = frozenset({"RANGED", "ALL"})
#: ``targeting`` tokens that state it does not: ``MELEE`` reaches only what it blocks,
#: ``NONE`` states no attack targeting at all.
_MELEE_TARGETING: frozenset[str] = frozenset({"MELEE"})
_NO_TARGETING: frozenset[str] = frozenset({"NONE"})

_CONF_RANGED = 0.9  # authoritative: arts type + a measured range
_CONF_TARGETING = 0.8  # arts + the source's own reach token, but no measured radius
_CONF_RANGE_MISSING = 0.6  # arts confirmed, reach unstated by any field -> inferred


class RangedArtsRule:
    """Flags enemies dealing ranged arts damage (§V6, §V26)."""

    rule_id = RULE_ID

    def evaluate(self, ctx: StageThreatContext) -> RuleResult:
        evidence: list[EvidenceItem] = []
        limitations: list[str] = []
        warnings: list[str] = []
        confidence = 0.0

        for occ in by_game_id(ctx.occurrences):
            arts = _arts_token(occ.damage_types)
            if arts is None:
                continue
            rng = occ.attack_range
            targeting = occ.targeting.upper() if occ.targeting else None

            deciding_field: str
            deciding_value: Any
            note: str
            if rng is not None and rng >= _RANGED_MIN:
                deciding_field, deciding_value, note = "attack_range", rng, "arts damage at range"
                conf = _CONF_RANGED
            elif rng is not None:
                continue  # arts but melee range -> not a ranged-arts threat
            elif targeting in _RANGED_TARGETING:
                deciding_field, deciding_value = "targeting", occ.targeting
                note = "arts damage; the source states it strikes beyond melee"
                conf = _CONF_TARGETING
                limitations.append(
                    f"{occ.game_id}: attack_range missing; reach read from targeting, "
                    "so the distance is unknown"
                )
            elif targeting in _MELEE_TARGETING:
                continue  # the source's own reach token says melee -> not a ranged threat
            elif targeting in _NO_TARGETING:
                # §V26 conflicting typed fields: it deals arts damage yet states no
                # attack targeting at all. Omit the conclusion, say why.
                warnings.append(
                    f"{occ.game_id}: deals {arts} damage but targeting={occ.targeting!r} "
                    "states no attack reach; omitted from ranged-arts conclusion"
                )
                continue
            else:
                deciding_field, deciding_value = "damage_types", arts
                note = "arts damage; range unknown"
                conf = _CONF_RANGE_MISSING
                limitations.append(
                    f"{occ.game_id}: attack_range and targeting both missing; "
                    "ranged reach unconfirmed"
                )

            evidence.append(
                EvidenceItem(ref=occ.game_id, field=deciding_field, value=deciding_value, note=note)
            )
            # §V101: the spawn count used to be appended into this row's note as
            # "(total_count=7)" -- it is a fact of its own, so it is a row of its own and
            # the note is left as pure prose.
            count_row = count_evidence(occ)
            if count_row is not None:
                evidence.append(count_row)
            confidence = max(confidence, conf)

        if not evidence:
            return RuleResult(warnings=tuple(warnings))

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
            ),
            warnings=tuple(warnings),
        )


def _arts_token(damage_types: tuple[str, ...] | None) -> str | None:
    """The enemy's arts damage token as the source spells it, else ``None``.

    ``damage_types`` is a list because an enemy may deal several kinds at once, so a
    membership test is what decides -- never the first entry, which for the 42 dual
    enemies is ``PHYSIC`` and would hide the arts half.
    """
    if not damage_types:
        return None
    for token in damage_types:
        if token.lower() in _ARTS_TYPES:
            return token
    return None
