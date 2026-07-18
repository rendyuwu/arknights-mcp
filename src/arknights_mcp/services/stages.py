"""Internal stage analysis service (§T17): the single domain entry point both
transports call to analyze a stage (§V14).

Given a read-only SQLite connection and a ``(server, stage)`` selector, it loads
the stage facts + region + provenance (§V5) and the stage's typed enemy
occurrences, builds a :class:`~arknights_mcp.analyzers.base.StageThreatContext`,
and runs the deterministic threat analyzer. Every observation it returns keeps
the five §V6 fields the analyzer stamped (``rule_id`` + evidence + confidence +
limitations + ``analyzer_version``); the service adds no natural-language
interpretation of its own.

Read-only + parameterized SQL only (§V2): the parameterized ``SELECT``s live in
:class:`~arknights_mcp.db.repositories.stages.StageRepository` (§T20), the sole
sanctioned SQL surface; this service only reads through it and never mutates the
database. It does not open the connection (the read-only connection factory is
:func:`~arknights_mcp.db.connection.open_read_only`); callers pass one in, so
both transports share this exact function (§V14). No transport-specific logic
lives here.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal

from arknights_mcp.analyzers import (
    EnemyOccurrence,
    Observation,
    StageThreatContext,
)
from arknights_mcp.analyzers import (
    analyze_stage as run_threat_analysis,
)
from arknights_mcp.db.repositories.stages import StageRepository, StageRow
from arknights_mcp.models.common import PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX

#: Typed outcome of a stage lookup. The full §V23 status vocabulary is wired
#: into the tool envelope in §T29; the M0 service reports only these two.
StageAnalysisStatus = Literal["ok", "not_found"]


@dataclass(frozen=True)
class StageProvenance:
    """Region-scoped provenance for a factual stage response (§V5)."""

    snapshot_id: str
    imported_at: str


@dataclass(frozen=True)
class StageFacts:
    """Typed, allowlisted facts about one stage (no prose; §V16, §V18)."""

    server: str
    game_id: str
    stage_code: str | None
    display_name: str | None
    zone_game_id: str | None
    stage_type: str | None
    difficulty: str | None
    sanity_cost: int | None
    recommended_level: int | None
    max_life_points: int | None
    provenance: StageProvenance


@dataclass(frozen=True)
class EnemyOccurrenceFacts:
    """One enemy's typed appearance in the stage (from ``stage_enemies``)."""

    game_id: str
    display_name: str | None
    enemy_class: str | None
    is_boss: bool
    is_elite: bool
    motion_type: str | None
    attack_type: str | None
    level_variant: int
    total_count: int | None
    first_spawn_time: float | None
    last_spawn_time: float | None
    route_count: int | None


@dataclass(frozen=True)
class StageAnalysisResult:
    """Domain result of the stage analysis service.

    Carries region (``server``) + provenance on the facts (§V5) and the
    evidence-backed observations (§V6). ``status == "not_found"`` implies
    ``stage is None`` and empty occurrences/observations.
    """

    status: StageAnalysisStatus
    server: str
    stage: StageFacts | None
    occurrences: tuple[EnemyOccurrenceFacts, ...]
    observations: tuple[Observation, ...]
    warnings: tuple[str, ...]
    analyzer_version: str | None


def _parse_abilities(raw: str | None) -> tuple[str, ...] | None:
    """Decode ``enemy_levels.abilities_json`` preserving the §V26 missing/empty
    distinction the analyzer relies on: SQL ``NULL`` -> ``None`` (field absent),
    ``"[]"`` -> ``()`` (present but empty)."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return tuple(str(a) for a in data)


def _stage_facts(stage: StageRow) -> StageFacts:
    """Shape a repository row into the typed, region-attributed facts (§V5/§V37).

    Single home for the ``StageRow -> StageFacts`` mapping shared by
    :func:`analyze_stage` and :func:`get_stage`; the two joins backing ``stage``
    are NOT NULL, so ``provenance`` (snapshot_id + imported_at) is always present.
    """
    return StageFacts(
        server=stage.server,
        game_id=stage.game_id,
        stage_code=stage.stage_code,
        display_name=stage.display_name,
        zone_game_id=stage.zone_game_id,
        stage_type=stage.stage_type,
        difficulty=stage.difficulty,
        sanity_cost=stage.sanity_cost,
        recommended_level=stage.recommended_level,
        max_life_points=stage.max_life_points,
        provenance=StageProvenance(snapshot_id=stage.snapshot_id, imported_at=stage.imported_at),
    )


def _resolve_stage(
    repo: StageRepository,
    server: str,
    *,
    stage_code: str | None,
    game_id: str | None,
) -> StageRow | None:
    """Resolve a stage by ``game_id`` (preferred, unique) or ``stage_code`` (§V37).

    Single home for the selector shared by :func:`analyze_stage` and
    :func:`get_stage`. Raises :class:`ValueError` when neither is given (the tool
    models require exactly one, but a direct caller must fail loudly, not silently).
    """
    if game_id is not None:
        return repo.stage_by_game_id(server, game_id)
    if stage_code is not None:
        return repo.stage_by_code(server, stage_code)
    raise ValueError("stage lookup requires stage_code or game_id")


def _not_found(server: str) -> StageAnalysisResult:
    return StageAnalysisResult(
        status="not_found",
        server=server,
        stage=None,
        occurrences=(),
        observations=(),
        warnings=(),
        analyzer_version=None,
    )


def analyze_stage(
    conn: sqlite3.Connection,
    *,
    server: str,
    stage_code: str | None = None,
    game_id: str | None = None,
) -> StageAnalysisResult:
    """Analyze one stage for ``server``, selected by ``game_id`` (preferred, the
    unique key) or ``stage_code``. Read-only; parameterized SQL only (§V2).

    Returns a :class:`StageAnalysisResult` with region + provenance on the facts
    (§V5) and the analyzer's evidence-backed observations (§V6). Both transports
    call this same function (§V14).
    """
    repo = StageRepository(conn)
    stage = _resolve_stage(repo, server, stage_code=stage_code, game_id=game_id)

    if stage is None:
        return _not_found(server)

    facts = _stage_facts(stage)

    occurrences: list[EnemyOccurrenceFacts] = []
    threat_inputs: list[EnemyOccurrence] = []
    for enemy in repo.stage_enemies(stage.stage_pk):
        occurrences.append(
            EnemyOccurrenceFacts(
                game_id=enemy.game_id,
                display_name=enemy.display_name,
                enemy_class=enemy.enemy_class,
                is_boss=enemy.is_boss,
                is_elite=enemy.is_elite,
                motion_type=enemy.motion_type,
                attack_type=enemy.attack_type,
                level_variant=enemy.level_variant,
                total_count=enemy.total_count,
                first_spawn_time=enemy.first_spawn_time,
                last_spawn_time=enemy.last_spawn_time,
                route_count=enemy.route_count,
            )
        )
        threat_inputs.append(
            EnemyOccurrence(
                game_id=enemy.game_id,
                display_name=enemy.display_name,
                motion_type=enemy.motion_type,
                attack_type=enemy.attack_type,
                abilities=_parse_abilities(enemy.abilities_json),
                total_count=enemy.total_count,
            )
        )

    analysis = run_threat_analysis(
        StageThreatContext(
            server=stage.server,
            stage_code=stage.stage_code,
            occurrences=tuple(threat_inputs),
        )
    )

    return StageAnalysisResult(
        status="ok",
        server=stage.server,
        stage=facts,
        occurrences=tuple(occurrences),
        observations=analysis.observations,
        warnings=analysis.warnings,
        analyzer_version=analysis.analyzer_version,
    )


# --- get_stage (§T34): facts by default; heavy map/routes/spawns opt-in + paged.


@dataclass(frozen=True)
class SectionPage:
    """Bounded page descriptor for one opt-in section (§V19/§V22).

    ``total`` is the full row count; ``has_more`` signals another bounded page
    (never an invitation to dump). Mirrors
    :class:`~arknights_mcp.models.common.PageInfo` on the wire.
    """

    page: int
    page_size: int
    total: int
    has_more: bool


@dataclass(frozen=True)
class StageMapFacts:
    """The stage's map header; tiles are delivered as a separate paged section."""

    width: int | None
    height: int | None
    map_version: str | None
    environment: object | None


@dataclass(frozen=True)
class TileFacts:
    """One tile of the stage grid (already allowlisted + sanitized; §V18)."""

    x: int
    y: int
    tile_key: str | None
    height_type: str | None
    buildable_type: str | None
    passable: bool | None


@dataclass(frozen=True)
class RouteFacts:
    """One enemy route; positions decoded from the stored (sanitized) JSON."""

    route_index: int
    start_position: object | None
    end_position: object | None
    checkpoints: object | None


@dataclass(frozen=True)
class SpawnFacts:
    """One scheduled spawn on the stage timeline (typed structural fields only)."""

    wave_index: int
    enemy_game_id: str
    enemy_level_variant: int | None
    route_index: int | None
    spawn_time: float | None
    count: int | None
    interval: float | None
    spawn_group: str | None
    hidden: bool


@dataclass(frozen=True)
class StageDetailResult:
    """Domain result of :func:`get_stage`.

    Carries region + provenance on ``stage`` (§V5). The heavy sections are
    populated only when their include flag is set; each populated section pairs
    its rows with a bounded :class:`SectionPage` (§V19/§V22). ``status ==
    "not_found"`` implies every section is empty/``None``.
    """

    status: StageAnalysisStatus
    server: str
    stage: StageFacts | None
    stage_map: StageMapFacts | None
    tiles: tuple[TileFacts, ...]
    tiles_page: SectionPage | None
    routes: tuple[RouteFacts, ...]
    routes_page: SectionPage | None
    spawns: tuple[SpawnFacts, ...]
    spawns_page: SectionPage | None


def _json_load(raw: str | None) -> object | None:
    """Decode a stored (sanitized) JSON fragment back to a Python object.

    The value was allowlisted + sanitized at import (§V18/§V31), so decoding it
    here re-exposes only vetted structural data. A ``NULL`` column or an
    undecodable string maps to ``None`` (absent), never a raw string leak.
    """
    if raw is None:
        return None
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return decoded


def _validate_page(page: int, page_size: int) -> tuple[int, int]:
    """Reject out-of-range pagination -- never silently widen it (§V19).

    Mirrors :class:`~arknights_mcp.models.common.PageParams` (``page >= 1``,
    ``1 <= page_size <= PAGE_SIZE_MAX``): the model is the MCP gate, but a caller
    reaching this service directly (or a transport skipping model validation) must
    get the *same* rejection, not a silent clamp -- one §V19 contract, both places.
    """
    p, size = int(page), int(page_size)
    if p < 1:
        raise ValueError(f"page {p} must be >= 1 (§V19)")
    if size < 1 or size > PAGE_SIZE_MAX:
        raise ValueError(f"page_size {size} outside the §V19 window [1, {PAGE_SIZE_MAX}]")
    return p, size


def _section_page(page: int, page_size: int, total: int) -> SectionPage:
    """Build the §V19 page descriptor. ``has_more`` is purely count-derived."""
    return SectionPage(
        page=page,
        page_size=page_size,
        total=total,
        has_more=page * page_size < total,
    )


def _not_found_detail(server: str) -> StageDetailResult:
    return StageDetailResult(
        status="not_found",
        server=server,
        stage=None,
        stage_map=None,
        tiles=(),
        tiles_page=None,
        routes=(),
        routes_page=None,
        spawns=(),
        spawns_page=None,
    )


def get_stage(
    conn: sqlite3.Connection,
    *,
    server: str,
    stage_code: str | None = None,
    game_id: str | None = None,
    include_map: bool = False,
    include_routes: bool = False,
    include_spawns: bool = False,
    page: int = 1,
    page_size: int = PAGE_SIZE_DEFAULT,
) -> StageDetailResult:
    """Fetch one stage's facts + optional map/routes/spawns (§T34; §V5/§V19/§V22).

    Read-only; parameterized SQL only (§V2). The default response is facts +
    provenance only (§V22 -- the heavy sections stay off); each opted-in section is
    returned as a bounded page (§V19), so even an included payload never yields an
    unbounded slice. ``page``/``page_size`` are validated against the §V19 window
    here too, mirroring the model gate. Both transports call this function (§V14).
    """
    p, size = _validate_page(page, page_size)
    repo = StageRepository(conn)
    stage = _resolve_stage(repo, server, stage_code=stage_code, game_id=game_id)
    if stage is None:
        return _not_found_detail(server)

    offset = (p - 1) * size
    stage_pk = stage.stage_pk

    stage_map: StageMapFacts | None = None
    tiles: tuple[TileFacts, ...] = ()
    tiles_page: SectionPage | None = None
    if include_map:
        raw_map = repo.stage_map(stage_pk)
        stage_map = StageMapFacts(
            width=raw_map.width if raw_map else None,
            height=raw_map.height if raw_map else None,
            map_version=raw_map.map_version if raw_map else None,
            environment=_json_load(raw_map.environment_json) if raw_map else None,
        )
        tiles = tuple(
            TileFacts(
                x=t.x,
                y=t.y,
                tile_key=t.tile_key,
                height_type=t.height_type,
                buildable_type=t.buildable_type,
                passable=t.passable,
            )
            for t in repo.tiles(stage_pk, size, offset)
        )
        tiles_page = _section_page(p, size, repo.tile_count(stage_pk))

    routes: tuple[RouteFacts, ...] = ()
    routes_page: SectionPage | None = None
    if include_routes:
        routes = tuple(
            RouteFacts(
                route_index=r.route_index,
                start_position=_json_load(r.start_position_json),
                end_position=_json_load(r.end_position_json),
                checkpoints=_json_load(r.checkpoints_json),
            )
            for r in repo.routes(stage_pk, size, offset)
        )
        routes_page = _section_page(p, size, repo.route_count(stage_pk))

    spawns: tuple[SpawnFacts, ...] = ()
    spawns_page: SectionPage | None = None
    if include_spawns:
        spawns = tuple(
            SpawnFacts(
                wave_index=s.wave_index,
                enemy_game_id=s.enemy_game_id,
                enemy_level_variant=s.enemy_level_variant,
                route_index=s.route_index,
                spawn_time=s.spawn_time,
                count=s.count,
                interval=s.interval,
                spawn_group=s.spawn_group,
                hidden=s.hidden,
            )
            for s in repo.spawns(stage_pk, size, offset)
        )
        spawns_page = _section_page(p, size, repo.spawn_count(stage_pk))

    return StageDetailResult(
        status="ok",
        server=stage.server,
        stage=_stage_facts(stage),
        stage_map=stage_map,
        tiles=tiles,
        tiles_page=tiles_page,
        routes=routes,
        routes_page=routes_page,
        spawns=spawns,
        spawns_page=spawns_page,
    )
