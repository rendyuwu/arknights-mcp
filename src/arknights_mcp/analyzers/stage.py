"""Stage threat analyzer: runs deterministic rules over a stage's typed enemy
occurrences and returns evidence-backed observations (§V6, §V26).

Pure and DB-free: the service layer (§T17) builds a :class:`StageThreatContext`
from the read-only DB and calls :func:`analyze_stage`. There is no
natural-language input -- rules read typed fields only (§V26).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from arknights_mcp.analyzers.base import (
    ANALYZER_VERSION,
    Observation,
    StageThreatContext,
    ThreatRule,
)
from arknights_mcp.analyzers.rules import THREAT_RULES


@dataclass(frozen=True)
class StageAnalysis:
    """Aggregate result of running every threat rule over one stage (§V6)."""

    server: str
    stage_code: str | None
    observations: tuple[Observation, ...]
    warnings: tuple[str, ...]
    analyzer_version: str = ANALYZER_VERSION


def analyze_stage(
    ctx: StageThreatContext,
    rules: Sequence[ThreatRule] = THREAT_RULES,
) -> StageAnalysis:
    """Run ``rules`` (default: the registry) over ``ctx`` deterministically."""
    observations: list[Observation] = []
    warnings: list[str] = []
    for rule in rules:
        result = rule.evaluate(ctx)
        if result.observation is not None:
            observations.append(result.observation)
        warnings.extend(result.warnings)
    return StageAnalysis(
        server=ctx.server,
        stage_code=ctx.stage_code,
        observations=tuple(observations),
        warnings=tuple(warnings),
    )
