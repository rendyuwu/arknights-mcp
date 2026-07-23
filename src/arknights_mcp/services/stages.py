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
from collections.abc import Sequence
from dataclasses import dataclass, field
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
from arknights_mcp.db.repositories.stages import (
    StageMapRow,
    StageRepository,
    StageRouteRow,
    StageRow,
)
from arknights_mcp.models.common import PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX
from arknights_mcp.services.stage_map_render import (
    MAX_MAP_CELLS,
    MAX_MAP_ROUTES,
    PLACEHOLDER_POINT,
    MapCell,
    MapRoute,
    RenderedMap,
    render_stage_map,
)
from arknights_mcp.services.stage_tile_grid import (
    TileGridFacts,
    resolve_tile_grid,
)
from arknights_mcp.util.coerce import json_load
from arknights_mcp.util.text import camel_to_snake

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
class RouteFacts:
    """One DISTINCT enemy-route geometry + how many raw records share it (§V74 (a)).

    A stage stores many route records that share identical ``(start, end,
    checkpoints)`` geometry (4-4: 26 records, ~4 distinct); emitting every record
    is a raw dump that overstates the route count (§V49) and burns the §V22 budget.
    The digest collapses records with identical geometry to one, carrying the raw
    ``route_indices`` that share it (so a spawn's ``route_index`` still joins) and
    an ``occurrence_count``.

    ``checkpoints`` is always a list (§V51), with the non-spatial WAIT placeholder
    positions dropped (§V74 (b)) and each checkpoint object's keys normalized to
    snake_case (§V71 (d)); an empty set is ``[]`` on the wire, never the source's
    ``{}``.
    """

    route_indices: tuple[int, ...]
    occurrence_count: int
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
    populated only when their include flag is set; ``routes`` and ``spawns`` each
    pair their rows with a bounded :class:`SectionPage` (§V19/§V22). ``tile_grid``
    is the compact per-row grid encoding (§V74 (c)) -- a whole board fits one
    response, so it carries no page cursor; an over-budget board yields
    ``tile_grid=None`` plus a §V22 caption in ``limitations``. ``map_image`` is the
    render-own SVG of the stage grid (§T122), populated only when
    ``include_map_image`` is set and the board renders within the §V22 budget;
    ``limitations`` carries any §V22 caption (e.g. an over-budget map image was
    omitted). ``status == "not_found"`` implies every section is empty/``None``.
    """

    status: StageAnalysisStatus
    server: str
    stage: StageFacts | None
    stage_map: StageMapFacts | None
    tile_grid: TileGridFacts | None
    routes: tuple[RouteFacts, ...]
    routes_page: SectionPage | None
    spawns: tuple[SpawnFacts, ...]
    spawns_page: SectionPage | None
    map_image: RenderedMap | None = None
    limitations: tuple[str, ...] = ()


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
        tile_grid=None,
        routes=(),
        routes_page=None,
        spawns=(),
        spawns_page=None,
    )


def _point_xy(decoded: object | None) -> tuple[int, int] | None:
    """Normalise a stored ``{"col", "row"}`` position to an ``(x, y)`` grid point.

    The route position fragments are stored as ``{col, row}`` (§T20); the render
    keys on ``(x, y) == (col, row)``. Returns ``None`` for any other shape (a NULL
    column, an empty set serialized as ``{}``, or a non-integer coordinate) so a
    malformed position is skipped, not fabricated (§V26)."""
    if isinstance(decoded, dict):
        col = decoded.get("col")
        row = decoded.get("row")
        # bool is an int subclass; exclude it -- a coordinate is a plain int.
        if (
            isinstance(col, int)
            and not isinstance(col, bool)
            and isinstance(row, int)
            and not isinstance(row, bool)
        ):
            return (col, row)
    return None


def _checkpoint_points(decoded: object | None) -> tuple[tuple[int, int], ...]:
    """Normalise a stored ``checkpoints`` array to ordered ``(x, y)`` grid points.

    Unlike the flat ``startPosition``/``endPosition`` fragments, each stored
    checkpoint is a ``{type, position: {col, row}, ...}`` object (§T20), so the
    ``{col, row}`` coordinate is read from its nested ``position`` -- a checkpoint
    that is already a bare ``{col, row}`` is accepted as a fallback. A malformed or
    positionless checkpoint is skipped, not fabricated (§V26)."""
    if not isinstance(decoded, list):
        return ()
    points: list[tuple[int, int]] = []
    for item in decoded:
        point = _checkpoint_position(item)
        if point is not None:
            points.append(point)
    return tuple(points)


#: The distinct-route key: ``(start_xy, end_xy, checkpoint_positions)``. Positions
#: are ``_point_xy`` normalisations (``None`` for a malformed/absent coordinate);
#: WAIT placeholders are dropped before the checkpoint sequence is built (§V74).
_GeometryKey = tuple[
    tuple[int, int] | None,
    tuple[int, int] | None,
    tuple[tuple[int, int] | None, ...],
]


def _checkpoint_position(item: object) -> tuple[int, int] | None:
    """The ``(x, y)`` grid point of one stored checkpoint, or ``None`` if positionless.

    A checkpoint is a ``{type, position: {col, row}, ...}`` object (§T20); a bare
    ``{col, row}`` is accepted as a fallback (mirrors :func:`_checkpoint_points`)."""
    position = item["position"] if isinstance(item, dict) and "position" in item else item
    return _point_xy(position)


def _snake_case_keys(value: object) -> object:
    """Recursively normalize a decoded checkpoint's dict keys to snake_case (§V71 (d)).

    Upstream checkpoint objects leak camelCase keys (``reachOffset`` /
    ``randomizeReachOffset`` / ``reachDistance``); the wire contract is snake_case,
    normalized at the shaping layer via the shared :func:`~arknights_mcp.util.text
    .camel_to_snake` (§V37) -- the stored fragment keeps the source keys. Nested
    ``position`` / ``reachOffset`` sub-dicts have their keys normalized too; non-dict
    leaves pass through."""
    if isinstance(value, dict):
        return {camel_to_snake(str(k)): _snake_case_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_snake_case_keys(v) for v in value]
    return value


def _digest_checkpoints(
    decoded: object | None,
) -> tuple[list[object], tuple[tuple[int, int] | None, ...]]:
    """Clean + snake_case a route's checkpoints and return its position sequence (§V74).

    Returns ``(emit_objects, positions)``: WAIT placeholder checkpoints (position ==
    :data:`~arknights_mcp.services.stage_map_render.PLACEHOLDER_POINT`) are dropped
    from the emitted objects (§V74 (b)), each surviving object's keys are snake_cased
    (§V71 (d)), and ``positions`` is the surviving ``(x, y)`` sequence used as part of
    the distinct-geometry key. A non-list fragment normalises to ``([], ())``."""
    if not isinstance(decoded, list):
        return [], ()
    emit: list[object] = []
    positions: list[tuple[int, int] | None] = []
    for item in decoded:
        point = _checkpoint_position(item)
        if point == PLACEHOLDER_POINT:
            continue  # §V74 (b): non-spatial WAIT marker, never emitted as geometry
        emit.append(_snake_case_keys(item))
        positions.append(point)
    return emit, tuple(positions)


@dataclass
class _RouteGroup:
    """Accumulator for one distinct route geometry while digesting (§V74 (a))."""

    indices: list[int] = field(default_factory=list)
    start: object | None = None
    end: object | None = None
    checkpoints: list[object] = field(default_factory=list)


def _distinct_routes(records: Sequence[StageRouteRow]) -> list[RouteFacts]:
    """Collapse raw route records to DISTINCT geometry (§V74 (a); the wire twin of B65).

    Records sharing an identical ``(start, end, checkpoint-positions)`` geometry --
    WAIT placeholders already dropped (§V74 (b)) -- collapse to one
    :class:`RouteFacts` carrying every contributing ``route_index`` (so a spawn's
    ``route_index`` still joins) and an ``occurrence_count``. The first record's
    decoded start/end/checkpoint objects represent the group; insertion order (dict)
    keeps first-occurrence order so paging is deterministic (§C). Mirrors the render's
    :func:`~arknights_mcp.services.stage_map_render._distinct_route_geometries` (§V37:
    both digest by spatial geometry -- that one over points, this over the emitted
    checkpoint objects)."""
    groups: dict[_GeometryKey, _RouteGroup] = {}
    for record in records:
        start = json_load(record.start_position_json)
        end = json_load(record.end_position_json)
        emit_checkpoints, positions = _digest_checkpoints(json_load(record.checkpoints_json))
        key: _GeometryKey = (_point_xy(start), _point_xy(end), positions)
        group = groups.get(key)
        if group is None:
            groups[key] = _RouteGroup(
                indices=[record.route_index],
                start=start,
                end=end,
                checkpoints=emit_checkpoints,
            )
        else:
            group.indices.append(record.route_index)
    return [
        RouteFacts(
            route_indices=tuple(sorted(group.indices)),
            occurrence_count=len(group.indices),
            start_position=group.start,
            end_position=group.end,
            checkpoints=group.checkpoints,
        )
        for group in groups.values()
    ]


def _build_map_image(
    repo: StageRepository, stage_pk: int, raw_map: StageMapRow | None
) -> tuple[RenderedMap | None, str | None]:
    """Render the stage grid into a bounded SVG (§T122; §V16/§V22).

    Reads the full grid (each read bounded by the render cap so an oversized table
    is never loaded whole, §V22), adapts the repository rows into the render's
    plain value objects, and returns ``(image, limitation)``: an in-budget board
    yields the image; an over-budget one yields no image + a §V22 caption. The
    image is a DERIVED render from typed grid data -- no third-party art byte and
    no imported source string reaches it (§V16/§V18)."""
    cells = [
        MapCell(
            x=t.x,
            y=t.y,
            height_type=t.height_type,
            buildable_type=t.buildable_type,
            passable=t.passable,
        )
        for t in repo.all_tiles(stage_pk, MAX_MAP_CELLS + 1)
    ]
    routes = [
        MapRoute(
            start=_point_xy(json_load(r.start_position_json)),
            end=_point_xy(json_load(r.end_position_json)),
            checkpoints=_checkpoint_points(json_load(r.checkpoints_json)),
        )
        for r in repo.all_routes(stage_pk, MAX_MAP_ROUTES)
    ]
    result = render_stage_map(
        width=raw_map.width if raw_map else None,
        height=raw_map.height if raw_map else None,
        cells=cells,
        routes=routes,
    )
    return result.image, result.limitation


def get_stage(
    conn: sqlite3.Connection,
    *,
    server: str,
    stage_code: str | None = None,
    game_id: str | None = None,
    include_map: bool = False,
    include_routes: bool = False,
    include_spawns: bool = False,
    include_map_image: bool = False,
    routes_page: int = 1,
    routes_page_size: int = PAGE_SIZE_DEFAULT,
    spawns_page: int = 1,
    spawns_page_size: int = PAGE_SIZE_DEFAULT,
) -> StageDetailResult:
    """Fetch one stage's facts + optional map/routes/spawns (§T34; §V5/§V19/§V22).

    Read-only; parameterized SQL only (§V2). The default response is facts +
    provenance only (§V22 -- the heavy sections stay off). ``include_map`` returns
    the tile grid as one compact per-row block (§V74 (c), no page cursor -- a whole
    board fits one response); ``include_routes``/``include_spawns`` each page
    through their **own** cursor (``routes_page``/``spawns_page``), so a client can
    request several sections at once and still page a large one without shifting the
    others off, and no included payload ever yields an unbounded slice (§V19). A
    tile board larger than the §V22 cap yields no grid + a limitation.
    ``include_map_image`` (off by default, §V22) adds a render-own SVG of the stage
    grid (§T122) -- a DERIVED image drawn from the stored typed grid data, never
    third-party art (§V16) and never the §V63 URL reference; an over-budget board is
    omitted with a §V22 limitation. Every cursor is validated against the §V19
    window here too, mirroring the model gate. Both transports call this function
    (§V14).
    """
    rp, rsize = _validate_page(routes_page, routes_page_size)
    sp, ssize = _validate_page(spawns_page, spawns_page_size)
    repo = StageRepository(conn)
    stage = _resolve_stage(repo, server, stage_code=stage_code, game_id=game_id)
    if stage is None:
        return _not_found_detail(server)

    stage_pk = stage.stage_pk
    limitations: list[str] = []

    # The map header is read once and shared by the tile grid and the render-own
    # image (§V37), so a request for both does not query it twice.
    raw_map: StageMapRow | None = None
    if include_map or include_map_image:
        raw_map = repo.stage_map(stage_pk)

    stage_map: StageMapFacts | None = None
    tile_grid: TileGridFacts | None = None
    if include_map:
        # Read the full grid, bounded by the §V22 cap so an oversized table is never
        # loaded whole (the +1 detects the over-cap case). The compact per-row
        # encoding makes a whole board fit one response, so tiles are not paged
        # (§V74 (c)).
        tile_rows = repo.all_tiles(stage_pk, MAX_MAP_CELLS + 1)
        # Only surface a map section when there is one; an all-null header with no
        # tiles is indistinguishable from "map absent", so omit it instead (the
        # tool then emits no ``map`` key at all).
        if raw_map is not None or tile_rows:
            stage_map = StageMapFacts(
                width=raw_map.width if raw_map else None,
                height=raw_map.height if raw_map else None,
                map_version=raw_map.map_version if raw_map else None,
                environment=json_load(raw_map.environment_json) if raw_map else None,
            )
            # §V74 (c)/§V26: resolve the grid and, when a non-empty board is over the
            # count cap or refused (over-extent / too many types), the say-so limitation
            # (B70/B71) so a refused grid is never a silent None read as "no tiles".
            tile_grid, grid_limitation = resolve_tile_grid(
                tile_rows,
                raw_map.width if raw_map else None,
                raw_map.height if raw_map else None,
            )
            if grid_limitation is not None:
                limitations.append(grid_limitation)

    routes: tuple[RouteFacts, ...] = ()
    routes_page_info: SectionPage | None = None
    if include_routes:
        # §V74 (a)/B21: digest the FULL route set to distinct geometry BEFORE paging,
        # so page 1 is the first N distinct routes and the total is stable across
        # pages (a per-page dedup would split one geometry across a page boundary).
        # The read is bounded by MAX_MAP_ROUTES (§V22), the same cap the render uses.
        distinct = _distinct_routes(repo.all_routes(stage_pk, MAX_MAP_ROUTES))
        offset = (rp - 1) * rsize
        routes = tuple(distinct[offset : offset + rsize])
        routes_page_info = _section_page(rp, rsize, len(distinct))

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

    map_image: RenderedMap | None = None
    if include_map_image:
        map_image, image_limitation = _build_map_image(repo, stage_pk, raw_map)
        if image_limitation is not None:
            limitations.append(image_limitation)

    return StageDetailResult(
        status="ok",
        server=stage.server,
        stage=_stage_facts(stage),
        stage_map=stage_map,
        tile_grid=tile_grid,
        routes=routes,
        routes_page=routes_page_info,
        spawns=spawns,
        spawns_page=spawns_page_info,
        map_image=map_image,
        limitations=tuple(limitations),
    )
