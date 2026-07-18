"""Aerial threat rule: does a stage field flying enemies? (§V6, §V26).

Deterministic and typed-field-only -- decides from ``motion_type`` (a typed
enum value) and ``abilities`` (a typed id list), never from a name/description
string (§V26). A missing ``motion_type`` reduces confidence and is recorded as a
limitation; a ground ``motion_type`` that conflicts with an ``aerial`` ability is
omitted from the conclusion and surfaced as a warning (§V26).
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
    AERIAL_ABILITY,
    FLY_MOTIONS,
    GROUND_MOTIONS,
    ability_tokens,
    by_game_id,
    count_note,
    distinct_refs,
)

RULE_ID = "threat.aerial"

_CONF_MOTION_FLY = 0.9  # authoritative typed motion field
_CONF_ABILITY_ONLY = 0.6  # inferred from ability when motion_type is missing/unknown


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
        warnings: list[str] = []
        confidence = 0.0
        total_spawns = 0

        # Sort by game_id so evidence/warning order is deterministic (§V26).
        for occ in by_game_id(ctx.occurrences):
            motion = occ.motion_type.upper() if occ.motion_type else None
            abilities = ability_tokens(occ.abilities)
            has_aerial = abilities is not None and AERIAL_ABILITY in abilities

            deciding_field: str | None = None
            deciding_value: Any = None
            conf = 0.0

            if motion in FLY_MOTIONS:
                deciding_field = "motion_type"
                deciding_value = occ.motion_type
                conf = _CONF_MOTION_FLY
            elif motion in GROUND_MOTIONS:
                if has_aerial:
                    warnings.append(
                        f"{occ.game_id}: motion_type={occ.motion_type!r} conflicts with "
                        "'aerial' ability; omitted from aerial conclusion"
                    )
                continue
            elif has_aerial:
                # motion_type is missing or an unrecognized value, but a typed
                # 'aerial' ability is present -> infer flying at lower confidence.
                deciding_field = "abilities"
                deciding_value = AERIAL_ABILITY
                conf = _CONF_ABILITY_ONLY
                if motion is None:
                    limitations.append(
                        f"{occ.game_id}: motion_type missing; aerial inferred from 'aerial' ability"
                    )
                else:
                    limitations.append(
                        f"{occ.game_id}: unrecognized motion_type={occ.motion_type!r}; "
                        "aerial inferred from 'aerial' ability"
                    )

            if deciding_field is None:
                continue

            evidence.append(
                EvidenceItem(
                    ref=occ.game_id,
                    field=deciding_field,
                    value=deciding_value,
                    note=count_note(occ.total_count),
                )
            )
            confidence = max(confidence, conf)
            total_spawns += occ.total_count or 0

        if not evidence:
            return RuleResult(warnings=tuple(warnings))

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
        return RuleResult(observation=observation, warnings=tuple(warnings))
