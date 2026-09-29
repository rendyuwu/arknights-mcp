"""Checkpoint-classifier contract tests over the REAL corpus.

The route digest partitions stored checkpoints by their source ``type`` enum into
non-spatial markers (dropped from geometry) and spatial path points. The original
defect: the previous classifier keyed on an INVENTED literal ``"WAIT"`` that matched 0 of
~128k real non-spatial checkpoints, so the filter never fired while every
synthetic-fixture test passed -- the fixture asserted against its own invention.

These tests pin the classifier against ``route_checkpoints_real.json`` -- real
``stage_routes`` rows extracted verbatim from a promoted build (en 7-2 =
``main_07-01`` + 7-3 = ``main_07-02``; real-shape discipline extended to field
VALUE domains). They assert:

* the partition is NON-DEGENERATE: >= 1 real record classified on EACH side;
* every real token is classified -- and the ``DISAPPEAR`` (non-spatial) vs
  ``APPEAR_AT_POS`` (spatial) substring trap lands on the right sides;
* the real 7-2 route set digests 28 records -> 6 distinct groups: the pair
  differing ONLY by a WAIT_FOR_SECONDS merges, pairs differing by a real MOVE stay
  distinct;
* a type-less checkpoint is spatial -- the ``(0, 0)`` placeholder fallback is dead
  (the placeholder was rebased away from ``(0, 0)``);
* an UNKNOWN token rides the conservative spatial side WITH a say-so limitation,
  never a silent bucket.
"""

from __future__ import annotations

import json
from pathlib import Path

from arknights_mcp.db.repositories.stages import StageRouteRow
from arknights_mcp.services.stage_route_digest import (
    NON_SPATIAL_CHECKPOINT_TYPES,
    SPATIAL_CHECKPOINT_TYPES,
    _digest_checkpoints,
    _distinct_routes,
    _is_non_spatial_checkpoint,
    unknown_checkpoint_type_limitation,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "route_checkpoints_real.json"


def _real_rows(game_id: str) -> list[StageRouteRow]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [
        StageRouteRow(
            route_index=r["route_index"],
            start_position_json=r["start_position_json"],
            end_position_json=r["end_position_json"],
            checkpoints_json=r["checkpoints_json"],
        )
        for r in raw[game_id]
    ]


def _real_checkpoints() -> list[dict[str, object]]:
    """Every stored checkpoint object across both real stages' route rows."""
    items: list[dict[str, object]] = []
    for game_id in ("main_07-01", "main_07-02"):
        for row in _real_rows(game_id):
            # A NULL column or the source's empty-set `{}` carries no checkpoints.
            decoded = json.loads(row.checkpoints_json) if row.checkpoints_json else None
            if isinstance(decoded, list):
                items.extend(cp for cp in decoded if isinstance(cp, dict))
    return items


def test_real_corpus_partition_is_non_degenerate() -> None:
    # The classifier must match >= 1 REAL record on EACH side of the
    # partition -- a side matching 0 records is a silent no-op shipped as a filter
    # (an earlier defect: `type == "WAIT"` matched nothing while the suite stayed green).
    checkpoints = _real_checkpoints()
    non_spatial = [cp for cp in checkpoints if _is_non_spatial_checkpoint(cp)]
    spatial = [cp for cp in checkpoints if not _is_non_spatial_checkpoint(cp)]
    assert len(non_spatial) >= 1
    assert len(spatial) >= 1


def test_real_corpus_tokens_all_classified() -> None:
    # Every `type` token in the real corpus belongs to exactly one of
    # the two enumerated sets -- an unclassified real token would silently ride the
    # conservative side without this test ever noticing the census went stale.
    tokens = {str(cp["type"]) for cp in _real_checkpoints() if "type" in cp}
    assert tokens  # the fixture carries typed checkpoints at all
    assert tokens <= (NON_SPATIAL_CHECKPOINT_TYPES | SPATIAL_CHECKPOINT_TYPES)
    # The substring trap: DISAPPEAR (non-spatial despawn) vs APPEAR_AT_POS (real
    # position) -- both present in the corpus, and a sloppy prefix/substring match
    # would put them on the same side.
    assert "DISAPPEAR" in tokens and "APPEAR_AT_POS" in tokens
    assert _is_non_spatial_checkpoint({"type": "DISAPPEAR", "position": {"col": 1, "row": 1}})
    assert not _is_non_spatial_checkpoint(
        {"type": "APPEAR_AT_POS", "position": {"col": 1, "row": 1}}
    )


def test_real_7_2_digests_to_six_distinct_route_groups() -> None:
    # Real 7-2 (`main_07-01`, 28 raw records) held 7 groups
    # under the dead classifier because a WAIT_FOR_SECONDS marker split identical
    # geometry; with markers dropped it digests to 6 distinct groups.
    distinct = _distinct_routes(_real_rows("main_07-01"))
    assert len(distinct) == 6
    # The merged pair: record 2 differs from 7/8/15/19/24/25 ONLY by a
    # WAIT_FOR_SECONDS checkpoint -> one group, occurrence_count 7.
    merged = next(g for g in distinct if 2 in g.route_indices)
    assert merged.route_indices == (2, 7, 8, 15, 19, 24, 25)
    assert merged.occurrence_count == 7
    # Pairs differing by a REAL MOVE stay distinct (the eyeballed "-> 4"
    # was wrong precisely because these do NOT merge).
    index_sets = {g.route_indices for g in distinct}
    assert (11,) in index_sets and (13, 20, 26) in index_sets
    assert (12,) in index_sets and (14, 21, 27) in index_sets


def test_real_7_2_emits_no_non_spatial_marker_as_geometry() -> None:
    # No emitted checkpoint carries a non-spatial token -- the marker is
    # dropped from BOTH the emit objects and the distinct-geometry key.
    for group in _distinct_routes(_real_rows("main_07-01")):
        for checkpoint in group.checkpoints:
            assert isinstance(checkpoint, dict)
            assert checkpoint["type"] not in NON_SPATIAL_CHECKPOINT_TYPES


def test_typeless_checkpoint_is_spatial_placeholder_fallback_dead() -> None:
    # The placeholder is rebased upstream to (0, height-1), so a (0, 0)
    # position proves nothing -- the old type-absent placeholder fallback is dead.
    # A type-less checkpoint is kept as a spatial path point (conservative side).
    decoded = [
        {"position": {"col": 0, "row": 0}},  # type-less corner -> KEPT (was dropped)
        {"position": {"col": 2, "row": 3}},  # type-less real point -> kept
    ]
    _, positions = _digest_checkpoints(decoded)
    assert positions == ((0, 0), (2, 3))


def _row(index: int, checkpoints: list[dict[str, object]]) -> StageRouteRow:
    return StageRouteRow(
        route_index=index,
        start_position_json=json.dumps({"col": 0, "row": 0}),
        end_position_json=json.dumps({"col": 5, "row": 2}),
        checkpoints_json=json.dumps(checkpoints),
    )


def test_unknown_token_rides_spatial_side_with_a_limitation() -> None:
    # A token in NEITHER set (a future upstream checkpoint kind) stays
    # on the conservative spatial side -- the fact remains visible on the wire --
    # and the limitation names it; it is never a silent bucket.
    unknown = {"type": "TELEPORT", "position": {"col": 4, "row": 1}}
    emit, positions = _digest_checkpoints([unknown])
    assert positions == ((4, 1),)  # kept as geometry
    assert len(emit) == 1
    limitation = unknown_checkpoint_type_limitation([_row(0, [unknown])])
    assert limitation is not None
    assert "TELEPORT" in limitation


def test_known_tokens_raise_no_unknown_limitation() -> None:
    # The say-so fires ONLY for tokens outside the census: the full real corpus
    # (both stages) plus a type-less checkpoint yields None.
    rows = _real_rows("main_07-01") + _real_rows("main_07-02")
    rows.append(_row(99, [{"position": {"col": 1, "row": 1}}]))  # type-less: no token
    assert unknown_checkpoint_type_limitation(rows) is None
