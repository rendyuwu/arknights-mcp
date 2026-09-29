"""Distinct-geometry digest of a stage's enemy routes + checkpoints.

A stage stores many raw route records that share identical ``(start, end,
checkpoints)`` geometry (4-4: 26 records, ~4 distinct); emitting every record is a
raw dump that overstates the route count and burns the budget. This
module collapses records to distinct geometry, normalises the stored checkpoint
shapes to the wire contract (snake_case keys; non-spatial markers dropped
by their typed ``type`` field against the real token set;
zero/false-default optional fields suppressed), and reports the read-cap
truncation
say-so. It is a self-contained, pure transform over the typed route
rows -- no SQL, no network, no imported prose reaches it -- kept out of the
stage service module so that module stays within the size cap (parallel to
:mod:`~arknights_mcp.services.stage_tile_grid` and
:mod:`~arknights_mcp.services.stage_map_render`).

Both transports reach this through the shared stage service; it holds the
single home for the wire route digest, whose spatial twin is the render's
:func:`~arknights_mcp.services.stage_map_render._distinct_route_geometries`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from arknights_mcp.db.repositories.stages import StageRouteRow
from arknights_mcp.services.stage_map_render import MAX_MAP_ROUTES
from arknights_mcp.util.coerce import json_load
from arknights_mcp.util.text import camel_to_snake


@dataclass(frozen=True)
class RouteFacts:
    """One DISTINCT enemy-route geometry + how many raw records share it.

    A stage stores many route records that share identical ``(start, end,
    checkpoints)`` geometry (4-4: 26 records, ~4 distinct); emitting every record
    is a raw dump that overstates the route count and burns the budget.
    The digest collapses records with identical geometry to one, carrying the raw
    ``route_indices`` that share it (so a spawn's ``route_index`` still joins) and
    an ``occurrence_count``.

    ``checkpoints`` is always a list, with the typed non-spatial markers
    dropped and each checkpoint object's keys normalized to
    snake_case; an empty set is ``[]`` on the wire, never the source's
    ``{}``. Each checkpoint carries ``type`` + ``position`` always; its optional
    ``time`` / ``reach_distance`` / ``reach_offset`` / ``randomize_reach_offset``
    fields ride only when they deviate from their zero/false default.
    """

    route_indices: tuple[int, ...]
    occurrence_count: int
    start_position: object | None
    end_position: object | None
    checkpoints: list[object]


def _point_xy(decoded: object | None) -> tuple[int, int] | None:
    """Normalise a stored ``{"col", "row"}`` position to an ``(x, y)`` grid point.

    The route position fragments are stored as ``{col, row}``, already rebased
    into the canonical top-origin grid frame at import, so
    ``(x, y) == (col, row)`` indexes the same board as a tile's ``(x, y)`` and as a
    ``tile_grid`` row -- which is what lets the render draw tiles and route markers in
    ONE frame. Returns ``None`` for any other shape (a NULL
    column, an empty set serialized as ``{}``, or a non-integer coordinate) so a
    malformed position is skipped, not fabricated."""
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
    checkpoint is a ``{type, position: {col, row}, ...}`` object, so the
    ``{col, row}`` coordinate is read from its nested ``position`` -- a checkpoint
    that is already a bare ``{col, row}`` is accepted as a fallback. A malformed or
    positionless checkpoint is skipped, not fabricated.

    A typed non-spatial checkpoint (:func:`_is_non_spatial_checkpoint`)
    is dropped; a real ``MOVE`` at grid corner ``(0, 0)`` survives (the render draws
    what this returns and no longer position-cleans it)."""
    if not isinstance(decoded, list):
        return ()
    points: list[tuple[int, int]] = []
    for item in decoded:
        if _is_non_spatial_checkpoint(item):
            continue  # non-spatial marker (typed), never a path point
        point = _checkpoint_position(item)
        if point is not None:
            points.append(point)
    return tuple(points)


#: The distinct-route key: ``(start_xy, end_xy, checkpoint_positions)``. Positions
#: are ``_point_xy`` normalisations (``None`` for a malformed/absent coordinate);
#: non-spatial markers are dropped before the checkpoint sequence is built.
_GeometryKey = tuple[
    tuple[int, int] | None,
    tuple[int, int] | None,
    tuple[tuple[int, int] | None, ...],
]


def _checkpoint_position(item: object) -> tuple[int, int] | None:
    """The ``(x, y)`` grid point of one stored checkpoint, or ``None`` if positionless.

    A checkpoint is a ``{type, position: {col, row}, ...}`` object; a bare
    ``{col, row}`` is accepted as a fallback (mirrors :func:`_checkpoint_points`)."""
    position = item["position"] if isinstance(item, dict) and "position" in item else item
    return _point_xy(position)


#: Typed checkpoint kinds that mark a non-spatial pause/despawn, NOT a path point.
#: Enumerated from the real corpus -- the counted token census
#: lives in the corpus evidence; the earlier invented literal ``"WAIT"``
#: matched no real record, so the filter never fired.
NON_SPATIAL_CHECKPOINT_TYPES = frozenset(
    {
        "WAIT_FOR_SECONDS",
        "WAIT_CURRENT_FRAGMENT_TIME",
        "WAIT_CURRENT_WAVE_TIME",
        "WAIT_BOSSRUSH_WAVE",
        "DISAPPEAR",
    }
)

#: Typed checkpoint kinds carrying a real board position. Kept
#: separate from the non-spatial set so an UNRECOGNIZED token is detectable: it
#: belongs to neither set and rides the conservative (spatial) side with a say-so
#: (:func:`unknown_checkpoint_type_limitation`), never a silent bucket.
SPATIAL_CHECKPOINT_TYPES = frozenset({"MOVE", "APPEAR_AT_POS", "PATROL_MOVE", "MAP_OFFSET_MOVE"})


def _checkpoint_type(item: object) -> str | None:
    """One stored checkpoint's normalized ``type`` token, or ``None`` when absent."""
    if isinstance(item, dict):
        raw_type = item.get("type")
        if raw_type is not None:
            return str(raw_type).strip().upper()
    return None


def _is_non_spatial_checkpoint(item: object) -> bool:
    """Is one stored checkpoint a non-spatial pause/despawn marker?

    Keyed on the typed ``type`` field ONLY, against the real token set
    (:data:`NON_SPATIAL_CHECKPOINT_TYPES`) -- never a position
    coincidence: a real ``MOVE`` targeting grid corner ``(0, 0)`` is kept, and
    7-23% of real non-spatial markers carry real coordinates while some ``MOVE``
    records sit at the placeholder, so position corroboration is wrong in BOTH
    directions. A type-less or unrecognized-type checkpoint is treated as
    spatial -- the conservative side: the fact stays visible on the wire.
    Single home for :func:`_digest_checkpoints` + :func:`_checkpoint_points`."""
    token = _checkpoint_type(item)
    return token is not None and token in NON_SPATIAL_CHECKPOINT_TYPES


def unknown_checkpoint_type_limitation(records: Sequence[StageRouteRow]) -> str | None:
    """Say-so when a route carries a checkpoint ``type`` outside the known census.

    A token in neither :data:`NON_SPATIAL_CHECKPOINT_TYPES` nor
    :data:`SPATIAL_CHECKPOINT_TYPES` (a new upstream checkpoint kind) is kept on the
    conservative spatial side, and this limitation names it so the classification
    uncertainty is disclosed, never a silent bucket. Returns ``None``
    when every typed checkpoint matches the census. Single home for both the
    route digest and the map render read paths; no cite reaches the client
    string."""
    unknown: set[str] = set()
    for record in records:
        decoded = json_load(record.checkpoints_json)
        if not isinstance(decoded, list):
            continue
        for item in decoded:
            token = _checkpoint_type(item)
            if (
                token is not None
                and token not in NON_SPATIAL_CHECKPOINT_TYPES
                and token not in SPATIAL_CHECKPOINT_TYPES
            ):
                unknown.add(token)
    if not unknown:
        return None
    return (
        f"route checkpoint type(s) {', '.join(sorted(unknown))} not recognized; "
        "treated as spatial path points, so route geometry and distinct-route "
        "grouping may over-report"
    )


def _snake_case_keys(value: object) -> object:
    """Recursively normalize a decoded checkpoint's dict keys to snake_case.

    Upstream checkpoint objects leak camelCase keys (``reachOffset`` /
    ``randomizeReachOffset`` / ``reachDistance``); the wire contract is snake_case,
    normalized at the shaping layer via the shared :func:`~arknights_mcp.util.text
    .camel_to_snake` -- the stored fragment keeps the source keys. Nested
    ``position`` / ``reachOffset`` sub-dicts have their keys normalized too; non-dict
    leaves pass through."""
    if isinstance(value, dict):
        return {camel_to_snake(str(k)): _snake_case_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_snake_case_keys(v) for v in value]
    return value


#: Optional checkpoint keys emitted ONLY when they deviate from their zero/false
#: default (omit-discipline). A MOVE checkpoint carries these at their
#: default on nearly every record, so suppressing the defaults roughly halves route
#: bytes. Keys are snake_case -- suppression runs AFTER
#: :func:`_snake_case_keys`. ``type`` + ``position`` are always emitted.
_CHECKPOINT_OPTIONAL_KEYS = frozenset(
    {"time", "reach_distance", "reach_offset", "randomize_reach_offset"}
)


def _is_checkpoint_field_default(value: object) -> bool:
    """Is one optional checkpoint field at its zero/false default?

    ``time``/``reach_distance`` default to numeric ``0``; ``randomize_reach_offset``
    to ``False``; ``reach_offset`` to an all-zero ``{x, y}`` offset. A scalar ``0`` /
    ``0.0`` / ``False`` counts, as does a dict/list whose every leaf is itself a
    default (an empty dict/list too). ``bool`` is tested before ``int`` so a truthy
    flag does not read as a non-zero number. Any other value is a real deviation and
    is kept (an omitted key means "at default", never "unknown")."""
    if value is None:
        return True
    if isinstance(value, bool):
        return value is False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, dict):
        return all(_is_checkpoint_field_default(v) for v in value.values())
    if isinstance(value, list):
        return all(_is_checkpoint_field_default(v) for v in value)
    return False


def _suppress_default_checkpoint_fields(item: object) -> object:
    """Drop a checkpoint's optional fields sitting at their zero/false default.

    ``type`` + ``position`` always ride; the four optional fields
    (:data:`_CHECKPOINT_OPTIONAL_KEYS`) are emitted ONLY when they deviate from the
    default (omit-discipline) -- a suppressed key reads as "at default", not
    "unknown". Cuts ~50% of route bytes on the common all-default MOVE. A
    non-dict checkpoint passes through untouched; an unrecognised key is kept."""
    if not isinstance(item, dict):
        return item
    return {
        k: v
        for k, v in item.items()
        if k not in _CHECKPOINT_OPTIONAL_KEYS or not _is_checkpoint_field_default(v)
    }


def _digest_checkpoints(
    decoded: object | None,
) -> tuple[list[object], tuple[tuple[int, int] | None, ...]]:
    """Clean + snake_case a route's checkpoints and return its position sequence.

    Returns ``(emit_objects, positions)``: typed non-spatial checkpoints
    (:func:`_is_non_spatial_checkpoint`, by ``type`` -- NOT a position coincidence)
    are dropped, surviving keys snake_cased and their
    optional fields default-suppressed (:func:`_suppress_default_checkpoint_fields`);
    ``positions`` is the surviving ``(x, y)`` sequence for the
    distinct-geometry key -- a real ``MOVE`` at corner ``(0, 0)`` is retained so routes
    differing only there stay distinct. A non-list fragment normalises to
    ``([], ())``."""
    if not isinstance(decoded, list):
        return [], ()
    emit: list[object] = []
    positions: list[tuple[int, int] | None] = []
    for item in decoded:
        if _is_non_spatial_checkpoint(item):
            continue  # non-spatial marker (typed), never geometry
        emit.append(_suppress_default_checkpoint_fields(_snake_case_keys(item)))
        positions.append(_checkpoint_position(item))
    return emit, tuple(positions)


@dataclass
class _RouteGroup:
    """Accumulator for one distinct route geometry while digesting."""

    indices: list[int] = field(default_factory=list)
    start: object | None = None
    end: object | None = None
    checkpoints: list[object] = field(default_factory=list)


def _distinct_routes(records: Sequence[StageRouteRow]) -> list[RouteFacts]:
    """Collapse raw route records to DISTINCT geometry.

    Records sharing an identical ``(start, end, checkpoint-positions)`` geometry --
    non-spatial markers already dropped -- collapse to one
    :class:`RouteFacts` carrying every contributing ``route_index`` (so a spawn's
    ``route_index`` still joins) and an ``occurrence_count``. The first record's
    decoded start/end/checkpoint objects represent the group; insertion order (dict)
    keeps first-occurrence order so paging is deterministic. Mirrors the render's
    :func:`~arknights_mcp.services.stage_map_render._distinct_route_geometries` (both
    digest by spatial geometry -- that one over points, this over the emitted
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


def _route_truncated_limitation() -> str:
    """Say-so when the raw route read hit the ``MAX_MAP_ROUTES`` cap.

    The route read is bounded by :data:`MAX_MAP_ROUTES` BEFORE the distinct-geometry
    dedup (:func:`_distinct_routes`), so a raw count equal to the cap means records past
    it were never read -- a distinct geometry whose records ALL fall past the cap would
    vanish and the paged total under-report. Emitting this keeps that undercount visible
    rather than a silent pre-dedup miss. Single home; no cite reaches
    the client string."""
    return (
        f"route records truncated at the {MAX_MAP_ROUTES}-record read cap; "
        "distinct-geometry count and paged total may under-report"
    )
