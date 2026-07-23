"""Distinct-geometry digest of a stage's enemy routes + checkpoints (§V74/§V49).

A stage stores many raw route records that share identical ``(start, end,
checkpoints)`` geometry (4-4: 26 records, ~4 distinct); emitting every record is a
raw dump that overstates the route count (§V49) and burns the §V22 budget. This
module collapses records to distinct geometry, normalises the stored checkpoint
shapes to the wire contract (snake_case keys §V71 (d); WAIT markers dropped by
their typed ``type`` field §V74 (b)/B74), and reports the read-cap truncation
say-so (§V74 (a)/B73). It is a self-contained, pure transform over the typed route
rows -- no SQL, no network, no imported prose reaches it (§V18) -- kept out of the
stage service module so that module stays within the §V38 size cap (parallel to
:mod:`~arknights_mcp.services.stage_tile_grid` and
:mod:`~arknights_mcp.services.stage_map_render`).

Both transports reach this through the shared stage service (§V14); it holds the
single home (§V37) for the wire route digest, whose spatial twin is the render's
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
    positionless checkpoint is skipped, not fabricated (§V26).

    A typed WAIT checkpoint (:func:`_is_wait_checkpoint`, §V74 (b)/B74) is dropped; a
    real ``MOVE`` at grid corner ``(0, 0)`` survives (the render draws what this returns
    and no longer position-cleans it)."""
    if not isinstance(decoded, list):
        return ()
    points: list[tuple[int, int]] = []
    for item in decoded:
        if _is_wait_checkpoint(item):
            continue  # §V74 (b)/B74: WAIT marker (typed), never a spatial path point
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


#: Typed checkpoint kind naming a non-spatial WAIT pause (§T20 ``{type, position}``).
_WAIT_CHECKPOINT_TYPE = "WAIT"

#: Placeholder position a WAIT carries upstream; corroboration ONLY (§V74 (b)/B74/§V26).
PLACEHOLDER_POINT = (0, 0)


def _is_wait_checkpoint(item: object) -> bool:
    """Is one stored checkpoint a non-spatial WAIT marker (§V74 (b)/B74)?

    WAIT is read from the typed ``type`` field (§T20), NOT a ``(0, 0)`` position
    coincidence: a real ``MOVE`` targeting grid corner ``(0, 0)`` is kept (the earlier
    position-only heuristic dropped it + collapsed routes differing only there, B74).
    Position ``(0, 0)`` corroborates ONLY when ``type`` is absent (malformed/legacy).
    Single §V37 home for :func:`_digest_checkpoints` + :func:`_checkpoint_points`."""
    if isinstance(item, dict):
        raw_type = item.get("type")
        if raw_type is not None:
            return str(raw_type).strip().upper() == _WAIT_CHECKPOINT_TYPE
    return _checkpoint_position(item) == PLACEHOLDER_POINT


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

    Returns ``(emit_objects, positions)``: typed WAIT checkpoints
    (:func:`_is_wait_checkpoint`, by ``type`` -- NOT a ``(0, 0)`` coincidence, B74) are
    dropped (§V74 (b)), surviving keys snake_cased (§V71 (d)); ``positions`` is the
    surviving ``(x, y)`` sequence for the distinct-geometry key -- a real ``MOVE`` at
    corner ``(0, 0)`` is retained so routes differing only there stay distinct (B74).
    A non-list fragment normalises to ``([], ())``."""
    if not isinstance(decoded, list):
        return [], ()
    emit: list[object] = []
    positions: list[tuple[int, int] | None] = []
    for item in decoded:
        if _is_wait_checkpoint(item):
            continue  # §V74 (b)/B74: non-spatial WAIT marker (typed), never geometry
        emit.append(_snake_case_keys(item))
        positions.append(_checkpoint_position(item))
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


def _route_truncated_limitation() -> str:
    """Say-so when the raw route read hit the ``MAX_MAP_ROUTES`` cap (§V74 (a)/B73).

    The route read is bounded by :data:`MAX_MAP_ROUTES` BEFORE the distinct-geometry
    dedup (:func:`_distinct_routes`), so a raw count equal to the cap means records past
    it were never read -- a distinct geometry whose records ALL fall past the cap would
    vanish and the paged total under-report. Emitting this keeps that undercount visible
    rather than a silent pre-dedup miss (§V26). Single §V37 home; no spec cite reaches
    the client string (§V71 (b))."""
    return (
        f"route records truncated at the {MAX_MAP_ROUTES}-record read cap; "
        "distinct-geometry count and paged total may under-report"
    )
