"""Lane/route threat rule (§V6, §V26): flags stages whose enemies advance along
several distinct routes, so threats approach from multiple lanes at once and a
single defensive line cannot cover every path.

Reads the typed stage-level ``route_count`` (the number of distinct enemy routes)
and, as reinforcing evidence, each enemy's own ``route_count`` (how many routes it
splits across). No route data loaded -> the rule skips rather than concluding from
absent data (§V26). The headline is the distinct-route count -- a stage property,
not an enemy tally -- so §V35 does not conflate it with the evidence rows.
"""

from __future__ import annotations

from arknights_mcp.analyzers.base import (
    EvidenceItem,
    Observation,
    RuleResult,
    StageThreatContext,
)
from arknights_mcp.analyzers.rules._common import by_game_id, count_note

RULE_ID = "threat.lane_route"

#: At least this many distinct routes makes a stage multi-lane.
_MULTI_LANE = 2

_CONFIDENCE = 0.85  # authoritative typed route count


class LaneRouteRule:
    """Flags stages fielding enemies across multiple approach lanes (§V6, §V26)."""

    rule_id = RULE_ID

    def evaluate(self, ctx: StageThreatContext) -> RuleResult:
        route_count = ctx.route_count
        if route_count is None or route_count < _MULTI_LANE:
            return RuleResult()

        stage_ref = ctx.stage_code or "stage"
        evidence: list[EvidenceItem] = [
            EvidenceItem(
                ref=stage_ref,
                field="route_count",
                value=route_count,
                note=f"{route_count} distinct routes",
            )
        ]
        # Enemies that themselves split across several routes reinforce the threat.
        for occ in by_game_id(ctx.occurrences):
            if occ.route_count is not None and occ.route_count > 1:
                evidence.append(
                    EvidenceItem(
                        ref=occ.game_id,
                        field="route_count",
                        value=occ.route_count,
                        note=count_note(occ.total_count),
                    )
                )

        return RuleResult(
            observation=Observation(
                rule_id=RULE_ID,
                category="threat",
                tag="lane_route",
                title="Multiple approach lanes",
                summary=(
                    f"Stage fields {route_count} distinct enemy routes; threats approach from "
                    "several lanes at once, so a single defensive line cannot cover every path."
                ),
                confidence=_CONFIDENCE,
                evidence=tuple(evidence),
                limitations=(),
            )
        )
