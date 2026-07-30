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

from arknights_mcp.analyzers.base import (
    EvidenceItem,
    Observation,
    RuleResult,
    StageThreatContext,
)
from arknights_mcp.analyzers.rules._common import by_game_id, distinct_refs, fuller_view_note

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
#:
_FRAGMENT_WINDOW_LIMITATION = (
    "spawn window fragment-relative, aggregated across waves; may overstate burst"
)

#: §V108/B153 class: the window above is a min/max AGGREGATE over every wave -- a
#: deliberately coarse figure whose per-wave detail (each spawn's own ``spawn_time``,
#: ``interval`` and wave grouping) is what would settle whether the burst is real. That
#: detail is on ``get_stage(include_spawns)`` for all 1124 EN / 1149 CN stages this arm
#: fires on, so the caveat alone left the client with no way to resolve it.
#:
#: §V66: it rides the observation ONCE, not once per enemy. The caveat above is
#: enemy-specific and stays per row; the route is the same sentence for every enemy in
#: the stage, so repeating it would be N copies of one pointer.
_SPAWN_TIMELINE_ROUTE = fuller_view_note(
    this_view=(
        "The window is aggregated across waves and is relative to each wave's own start, "
        "not to elapsed stage time."
    ),
    flag="include_spawns",
    fuller="the per-wave spawn timeline",
)


class PressureSpikeRule:
    """Flags enemy types with a high spawn count as a possible burst (§V6, §V26, §V39)."""

    rule_id = RULE_ID

    def evaluate(self, ctx: StageThreatContext) -> RuleResult:
        evidence: list[EvidenceItem] = []
        limitations: list[str] = []
        confidence = 0.0
        windowed = False

        for occ in by_game_id(ctx.occurrences):
            count = occ.total_count
            if count is None or count < _SPIKE_MIN_COUNT:
                continue
            first, last = occ.first_spawn_time, occ.last_spawn_time

            # §V101/B137: the note used to read "13 spawns; computed window 7s is
            # fragment-relative, not elapsed" -- it restated the typed value AND buried a
            # second number the client had to parse out. The spawn count is the row's own
            # value; the window's two operands are separately emitted fields, so each is
            # its own row and the client derives the window itself. What the window MEANS
            # (fragment-relative, may overstate the burst) is prose, and it already rides
            # the per-enemy §V39 limitation, so no note is needed on any row.
            spawn_bounds: tuple[EvidenceItem, ...] = ()
            if first is not None and last is not None:
                window = max(last - first, 0.0)
                if window > _SPIKE_MAX_WINDOW:
                    continue  # wide computed spread -> conservative decline, not a spike
                # B30/§V39: the window is fragment-relative preDelay aggregated across
                # waves, not elapsed -> report the count but flag the window and reduce
                # confidence; never present it as a confirmed elapsed burst.
                spawn_bounds = (
                    EvidenceItem(ref=occ.game_id, field="first_spawn_time", value=first),
                    EvidenceItem(ref=occ.game_id, field="last_spawn_time", value=last),
                )
                conf = _CONF_WINDOWED
                limitations.append(f"{occ.game_id}: {_FRAGMENT_WINDOW_LIMITATION}")
                windowed = True
            else:
                conf = _CONF_WINDOW_MISSING
                limitations.append(f"{occ.game_id}: spawn timing missing; burst window unconfirmed")

            evidence.append(EvidenceItem(ref=occ.game_id, field="total_count", value=count))
            evidence.extend(spawn_bounds)
            confidence = max(confidence, conf)

        if not evidence:
            return RuleResult()

        # §V108/§V66: one route to the per-wave timeline for the whole observation, and
        # only when a window was actually computed -- the count-only arm has no window to
        # qualify, so pointing it at the timeline would answer a question it never raised.
        if windowed:
            limitations.append(_SPAWN_TIMELINE_ROUTE)

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
