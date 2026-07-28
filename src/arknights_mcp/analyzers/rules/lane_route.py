"""Lane/route threat rule (§V6, §V26, §V49): flags stages whose enemies advance
along several route records, so threats plausibly approach from more than one path
and a single defensive line may not cover every approach.

Reads the typed stage-level ``route_count`` (the number of raw enemy-route RECORDS)
and, as reinforcing evidence, each enemy's own ``route_count`` (how many route
records it splits across). No route data loaded -> the rule skips rather than
concluding from absent data (§V26).

§V49/B43: the raw ``route_count`` is a count of route RECORDS, which often share
start/end/checkpoint geometry (4-4: 26 records, far fewer distinct lanes). It is
therefore NOT a player-facing lane tally. The context carries only the scalar count
(no route geometry to cluster into effective lanes), so this rule takes §V49 pick
(b): it labels the evidence "raw route records", records the limitation that the
raw count is not the distinct-lane count, and reports at reduced confidence -- never
headlining "N lanes". Same raw-field-not-a-semantic-claim class as §V39 (preDelay)
/ §V44 (m_defined). The headline is the stage-level record count -- a stage
property, not an enemy tally -- so §V35 does not conflate it with the evidence rows.
"""

from __future__ import annotations

from arknights_mcp.analyzers.base import (
    EvidenceItem,
    Observation,
    RuleResult,
    StageThreatContext,
)
from arknights_mcp.analyzers.rules._common import by_game_id, count_evidence

RULE_ID = "threat.lane_route"

#: At least this many raw route records makes a stage plausibly multi-lane.
_MULTI_LANE = 2

#: §V49/B43: the raw route-record count overstates distinct lanes (records share
#: geometry) and the context carries no geometry to cluster -> the conclusion is a
#: plausible multi-path signal, not an authoritative lane measure, so confidence is
#: reduced from the old authoritative 0.85.
_CONFIDENCE = 0.5

#: §V49/B43: stamped on every firing so the client knows the raw record count is not
#: the distinct-lane count and the geometry was not clustered into effective lanes.
_RAW_ROUTE_LIMITATION = "raw route count != distinct lanes; geometry not clustered"


class LaneRouteRule:
    """Flags stages fielding enemies across multiple route records (§V6, §V26, §V49)."""

    rule_id = RULE_ID

    def evaluate(self, ctx: StageThreatContext) -> RuleResult:
        route_count = ctx.route_count
        if route_count is None or route_count < _MULTI_LANE:
            return RuleResult()

        # §V68/B136: the stage-level row refs the stage's game_id, never its stage_code --
        # normal/tough/challenge variants share one code, so a "14-18" ref is undecidable
        # and un-joinable to the stage block, which is keyed on game_id (B57 fixed the
        # same defect on get_item_drops; this surface kept it).
        evidence: list[EvidenceItem] = [
            # §V101: the number was duplicated into the note ("22 raw route records") --
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
                # §V101: the spawn count is a separate fact -> a separate row.
                count_row = count_evidence(occ)
                if count_row is not None:
                    evidence.append(count_row)

        return RuleResult(
            observation=Observation(
                rule_id=RULE_ID,
                category="threat",
                tag="lane_route",
                title="Multiple approach routes",
                summary=(
                    f"Stage carries {route_count} raw enemy-route records; enemies advance along "
                    "more than one path, so a single defensive line may not cover every approach. "
                    "The raw record count overstates distinct lanes -- records may share "
                    "start/end/checkpoint geometry -- so it is not a lane tally."
                ),
                confidence=_CONFIDENCE,
                evidence=tuple(evidence),
                limitations=(_RAW_ROUTE_LIMITATION,),
            )
        )
