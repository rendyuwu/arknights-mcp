"""Stage drop-rate service (§T91): the shared ``get_stage_drops`` domain entry
point both transports call (§V14).

Given a read-only connection and a ``(server, stage)`` selector, it loads the
stage (region + provenance, §V5) and its penguin drop-rate cache (§V54), applies
the §V53 stale check (``now`` past a drop's ``expires_at`` -> ``data_stale``, the
drop still returned but flagged), and -- when ``include_efficiency`` is set --
runs the deterministic §T90 farming analyzer over the typed drop facts (§V55).
The service adds no natural-language interpretation of its own; every emitted
observation keeps the five §V6 fields the analyzer stamped.

Read-only + parameterized SQL only (§V2): the parameterized ``SELECT``s live in
:class:`~arknights_mcp.db.repositories.stages.StageRepository` (the stage +
``sanity_cost``, reusing the shared ``_resolve_stage`` selector, §V37) and
:class:`~arknights_mcp.db.repositories.drops.DropRepository` (the drop cache).
It does not open the connection; callers pass one in, so both transports share
this exact function (§V14). It never fetches penguin at query time (§V52/§V1) --
it only reads the cache a CLI sync/import promoted.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from arknights_mcp.analyzers.base import Observation
from arknights_mcp.analyzers.farming import (
    DropFact,
    FarmingContext,
    analyze_farming,
)
from arknights_mcp.db.repositories.drops import DropRepository, StageDropRow
from arknights_mcp.db.repositories.stages import StageRepository
from arknights_mcp.services.stages import (
    StageFacts,
    _resolve_stage,
    _stage_facts,
)

#: Typed outcome of a drop lookup. ``data_stale`` is reported when at least one
#: served drop is past its ``expires_at`` (§V53); ``not_found`` when the stage is
#: absent OR the stage has no drop cache (§V24).
StageDropsStatus = Literal["ok", "data_stale", "not_found"]


@dataclass(frozen=True)
class DropFacts:
    """One item's typed drop fact for the wire (no prose; §V16/§V18/§V54).

    ``expired`` is this row's own §V53 verdict (``now`` past ``expires_at``); a
    stale drop is still returned, flagged rather than withheld. ``snapshot_id`` +
    ``fetched_at`` + ``expires_at`` are the penguin provenance chain (§V54).
    """

    item_game_id: str
    item_display_name: str | None
    item_rarity: str | None
    item_type: str | None
    region: str
    quantity: int | None
    times: int | None
    drop_rate: float | None
    snapshot_id: str
    fetched_at: str
    expires_at: str
    expired: bool


@dataclass(frozen=True)
class StageDropsResult:
    """Domain result of :func:`get_stage_drops`.

    Carries region + provenance on ``stage`` (§V5) and the penguin drop facts with
    their OWN provenance chain (§V54). ``observations`` is populated only when
    ``include_efficiency`` was requested (§V55); ``analyzer_version`` is then the
    analyzer that produced them. ``stale`` mirrors the ``data_stale`` status so a
    caller need not re-scan the drops.
    """

    status: StageDropsStatus
    server: str
    stage: StageFacts | None
    drops: tuple[DropFacts, ...]
    observations: tuple[Observation, ...]
    warnings: tuple[str, ...]
    analyzer_version: str | None
    stale: bool


def _is_expired(expires_at: str, now: datetime) -> bool:
    """True when ``now`` is at/past the drop cache's ``expires_at`` (§V53).

    An unparseable timestamp is treated as expired (fail-closed: an unreadable
    expiry is never presented as fresh, §V53). ``expires_at`` is stamped by the
    importer as an aware ISO string, but a naive value is normalized to UTC so the
    comparison never mixes naive/aware operands (mirrors ``status._age_days``).
    """
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return now >= parsed


def _drop_facts(row: StageDropRow, now: datetime) -> DropFacts:
    """Shape a repository row into the typed, region-attributed drop fact (§V5/§V54)."""
    return DropFacts(
        item_game_id=row.item_game_id,
        item_display_name=row.item_display_name,
        item_rarity=row.item_rarity,
        item_type=row.item_type,
        region=row.region,
        quantity=row.quantity,
        times=row.times,
        drop_rate=row.drop_rate,
        snapshot_id=row.snapshot_id,
        fetched_at=row.fetched_at,
        expires_at=row.expires_at,
        expired=_is_expired(row.expires_at, now),
    )


def _not_found(server: str) -> StageDropsResult:
    return StageDropsResult(
        status="not_found",
        server=server,
        stage=None,
        drops=(),
        observations=(),
        warnings=(),
        analyzer_version=None,
        stale=False,
    )


def get_stage_drops(
    conn: sqlite3.Connection,
    *,
    server: str,
    stage_code: str | None = None,
    game_id: str | None = None,
    include_efficiency: bool = False,
    now: datetime | None = None,
) -> StageDropsResult:
    """Fetch one stage's penguin drop-rate facts + optional farming efficiency (§T91).

    Selected by ``game_id`` (preferred, the unique key) or ``stage_code``.
    Read-only; parameterized SQL only (§V2); never a query-time penguin fetch
    (§V52). Returns a :class:`StageDropsResult` with region + provenance on the
    stage (§V5) and the drop facts with their own penguin provenance chain (§V54).

    A drop served past its ``expires_at`` is still returned but flagged, and the
    result status is ``data_stale`` (§V53) -- never presented as fresh. When
    ``include_efficiency`` is set, the deterministic §T90 farming analyzer runs
    over the typed drop facts (sanity_cost / drop_rate); an expired cache downgrades
    every figure to a limitation (§V55). ``now`` is injectable for deterministic
    expiry testing; it defaults to the current UTC time. Both transports call this
    same function (§V14).
    """
    clock = now if now is not None else datetime.now(tz=UTC)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)

    stage = _resolve_stage(StageRepository(conn), server, stage_code=stage_code, game_id=game_id)
    if stage is None:
        return _not_found(server)

    drop_rows = DropRepository(conn).drops_for_stage(stage.stage_pk)
    if not drop_rows:
        # A stage with no drop cache asserts no drop fact -- report it as absent with
        # a suggested admin action (§V24), rather than an empty ``ok`` that reads as
        # "this stage drops nothing". The tool maps this to a not_found envelope.
        return _not_found(server)

    facts = tuple(_drop_facts(row, clock) for row in drop_rows)
    stale = any(f.expired for f in facts)

    observations: tuple[Observation, ...] = ()
    warnings: tuple[str, ...] = ()
    analyzer_version: str | None = None
    if include_efficiency:
        # §V55: deterministic sanity-per-item over typed fields only. A stale cache
        # is passed through so every efficiency figure is downgraded to a limitation
        # (never a fresh recommendation, §V53). ``sample_size`` = penguin ``times``.
        analysis = analyze_farming(
            FarmingContext(
                server=stage.server,
                stage_code=stage.stage_code,
                sanity_cost=stage.sanity_cost,
                drops=tuple(
                    DropFact(
                        item_game_id=f.item_game_id,
                        item_display_name=f.item_display_name,
                        drop_rate=f.drop_rate,
                        sample_size=f.times,
                    )
                    for f in facts
                ),
                expired=stale,
            )
        )
        observations = analysis.observations
        warnings = analysis.warnings
        analyzer_version = analysis.analyzer_version

    return StageDropsResult(
        status="data_stale" if stale else "ok",
        server=stage.server,
        stage=_stage_facts(stage),
        drops=facts,
        observations=observations,
        warnings=warnings,
        analyzer_version=analyzer_version,
        stale=stale,
    )
