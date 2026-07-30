"""Aerial threat rule: does a stage field flying enemies? (§V6, §V26).

Deterministic and typed-field-only -- decides from ``motion_type`` (a typed enum
value), never from a name/description string (§V26). A ``motion_type`` that is
missing or outside the known vocabulary yields no conclusion for that enemy and is
recorded as a limitation (§V26): the rule reports which enemies it could not judge
rather than judging them.

§T210 (c)/B160: this rule used to carry a second arm that inferred flight from an
``aerial`` ability token, and a third that warned when a ground motion contradicted
one. Both are gone with the ``abilities`` field itself -- no real source has ever
carried a typed ability vocabulary, so that arm could not fire on any build ever
promoted while the arm's tests passed on synthetic input. ``motion_type`` is the
authoritative field and it is populated on 3879/3879 real enemies (WALK 3547, FLY
332), which is why this rule is the one member of B160's four that kept firing.

§T213/B164: firing is not the same as judging. Both limitation arms below are
``dead_today`` -- 1030 observations on the promoted build, zero limitations between
them -- and the second was worse than quiet. It was unfirable, because the motion
vocabulary in :mod:`._common` declared ten tokens over a corpus that sends two, so
each guessed token concluded the case this arm exists to refuse. The vocabulary is
now the counted domain and both arms are declared, with their counts, in
:data:`~arknights_mcp.analyzers.rules.RULE_LIMITATION_ARMS` (§V117).
"""

from __future__ import annotations

from arknights_mcp.analyzers.base import (
    EvidenceItem,
    Observation,
    RuleResult,
    StageThreatContext,
)
from arknights_mcp.analyzers.rules._common import (
    FLY_MOTIONS,
    GROUND_MOTIONS,
    by_game_id,
    count_evidence,
    declined,
    distinct_refs,
)

RULE_ID = "threat.aerial"

_CONF_MOTION_FLY = 0.9  # authoritative typed motion field


def _summary(flyer_types: int, total_spawns: int) -> str:
    types_word = "type" if flyer_types == 1 else "types"
    return (
        f"Stage fields {flyer_types} aerial enemy {types_word} "
        f"({total_spawns} total spawns); ground-only defenses cannot engage them."
    )


class AerialThreatRule:
    """Flags stages that field flying enemies, with per-enemy evidence."""

    rule_id = RULE_ID

    def evaluate(self, ctx: StageThreatContext) -> RuleResult:
        evidence: list[EvidenceItem] = []
        limitations: list[str] = []
        confidence = 0.0
        total_spawns = 0

        # Sort by game_id so evidence/limitation order is deterministic (§V26).
        for occ in by_game_id(ctx.occurrences):
            motion = occ.motion_type.upper() if occ.motion_type else None

            if motion in GROUND_MOTIONS:
                continue
            if motion not in FLY_MOTIONS:
                # §V26: absent or unrecognized -> no conclusion, and say so rather than
                # defaulting the enemy to ground.
                if motion is None:
                    limitations.append(f"{occ.game_id}: motion_type missing; not judged as aerial")
                else:
                    limitations.append(
                        f"{occ.game_id}: unrecognized motion_type={occ.motion_type!r}; "
                        "not judged as aerial"
                    )
                continue

            evidence.append(
                EvidenceItem(ref=occ.game_id, field="motion_type", value=occ.motion_type)
            )
            # §V101: the spawn count is a fact with its own field path -> its own row,
            # never a "total_count=7" note welded onto the deciding row.
            count_row = count_evidence(occ)
            if count_row is not None:
                evidence.append(count_row)
            confidence = max(confidence, _CONF_MOTION_FLY)
            total_spawns += occ.total_count or 0

        if not evidence:
            # §V118 (b)/B165: a stage with no confirmed flyer used to discard the "could
            # not judge this enemy" rows with the observation, so both arms below were
            # narrower than they read -- they needed an unjudgeable enemy AND a flyer in
            # the same stage. The refusals now ride out on their own.
            return declined(limitations)

        # One enemy that appears at several level variants yields several evidence
        # items with the same ``ref``; the headline counts *distinct* enemies, not
        # evidence rows, so a single flyer is not reported as multiple types (§V35).
        distinct_flyers = distinct_refs(evidence)
        observation = Observation(
            rule_id=RULE_ID,
            category="threat",
            tag="aerial",
            title="Aerial enemies present",
            summary=_summary(distinct_flyers, total_spawns),
            confidence=confidence,
            evidence=tuple(evidence),
            limitations=tuple(limitations),
        )
        return RuleResult(observation=observation)
