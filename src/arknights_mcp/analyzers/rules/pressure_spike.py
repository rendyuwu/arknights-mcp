"""Spawn-pressure-spike threat rule (§V6, §V26): flags enemy types that arrive in a
concentrated burst -- many units of the same type within a short time window -- so
defenses face heavy pressure at once rather than a steady trickle.

Reads the typed occurrence fields ``total_count`` + ``first_spawn_time`` +
``last_spawn_time`` only (§V26). A high count with a *missing* spawn window is
reported at reduced confidence + a limitation (§V26); a high count spread over a
long window is not a spike and is skipped. One enemy across several level variants
counts once (§V35).
"""

from __future__ import annotations

from typing import Any

from arknights_mcp.analyzers.base import (
    EvidenceItem,
    Observation,
    RuleResult,
    StageThreatContext,
)
from arknights_mcp.analyzers.rules._common import by_game_id, distinct_refs

RULE_ID = "threat.pressure_spike"

#: A burst is at least this many spawns of one enemy type ...
_SPIKE_MIN_COUNT = 6
#: ... arriving within at most this many seconds (first -> last spawn).
_SPIKE_MAX_WINDOW = 12.0

_CONF_WINDOWED = 0.8  # count + a tight typed spawn window
_CONF_WINDOW_MISSING = 0.5  # count only; window unknown


class PressureSpikeRule:
    """Flags enemy types arriving in a concentrated spawn burst (§V6, §V26)."""

    rule_id = RULE_ID

    def evaluate(self, ctx: StageThreatContext) -> RuleResult:
        evidence: list[EvidenceItem] = []
        limitations: list[str] = []
        confidence = 0.0

        for occ in by_game_id(ctx.occurrences):
            count = occ.total_count
            if count is None or count < _SPIKE_MIN_COUNT:
                continue
            first, last = occ.first_spawn_time, occ.last_spawn_time

            note: str
            value: Any
            if first is not None and last is not None:
                window = max(last - first, 0.0)
                if window > _SPIKE_MAX_WINDOW:
                    continue  # many, but spread out -> steady pressure, not a spike
                value, note = count, f"{count} spawns within {window:g}s"
                conf = _CONF_WINDOWED
            else:
                value, note = count, f"{count} spawns; spawn window unknown"
                conf = _CONF_WINDOW_MISSING
                limitations.append(f"{occ.game_id}: spawn timing missing; burst window unconfirmed")

            evidence.append(
                EvidenceItem(ref=occ.game_id, field="total_count", value=value, note=note)
            )
            confidence = max(confidence, conf)

        if not evidence:
            return RuleResult()

        count_types = distinct_refs(evidence)
        types_word = "type" if count_types == 1 else "types"
        return RuleResult(
            observation=Observation(
                rule_id=RULE_ID,
                category="threat",
                tag="pressure_spike",
                title="Spawn pressure spike present",
                summary=(
                    f"Stage fields {count_types} enemy {types_word} arriving in a concentrated "
                    "burst; defenses face many units in a short window, not a steady trickle."
                ),
                confidence=confidence,
                evidence=tuple(evidence),
                limitations=tuple(limitations),
            )
        )
