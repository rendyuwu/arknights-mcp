"""Spawn-pressure-spike threat rule (§V6, §V26, §V39): flags enemy types that arrive
in a possibly concentrated burst -- many units of the same type -- so defenses may
face heavy pressure at once rather than a steady trickle.

Reads the typed occurrence fields ``total_count`` + ``first_spawn_time`` +
``last_spawn_time`` only (§V26). ``first/last_spawn_time`` is fragment-relative
``preDelay`` aggregated as min/max across ALL waves, NOT elapsed stage time (B30,
§V39): an enemy trickled across many waves each at a low per-fragment ``preDelay``
collapses to a ~0 computed window. So the rule never concludes a confident burst
from that cross-wave window -- it reports the high count at reduced confidence with
a limitation that the window is fragment-relative and may overstate the burst
(§V39 pick (c)). A *missing* window is likewise reported at reduced confidence + a
limitation (§V26). A high count spread over a wide computed window is not a spike
and is skipped (a conservative decline, never a burst conclusion). One enemy across
several level variants counts once (§V35).
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
#: ... within at most this many seconds of *computed* window (first -> last spawn).
#: The window is fragment-relative, not elapsed (B30) -> used only as a conservative
#: gate to decline a wide spread, never as a confident burst measure.
_SPIKE_MAX_WINDOW = 12.0

#: The computed window is fragment-relative preDelay aggregated across waves, not
#: elapsed time (B30, §V39) -> even a "windowed" fire is uncertain, so both the
#: windowed and window-missing cases report at the same reduced confidence.
_CONF_WINDOWED = 0.5  # count + a tight *computed* (fragment-relative) window
_CONF_WINDOW_MISSING = 0.5  # count only; window unknown

#: §V39: the limitation stamped on a windowed fire so the client knows the window is
#: not elapsed time and may overstate the burst.
_FRAGMENT_WINDOW_LIMITATION = (
    "spawn window fragment-relative, aggregated across waves; may overstate burst"
)


class PressureSpikeRule:
    """Flags enemy types with a high spawn count as a possible burst (§V6, §V26, §V39)."""

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
                    continue  # wide computed spread -> conservative decline, not a spike
                # B30/§V39: the window is fragment-relative preDelay aggregated across
                # waves, not elapsed -> report the count but flag the window and reduce
                # confidence; never present it as a confirmed elapsed burst.
                value = count
                note = (
                    f"{count} spawns; computed window {window:g}s is fragment-relative, not elapsed"
                )
                conf = _CONF_WINDOWED
                limitations.append(f"{occ.game_id}: {_FRAGMENT_WINDOW_LIMITATION}")
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
                title="Possible spawn pressure spike",
                summary=(
                    f"Stage fields {count_types} enemy {types_word} with a high spawn count; "
                    "spawn timing is fragment-relative and aggregated across waves, so a "
                    "concentrated burst is possible but unconfirmed from typed fields."
                ),
                confidence=confidence,
                evidence=tuple(evidence),
                limitations=tuple(limitations),
            )
        )
