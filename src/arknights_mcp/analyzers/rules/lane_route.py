"""Lane/route threat rule: flags stages whose enemies advance
along several route records, so threats plausibly approach from more than one path
and a single defensive line may not cover every approach.

Reads the typed stage-level ``route_count`` (the number of raw enemy-route RECORDS)
and, as reinforcing evidence, each enemy's own ``route_count`` (how many route
records it splits across). No route data loaded -> the rule skips rather than
concluding from absent data.

The raw ``route_count`` is a count of route RECORDS, which often share
start/end/checkpoint geometry (4-4: 26 records, far fewer distinct lanes). It is
therefore NOT a player-facing lane tally. The context carries only the scalar count
(no route geometry to cluster into effective lanes), so this rule labels the evidence
"raw route records", records the limitation that the
raw count is not the distinct-lane count, and reports at reduced confidence -- never
headlining "N lanes". Same raw-field-not-a-semantic-claim class as the spawn-time window
or the map-defined flag. The headline is the stage-level record count -- a stage
property, not an enemy tally -- so the distinct-ref tally does not conflate it with
the evidence rows.
"""

from __future__ import annotations

from arknights_mcp.analyzers.base import (
    EvidenceItem,
    Observation,
    RuleResult,
    StageThreatContext,
)
from arknights_mcp.analyzers.rules._common import by_game_id, count_evidence, fuller_view_note

RULE_ID = "threat.lane_route"

#: At least this many raw route records makes a stage plausibly multi-lane.
_MULTI_LANE = 2

#: The raw route-record count overstates distinct lanes (records share
#: geometry) and the context carries no geometry to cluster -> the conclusion is a
#: plausible multi-path signal, not an authoritative lane measure, so confidence is
#: reduced from the old authoritative 0.85.
_CONFIDENCE = 0.5

#: Stamped on every firing so the client knows the raw record count is not
#: the distinct-lane count and the geometry was not clustered into effective lanes.
#:
#: It used to stop at "geometry not clustered", naming no next call. A live
#: client read that as the SERVER having no route geometry, said so, and offered the
#: Arknights Wiki instead -- while ``get_stage(include_routes)`` returns the clustered
#: geometries with start, end and checkpoints for every one of the 2453 EN / 2484 CN
#: stages this rule fires on. The limitation was true of this view and false about the
#: server, so it now routes.
_RAW_ROUTE_LIMITATION = fuller_view_note(
    this_view=(
        "The raw route-record count is not the number of distinct lanes, because "
        "records may share start, end and checkpoint geometry."
    ),
    flag="include_routes",
    fuller="the clustered route geometries",
)


class LaneRouteRule:
    """Flags stages fielding enemies across multiple route records."""

    rule_id = RULE_ID

    def evaluate(self, ctx: StageThreatContext) -> RuleResult:
        route_count = ctx.route_count
        if route_count is None or route_count < _MULTI_LANE:
            return RuleResult()

        # The stage-level row refs the stage's game_id, never its stage_code --
        # normal/tough/challenge variants share one code, so a "14-18" ref is undecidable
        # and un-joinable to the stage block, which is keyed on game_id (the item-drops
        # view fixed this; this surface kept the defect).
        evidence: list[EvidenceItem] = [
            # The number was duplicated into the note ("22 raw route records") --
            # the value already carries it, and "records, not lanes" is what the field
            # name and the standing limitation say, so the note holds no number at all.
            EvidenceItem(
                ref=ctx.stage_game_id,
                field="metrics.route_record_count",
                value=route_count,
            )
        ]
        # Enemies that themselves split across several route records reinforce the threat.
        for occ in by_game_id(ctx.occurrences):
            if occ.route_count is not None and occ.route_count > 1:
                evidence.append(
                    EvidenceItem(ref=occ.game_id, field="route_count", value=occ.route_count)
                )
                # The spawn count is a separate fact -> a separate row.
                count_row = count_evidence(occ)
                if count_row is not None:
                    evidence.append(count_row)

        return RuleResult(
            observation=Observation(
                rule_id=RULE_ID,
                category="threat",
                tag="lane_route",
                title="Multiple approach routes",
                # The "records may share geometry, so this is not a lane tally"
                # clause used to close this summary AND now opens the routing limitation
                # below, word for word. One fact, one home -- and the limitation is the
                # better one, because that is where the next call is named.
                # "raw enemy-route records" stays here: the headline must be LABELLED
                # as records, never as N lanes.
                summary=(
                    f"Stage carries {route_count} raw enemy-route records; enemies advance along "
                    "more than one path, so a single defensive line may not cover every approach."
                ),
                confidence=_CONFIDENCE,
                evidence=tuple(evidence),
                limitations=(_RAW_ROUTE_LIMITATION,),
            )
        )
