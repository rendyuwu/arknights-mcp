"""Stage threat analyzer: runs deterministic rules over a stage's typed enemy
occurrences and returns evidence-backed observations (§V6, §V26).

It also owns the one disclosure no single rule can make: when the stage's substrate --
enemies, tiles, routes -- was never imported, the empty analysis is a gap in the data,
not a clear stage (§V118 c/B165).

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

#: §V118 (c)/B165: the substring that identifies the substrate-absence disclosure below.
#: Exported so the §V117 sweep can map this warning to its declared home instead of
#: reporting it as an undeclared rule arm.
SUBSTRATE_ABSENT_MARKER = "has been imported for this stage"


def _substrate_warning(ctx: StageThreatContext) -> str | None:
    """The one sentence a stage owes when a rule input was never IMPORTED (§V118 c).

    1749 of the 6716 stages on the promoted build carry no enemy rows, no tiles and no
    routes -- their level file was never imported -- and ``analyze_stage`` answered them
    with ``ok``, zero observations and zero warnings (B165). "No observations" is then
    read as "no threats", which is the B155/B153 shape: the client concludes the server
    does not have the data and reaches for a wiki, on a stage nothing was read from.

    One sentence naming which substrates are absent, not six rules each reporting their
    own half (§V66). It states the gap as a gap -- rules that had nothing to read -- and
    never as a property of the stage (§V7).
    """
    absent = [
        name
        for name, missing in (
            ("enemy", not ctx.occurrences),
            ("tile", ctx.tiles is None),
            ("route", not ctx.route_count),
        )
        if missing
    ]
    if not absent:
        return None
    listed = absent[0] if len(absent) == 1 else f"{', '.join(absent[:-1])} or {absent[-1]}"
    return (
        f"No {listed} data {SUBSTRATE_ABSENT_MARKER}, so the threat rules that read it had "
        "nothing to judge; that is a gap in the imported data, not a finding that the stage "
        "is clear."
    )


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
    # §V118 (c): the absent-substrate disclosure leads, because it explains why the rules
    # below it concluded little or nothing.
    substrate = _substrate_warning(ctx)
    if substrate is not None:
        warnings.append(substrate)
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
