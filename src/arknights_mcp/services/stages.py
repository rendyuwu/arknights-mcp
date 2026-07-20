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

import sqlite3
from dataclasses import dataclass
from typing import Literal

from arknights_mcp.analyzers import (
    EnemyOccurrence,
    Observation,
    StageThreatContext,
    StageTiles,
)
from arknights_mcp.analyzers import (
    analyze_stage as run_threat_analysis,
)
from arknights_mcp.db.repositories.stages import StageRepository, StageRow
from arknights_mcp.models.common import PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX
from arknights_mcp.util.coerce import json_load

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
    """One enemy's typed appearance in the stage (from ``stage_enemies``).

    Carries the §V47 per-enemy stat block (``hp`` / ``atk`` / ``def_`` / ``res`` /
    ``attack_interval`` / ``move_speed`` / ``weight``) the ``analyze_stage``
    ``depth=detailed`` occurrence promises; each is ``None`` when the level row or
    the source field is absent (§V26).

    ``variant_id`` is set for a stage-scoped inline variant (§T80/§V43), whose
    ``motion_type`` and stat block already read the variant's value over the base
    prefab (COALESCE in the repository; §V46); ``None`` for a plain base-enemy
    occurrence.
    """

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
    hp: int | None
    atk: int | None
    def_: int | None
    res: int | None
    attack_interval: float | None
    move_speed: float | None
    weight: int | None
    variant_id: str | None


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
    distinction the analyzer relies on: SQL ``NULL`` (or an undecodable fragment)
    -> ``None`` (field absent), ``"[]"`` -> ``()`` (present but empty). Decodes
    through the shared §V37 :func:`~arknights_mcp.util.coerce.json_load` home; only
    the list/str shaping the analyzer needs is applied on top."""
    data = json_load(raw)
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
                hp=enemy.hp,
                atk=enemy.atk,
                def_=enemy.def_,
                res=enemy.res,
                attack_interval=enemy.attack_interval,
                move_speed=enemy.move_speed,
                weight=enemy.weight,
                variant_id=enemy.variant_id,
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
                defense=enemy.def_,
                res=enemy.res,
                attack_range=enemy.attack_range,
                block_behavior=enemy.block_behavior,
                first_spawn_time=enemy.first_spawn_time,
                last_spawn_time=enemy.last_spawn_time,
                route_count=enemy.route_count,
            )
        )

    # Stage-level rule inputs (§T39): distinct routes + the deploy-tile summary. A
    # tile-less stage passes ``tiles=None`` so the tiles/deploy rule skips it (§V26).
    total_tiles, buildable_melee, buildable_ranged = repo.tile_summary(stage.stage_pk)
    tiles = (
        StageTiles(
            total=total_tiles,
            buildable_melee=buildable_melee,
            buildable_ranged=buildable_ranged,
        )
        if total_tiles > 0
        else None
    )

    analysis = run_threat_analysis(
        StageThreatContext(
            server=stage.server,
            stage_code=stage.stage_code,
            occurrences=tuple(threat_inputs),
            route_count=repo.route_count(stage.stage_pk),
            tiles=tiles,
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


def _as_checkpoint_list(decoded: object | None) -> list[object]:
    """Normalize a decoded route ``checkpoints`` fragment to an array (§V51).

    Checkpoints are semantically an ordered list, but the source serializes an
    empty set as ``{}`` and a ``NULL`` column decodes to ``None`` -- so the raw
    wire type varied row-to-row (populated ``[{...}]`` vs empty ``{}``), breaking
    a client that indexes the field as an array unconditionally (B44). A populated
    list passes through unchanged; any non-list (``None``, ``{}``, a stray dict)
    normalizes to ``[]`` so the field is *always* a JSON array on the wire.
    """
    return decoded if isinstance(decoded, list) else []


@dataclass(frozen=True)
class RouteFacts:
    """One enemy route; positions decoded from the stored (sanitized) JSON.

    ``checkpoints`` is always a list (§V51): the shaper normalizes the decoded
    fragment via :func:`_as_checkpoint_list`, so an empty set is ``[]`` on the
    wire, never the source's ``{}`` -- positions stay single coordinates.
    """

    route_index: int
    start_position: object | None
    end_position: object | None
    checkpoints: list[object]


@dataclass(frozen=True)
class SpawnFacts:
    """One scheduled spawn on the stage timeline (typed structural fields only).

    ``variant_id`` is the inline-variant id for a ``useDb:false`` spawn (§T80);
    ``enemy_game_id`` stays the base prefab, so a client can see both.
    """

    wave_index: int
    enemy_game_id: str
    enemy_level_variant: int | None
    route_index: int | None
    spawn_time: float | None
    count: int | None
    interval: float | None
    spawn_group: str | None
    hidden: bool
    variant_id: str | None


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
    map_page: int = 1,
    map_page_size: int = PAGE_SIZE_DEFAULT,
    routes_page: int = 1,
    routes_page_size: int = PAGE_SIZE_DEFAULT,
    spawns_page: int = 1,
    spawns_page_size: int = PAGE_SIZE_DEFAULT,
) -> StageDetailResult:
    """Fetch one stage's facts + optional map/routes/spawns (§T34; §V5/§V19/§V22).

    Read-only; parameterized SQL only (§V2). The default response is facts +
    provenance only (§V22 -- the heavy sections stay off). Each opted-in section
    pages through its **own** cursor (``map_page``/``routes_page``/``spawns_page``),
    so a client can request several sections at once and still page a large one
    without shifting the others off, and no included payload ever yields an
    unbounded slice (§V19). Every cursor is validated against the §V19 window here
    too, mirroring the model gate. Both transports call this function (§V14).
    """
    mp, msize = _validate_page(map_page, map_page_size)
    rp, rsize = _validate_page(routes_page, routes_page_size)
    sp, ssize = _validate_page(spawns_page, spawns_page_size)
    repo = StageRepository(conn)
    stage = _resolve_stage(repo, server, stage_code=stage_code, game_id=game_id)
    if stage is None:
        return _not_found_detail(server)

    stage_pk = stage.stage_pk

    stage_map: StageMapFacts | None = None
    tiles: tuple[TileFacts, ...] = ()
    tiles_page_info: SectionPage | None = None
    if include_map:
        raw_map = repo.stage_map(stage_pk)
        total_tiles = repo.tile_count(stage_pk)
        # Only surface a map section when there is one; an all-null header with no
        # tiles is indistinguishable from "map absent", so omit it instead (the
        # tool then emits no ``map`` key at all).
        if raw_map is not None or total_tiles > 0:
            offset = (mp - 1) * msize
            stage_map = StageMapFacts(
                width=raw_map.width if raw_map else None,
                height=raw_map.height if raw_map else None,
                map_version=raw_map.map_version if raw_map else None,
                environment=json_load(raw_map.environment_json) if raw_map else None,
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
                for t in repo.tiles(stage_pk, msize, offset)
            )
            tiles_page_info = _section_page(mp, msize, total_tiles)

    routes: tuple[RouteFacts, ...] = ()
    routes_page_info: SectionPage | None = None
    if include_routes:
        offset = (rp - 1) * rsize
        routes = tuple(
            RouteFacts(
                route_index=r.route_index,
                start_position=json_load(r.start_position_json),
                end_position=json_load(r.end_position_json),
                checkpoints=_as_checkpoint_list(json_load(r.checkpoints_json)),
            )
            for r in repo.routes(stage_pk, rsize, offset)
        )
        routes_page_info = _section_page(rp, rsize, repo.route_count(stage_pk))

    spawns: tuple[SpawnFacts, ...] = ()
    spawns_page_info: SectionPage | None = None
    if include_spawns:
        offset = (sp - 1) * ssize
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
                variant_id=s.variant_id,
            )
            for s in repo.spawns(stage_pk, ssize, offset)
        )
        spawns_page_info = _section_page(sp, ssize, repo.spawn_count(stage_pk))

    return StageDetailResult(
        status="ok",
        server=stage.server,
        stage=_stage_facts(stage),
        stage_map=stage_map,
        tiles=tiles,
        tiles_page=tiles_page_info,
        routes=routes,
        routes_page=routes_page_info,
        spawns=spawns,
        spawns_page=spawns_page_info,
    )
