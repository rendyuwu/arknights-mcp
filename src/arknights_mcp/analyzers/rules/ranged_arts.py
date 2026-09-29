"""Ranged-arts threat rule: flags enemies that deal arts damage from
range -- damage that ignores physical armor and lands from a distance.

Reads three typed fields, in a fixed order of authority:

* ``damage_types`` -- must contain an arts/magical token, else the enemy is skipped.
  This replaced the retired ``attack_type`` scalar, which upstream stopped filling
  (``null`` on 1585/1585 handbook entries) and which could not have expressed the 42
  enemies that deal PHYSIC *and* MAGIC anyway. It is the rule's GATE field: a
  list that is PRESENT and holds no arts token is a real negative and is skipped in
  silence, an ABSENT one is a refusal and says so.
* ``attack_range`` -- a measured radius. At or beyond :data:`_RANGED_MIN` it is the
  authoritative answer; a shorter one means the enemy has arts damage but no reach,
  which is not a ranged-arts threat and is skipped.
* ``attack_range_declared_none`` -- whether the ABSENCE of that radius is an answer.
  Upstream's ``-1.0`` sentinel says "this enemy has no attack radius"; the distance
  column drops it, so before the sentinel split it arrived here as the same NULL as "never
  stated". It is read BEFORE ``targeting``, because a rule that concludes
  from ``targeting`` here publishes a true conclusion under a false reason -- "attack_range
  missing" for a cell the source FILLED -- on the 13 EN / 16 CN arts enemies that carry the
  sentinel AND declare ``applyWay: RANGED|ALL``. Two typed fields disagreeing is a conflict,
  so those are warned and omitted, exactly like the ``targeting: NONE`` case.
* ``targeting`` -- upstream's own ``applyWay`` token, read when the radius is absent and
  unstated, and BEFORE any inference. This ordering is the rule: counted over
  the pinned EN snapshot, 383 enemies are arts-capable -- 259 carry a radius >= 1, 47 are
  at melee range, and 77 carry no radius at all. **55 of those 77 declare
  ``applyWay: MELEE``**, so falling straight from "no radius" to the inference
  would have published 55 enemies as ranged threats against the source's own word.

Only when all four leave reach undecided does the rule infer it at reduced
confidence plus a limitation. One enemy across several level variants counts
once. The summary states a fact, not a counter.
"""

from __future__ import annotations

from typing import Any

from arknights_mcp.analyzers.base import (
    EvidenceItem,
    Observation,
    RuleResult,
    StageThreatContext,
)
from arknights_mcp.analyzers.rules._common import (
    by_game_id,
    count_evidence,
    declined,
    distinct_refs,
)

RULE_ID = "threat.ranged_arts"

#: Typed ``damage_types`` tokens (lowercased) that mean arts / magical damage. The
#: real EN handbook ships ``PHYSIC`` 1022, ``MAGIC`` 338, ``NO_DAMAGE`` 180, both 42,
#: ``HEAL`` 2 and ``MAGIC``+``HEAL`` 1 -- so ``magic`` is the live token and the rest
#: are kept for a shape upstream has used or may use again (counted, not guessed).
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
    """Flags enemies dealing ranged arts damage."""

    rule_id = RULE_ID

    def evaluate(self, ctx: StageThreatContext) -> RuleResult:
        evidence: list[EvidenceItem] = []
        limitations: list[str] = []
        warnings: list[str] = []
        confidence = 0.0

        for occ in by_game_id(ctx.occurrences):
            if occ.damage_types is None:
                # Absent is not "no arts damage". This case used to fall into
                # the same silent ``continue`` as an enemy whose typed damage kinds were
                # present and simply held no arts token -- 227 occurrences over 181 stages,
                # every one of them an enemy the handbook never described at all (its
                # display_name is NULL too), reported as if it had been cleared.
                limitations.append(
                    f"{occ.game_id}: damage_types missing; not judged as ranged arts"
                )
                continue
            arts = _arts_token(occ.damage_types)
            if arts is None:
                continue  # typed damage kinds present, none of them arts -> a real negative
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
            elif occ.attack_range_declared_none:
                # The radius is not missing, it was DENIED -- the source
                # answered "no attack radius" and that mask is kept out of the distance
                # column. So no reach may be concluded here, and no limitation may call
                # the cell missing. When another typed field disagrees, that disagreement
                # is the finding: warn, omit, and say what each field stated -- never
                # what the sentinel might mean.
                if targeting in _RANGED_TARGETING:
                    warnings.append(
                        f"{occ.game_id}: deals {arts} damage; the source declares no attack "
                        f"radius for it yet targeting={occ.targeting!r} states it strikes "
                        "beyond melee; conflicting source fields, omitted from ranged-arts "
                        "conclusion"
                    )
                elif targeting in _NO_TARGETING:
                    warnings.append(_no_reach_warning(occ.game_id, arts, occ.targeting))
                continue
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
                # Conflicting typed fields: it deals arts damage yet states no
                # attack targeting at all. Omit the conclusion, say why.
                warnings.append(_no_reach_warning(occ.game_id, arts, occ.targeting))
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
            # The spawn count used to be appended into this row's note as
            # "(total_count=7)" -- it is a fact of its own, so it is a row of its own and
            # the note is left as pure prose.
            count_row = count_evidence(occ)
            if count_row is not None:
                evidence.append(count_row)
            confidence = max(confidence, conf)

        if not evidence:
            # The conflict warnings already survived a rule that concludes
            # nothing; the refusals now travel the same channel instead of being dropped.
            return declined(limitations, warnings)

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


def _no_reach_warning(game_id: str, arts: str, targeting: str | None) -> str:
    """The "arts damage but no attack reach" conflict, one home.

    Reached from two arms -- a radius that was never stated, and one the source DENIED --
    because the conflict is the same either way: the enemy deals arts damage while
    ``targeting`` states it reaches nothing. Wording it once keeps the two arms from
    drifting into two descriptions of one finding.
    """
    return (
        f"{game_id}: deals {arts} damage but targeting={targeting!r} "
        "states no attack reach; omitted from ranged-arts conclusion"
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
