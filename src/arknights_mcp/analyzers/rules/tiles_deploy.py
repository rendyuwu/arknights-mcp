"""Tiles/deploy threat rule (§V6, §V26): flags stages whose deploy surface is
constrained -- few high-ground (ranged) or ground (melee) buildable tiles -- so
there is little room to place units.

Reads the typed :class:`~arknights_mcp.analyzers.base.StageTiles` summary only
(§V26): counts derived from ``buildable_type`` + ``height_type``. A stage with no
tile data (``tiles is None``) or too few tiles to judge (a stub grid) is skipped
rather than concluded from (§V26). The summary states the tile counts as a fact,
never prescribes a squad (§V7). The headline numbers are tile counts, not enemy
tallies, so §V35 does not apply to them.
"""

from __future__ import annotations

from arknights_mcp.analyzers.base import (
    EvidenceItem,
    Observation,
    RuleResult,
    StageThreatContext,
)

RULE_ID = "threat.tiles_deploy"

#: Below this many tiles the grid is too small to judge a deploy surface (e.g. a
#: minimal fixture) -> the rule skips rather than flagging a stub as constrained.
_MIN_TILES = 8
#: At or below these counts the corresponding deploy surface is scarce.
_SCARCE_RANGED = 2
_SCARCE_MELEE = 3

_CONFIDENCE = 0.8  # authoritative typed tile counts


class TilesDeployRule:
    """Flags stages offering a constrained deployment surface (§V6, §V26)."""

    rule_id = RULE_ID

    def evaluate(self, ctx: StageThreatContext) -> RuleResult:
        tiles = ctx.tiles
        if tiles is None or tiles.total < _MIN_TILES:
            return RuleResult()

        scarce: list[str] = []
        if tiles.buildable_ranged <= _SCARCE_RANGED:
            scarce.append(f"high-ground (ranged) tiles: {tiles.buildable_ranged}")
        if tiles.buildable_melee <= _SCARCE_MELEE:
            scarce.append(f"ground (melee) tiles: {tiles.buildable_melee}")
        if not scarce:
            return RuleResult()

        # §V68/B136: ref = the stage's game_id, never the variant-shared stage_code.
        # §V101: the tile counts are emitted at stage.metrics, so the evidence names that
        # real path instead of a bare pseudo-field; the grid total was a number buried in
        # the note ("of 88 tiles") and is now its own row, one fact per row.
        evidence = (
            EvidenceItem(
                ref=ctx.stage_game_id,
                field="metrics.buildable_ranged",
                value=tiles.buildable_ranged,
            ),
            EvidenceItem(
                ref=ctx.stage_game_id,
                field="metrics.buildable_melee",
                value=tiles.buildable_melee,
            ),
            EvidenceItem(ref=ctx.stage_game_id, field="metrics.tile_total", value=tiles.total),
        )
        return RuleResult(
            observation=Observation(
                rule_id=RULE_ID,
                category="threat",
                tag="tiles_deploy",
                title="Constrained deployment surface",
                summary=(
                    "Stage offers few deployable tiles ("
                    + "; ".join(scarce)
                    + "), limiting where units can be placed."
                ),
                confidence=_CONFIDENCE,
                evidence=evidence,
                limitations=(),
            )
        )
