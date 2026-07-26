"""§T122 render-own stage-map image tests (§V16/§V22/§C).

Two layers:

* the pure renderer (:mod:`arknights_mcp.services.stage_map_render`) driven with
  plain grid values -- no DB -- asserting the derived-work / no-art-bytes contract
  (§V16), the bounded opt-in budget (§V22), and deterministic pure-Python SVG
  output (§C);
* the ``get_stage`` tool wired end to end against the pinned 4-4 fixture, proving
  the image rides an opt-in field, is absent by default, and renders a MAIN-story
  stage (which the §V63 URL-ref path cannot link) as our own derived image.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.envelopes import MAX_RESPONSE_BYTES, serialized_size
from arknights_mcp.mcp.tools.stage import build_get_stage_spec
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.stages import GetStageInput
from arknights_mcp.services.stage_map_render import (
    MAX_MAP_CELLS,
    SVG_MEDIA_TYPE,
    MapCell,
    MapRoute,
    render_stage_map,
)
from arknights_mcp.services.stage_route_digest import _checkpoint_points, _point_xy
from arknights_mcp.services.stages import get_stage
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

# Markers that would signal a third-party-art payload or the §V63 URL-ref path --
# a derived render must contain NONE of them (§V16).
_ART_MARKERS = ("<image", "xlink", "href", "githubusercontent", "yuanyan3060", ".png")


# --- pure renderer: §V16 derived work, no third-party art bytes ---------------


def test_render_is_a_self_contained_derived_svg() -> None:
    res = render_stage_map(
        width=3,
        height=3,
        cells=[
            MapCell(0, 0, "LOWLAND", "NONE", True),
            MapCell(2, 2, "HIGHLAND", "STRENGTH", True),
        ],
        routes=[MapRoute(start=(0, 0), end=(2, 2))],
    )
    assert res.image is not None
    svg = res.image.svg
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    assert res.image.media_type == SVG_MEDIA_TYPE
    # §V16 / §V63: no embedded art byte and no link to the third-party mirror.
    low = svg.lower()
    for marker in _ART_MARKERS:
        assert marker not in low, f"derived SVG must not contain {marker!r}"


def test_render_never_emits_an_imported_source_string() -> None:
    # §V16/§V18: tile fills are chosen from the TYPED fields via a fixed colour map;
    # an untrusted source string is never interpolated into the document.
    inject = '"><script>alert(1)</script>'
    res = render_stage_map(
        width=2,
        height=2,
        cells=[
            MapCell(0, 0, inject, inject, True),
            MapCell(1, 1, "HIGHLAND", "BOTH", None),
        ],
    )
    assert res.image is not None
    assert inject not in res.image.svg
    assert "<script>" not in res.image.svg
    assert "alert(1)" not in res.image.svg


def test_render_is_deterministic() -> None:
    # §C: pure-Python, fixed order + fixed literals -> byte-identical every time.
    cells = [MapCell(0, 0, "LOWLAND", "MELEE", True), MapCell(3, 2, "HIGHLAND", "RANGED", True)]
    routes = [MapRoute(start=(0, 0), end=(3, 2), checkpoints=((1, 1), (2, 2)))]
    first = render_stage_map(width=4, height=3, cells=cells, routes=routes)
    second = render_stage_map(width=4, height=3, cells=cells, routes=routes)
    assert first.image is not None and second.image is not None
    assert first.image.svg == second.image.svg


def test_render_encodes_every_stored_tile_and_route_marker() -> None:
    # Acceptance: the derived image draws the board + one rect per stored tile +
    # route start/end markers from the stage's own grid data.
    res = render_stage_map(
        width=3,
        height=3,
        cells=[MapCell(0, 0, "LOWLAND", "NONE", True), MapCell(2, 2, "LOWLAND", "NONE", True)],
        routes=[MapRoute(start=(0, 0), end=(2, 2))],
    )
    assert res.image is not None
    svg = res.image.svg
    assert svg.count("<rect class='f'") == 2  # two stored non-buildable tiles
    assert res.image.tile_count == 2
    assert "class='b'" in svg  # board backdrop
    assert "class='g'" in svg  # grid path
    assert "class='rs'" in svg  # route start
    assert "class='re'" in svg  # route end


def test_render_draws_checkpoint_polyline() -> None:
    res = render_stage_map(
        width=3,
        height=1,
        cells=[],
        routes=[MapRoute(start=(0, 0), end=(2, 0), checkpoints=((1, 0), (2, 0)))],
    )
    assert res.image is not None
    assert "class='rp'" in res.image.svg


# --- B74/B65: render draws real waypoints; WAIT filtered upstream by type ------


def test_render_draws_every_checkpoint_including_grid_corner() -> None:
    # §V74(b)/B74: WAIT filtering now happens UPSTREAM by the typed `type` field
    # (services.stages._is_wait_checkpoint), so the pure renderer draws every
    # checkpoint it is given -- including a real MOVE targeting grid corner (0, 0). The
    # earlier render-layer (0, 0)-dropping discarded such a genuine waypoint (B74).
    res = render_stage_map(
        width=8,
        height=9,
        cells=[],
        routes=[MapRoute(start=(1, 1), end=(7, 2), checkpoints=((1, 1), (0, 0), (7, 2)))],
    )
    assert res.image is not None
    svg = res.image.svg
    # (0, 0) centre on a height-9 board is "20,212" -- it IS drawn as a real path point,
    # in order, between the two other waypoints (no corner zig-zag: type-filtered upstream).
    assert "points='44,188 20,212 188,164'" in svg


def test_render_dedups_identical_route_geometry() -> None:
    # B65/§V74(a): a stage stores many route records sharing identical geometry (4-4:
    # 26 records, ~4 distinct). Drawing every record over-plots the overlay with
    # coincident markers (52 circles for ~4 routes). Identical geometry collapses to one.
    identical = [MapRoute(start=(0, 0), end=(2, 2)) for _ in range(5)]
    other = MapRoute(start=(0, 2), end=(2, 0))
    res = render_stage_map(width=3, height=3, cells=[], routes=[*identical, other])
    assert res.image is not None
    svg = res.image.svg
    # 5 identical + 1 distinct -> 2 start markers + 2 end markers, not 6 each.
    assert svg.count("class='rs'") == 2
    assert svg.count("class='re'") == 2


def test_render_keeps_routes_distinct_when_a_real_corner_move_differs() -> None:
    # §V74(a)+B74: the renderer dedups on exact geometry ONLY; WAIT markers are
    # filtered upstream by type, not here. Two routes differing by a REAL corner (0, 0)
    # MOVE are distinct paths and must NOT collapse to one (the earlier render-layer
    # (0, 0)-cleaning would have merged them).
    a = MapRoute(start=(0, 0), end=(2, 0), checkpoints=((1, 0), (2, 0)))
    b = MapRoute(start=(0, 0), end=(2, 0), checkpoints=((1, 0), (0, 0), (2, 0)))
    res = render_stage_map(width=3, height=1, cells=[], routes=[a, b])
    assert res.image is not None
    assert res.image.svg.count("class='rp'") == 2


# --- B65/B86: colour legend carried alongside the image ------------------------


def _legend_map(res) -> dict[str, str]:  # type: ignore[no-untyped-def]
    assert res.image is not None
    return {entry["color"]: entry["meaning"] for entry in res.image.legend}


def test_render_carries_colour_legend() -> None:
    # B65: the render shipped no colour legend, so a client could not decode the opaque
    # hex fills. Every rendered image carries a legend whose meanings are plain
    # client-facing text -- no spec cites / internal jargon (§V71).
    res = render_stage_map(
        width=2,
        height=2,
        cells=[MapCell(0, 0, "LOWLAND", "NONE", True, tile_key="tile_road")],
        routes=[MapRoute(start=(0, 0), end=(1, 1))],
    )
    assert res.image is not None
    legend = _legend_map(res)
    assert legend  # non-empty
    # every fill/marker colour the SVG actually emits is decodable via the legend, and
    # in the fixed canonical order (§C determinism): road tile fill, then start, then end.
    order = [e["color"] for e in res.image.legend]
    assert order == ["#cfd8dc", "#2e7d32", "#c62828"]
    for meaning in legend.values():
        assert "§" not in meaning


def test_legend_lists_only_present_colours() -> None:
    # §V82/B86: the legend lists ONLY the colours THIS render draws -- an "impassable"
    # swatch (#546e7a) is never listed for a board with no wall tiles, and no route
    # marker colour appears when there are no routes.
    res = render_stage_map(
        width=1, height=1, cells=[MapCell(0, 0, "LOWLAND", "MELEE", True)], routes=[]
    )
    legend = _legend_map(res)
    assert set(legend) == {"#90caf9"}  # a lone buildable-ground tile, nothing else
    assert "#546e7a" not in legend  # impassable not painted -> not listed
    assert "#2e7d32" not in legend and "#c62828" not in legend  # no routes -> no markers


def test_road_tile_is_enemy_path_not_buildable_ground() -> None:
    # §V82/B86: a road tile (the enemy path) that is ALSO melee-buildable must not read
    # as plain "buildable ground" -- it is coloured/labelled as the enemy path.
    res = render_stage_map(
        width=1, height=1, cells=[MapCell(0, 0, "LOWLAND", "MELEE", True, tile_key="tile_road")]
    )
    legend = _legend_map(res)
    assert set(legend) == {"#cfd8dc"}
    assert "#90caf9" not in legend  # NOT the melee "buildable ground" colour
    assert "buildable ground" not in legend["#cfd8dc"]
    assert "enemy path" in legend["#cfd8dc"]


def test_road_legend_carries_both_enemy_path_and_melee_deploy() -> None:
    # §V86 (B94): the road gloss must carry BOTH semantics -- enemy path AND
    # melee-deploy (the compact tile-grid legend shows road tiles buildable MELEE;
    # dropping either half misleads, §V82). T166 fixed the "buildable ground"
    # direction; this pins the opposite one.
    res = render_stage_map(
        width=1, height=1, cells=[MapCell(0, 0, "LOWLAND", "MELEE", True, tile_key="tile_road")]
    )
    meaning = _legend_map(res)["#cfd8dc"]
    assert "enemy path" in meaning
    assert "melee" in meaning


def test_forbidden_tile_is_not_a_walkable_path() -> None:
    # §V82/B86: a passable, non-buildable, non-path tile (tile_forbidden) is a
    # non-deployable tile, NOT glossed a "walkable path".
    res = render_stage_map(
        width=1, height=1, cells=[MapCell(0, 0, "LOWLAND", "NONE", True, tile_key="tile_forbidden")]
    )
    legend = _legend_map(res)
    assert set(legend) == {"#eceff1"}
    assert "walkable path" not in legend["#eceff1"]
    assert "cannot deploy" in legend["#eceff1"] or "non-deployable" in legend["#eceff1"]


def test_highland_ranged_platform_is_a_deploy_tile_not_a_wall() -> None:
    # §V82: a buildable highland platform is a ranged deploy surface even when it is not
    # ground-passable -- buildable is classified before passable, so it is not a wall.
    res = render_stage_map(
        width=1, height=1, cells=[MapCell(0, 0, "HIGHLAND", "RANGED", False, tile_key="tile_wall")]
    )
    legend = _legend_map(res)
    assert set(legend) == {"#a5d6a7"}
    assert "#546e7a" not in legend


def test_degenerate_route_is_not_rendered() -> None:
    # §V82/B86: a start==end, 0-checkpoint route stacks the start+end circles on one
    # cell (an artifact) -- it is dropped, drawing no markers and listing no marker colour.
    res = render_stage_map(
        width=3,
        height=3,
        cells=[MapCell(0, 0, "LOWLAND", "NONE", True, tile_key="tile_road")],
        routes=[MapRoute(start=(0, 0), end=(0, 0))],
    )
    assert res.image is not None
    svg = res.image.svg
    assert "class='rs'" not in svg
    assert "class='re'" not in svg
    legend = _legend_map(res)
    assert "#2e7d32" not in legend and "#c62828" not in legend


def test_start_equals_end_with_checkpoints_is_a_real_loop_kept() -> None:
    # §V82/B86: only the 0-checkpoint start==end route is degenerate. A start==end route
    # that still carries checkpoints is a real loop and IS drawn.
    res = render_stage_map(
        width=3,
        height=3,
        cells=[],
        routes=[MapRoute(start=(0, 0), end=(0, 0), checkpoints=((1, 1), (2, 2)))],
    )
    assert res.image is not None
    assert "class='rs'" in res.image.svg
    assert "class='rp'" in res.image.svg


# --- B94/§V86: byte economy -- shared style, one grid path, marker dedup -------


def test_all_fills_and_strokes_live_in_one_shared_style_block() -> None:
    # §V86 (B94): every colour is written ONCE in a shared <style> block; drawn
    # elements carry class + geometry only -- no per-element fill=/stroke= attribute
    # repeats (77 tile rects each restating a fill was ~half the document).
    res = render_stage_map(
        width=3,
        height=2,
        cells=[
            MapCell(0, 0, "LOWLAND", "MELEE", True),
            MapCell(1, 0, "LOWLAND", "MELEE", True, tile_key="tile_road"),
            MapCell(2, 1, "HIGHLAND", "RANGED", True),
        ],
        routes=[MapRoute(start=(0, 0), end=(2, 1), checkpoints=((1, 0), (2, 1)))],
    )
    assert res.image is not None
    svg = res.image.svg
    assert svg.count("<style>") == 1
    assert "fill=" not in svg and "stroke=" not in svg
    # every colour the legend lists appears exactly once -- inside the style block.
    for entry in res.image.legend:
        assert svg.count(entry["color"]) == 1


def test_style_block_lists_only_used_rules() -> None:
    # §V86/§V82: like the legend, the style block carries only the rules this render
    # uses -- a board with one melee tile and no routes styles no wall/road/marker.
    res = render_stage_map(width=1, height=1, cells=[MapCell(0, 0, "LOWLAND", "MELEE", True)])
    assert res.image is not None
    svg = res.image.svg
    assert "#90caf9" in svg  # the melee fill, in the style block
    for absent in ("#546e7a", "#cfd8dc", "#2e7d32", "#c62828", "#ef6c00"):
        assert absent not in svg


def test_gridlines_are_a_single_path_not_line_elements() -> None:
    # §V86 (B94): the 11x7 board carried 20 <line> elements each restating a stroke;
    # gridlines are one <path> of M/V/H segments styled by the shared grid class.
    res = render_stage_map(width=3, height=2, cells=[MapCell(0, 0, "LOWLAND", "MELEE", True)])
    assert res.image is not None
    svg = res.image.svg
    assert "<line" not in svg
    assert svg.count("<path class='g'") == 1
    # (eff_w + 1) vertical + (eff_h + 1) horizontal segments in the one path.
    assert svg.count("V") == 4 and svg.count("H") == 3


def test_identical_markers_at_same_coords_collapse_to_one() -> None:
    # §V86 (B94): distinct-geometry routes still converge -- four routes ending on one
    # exit drew four stacked, byte-identical end circles. Same kind + same coords
    # emits once; the distinct start markers all stay.
    routes = [MapRoute(start=(0, y), end=(2, 1)) for y in range(3)]
    res = render_stage_map(width=3, height=3, cells=[], routes=routes)
    assert res.image is not None
    svg = res.image.svg
    assert svg.count("class='rs'") == 3  # three distinct spawn cells
    assert svg.count("class='re'") == 1  # one shared exit cell, one circle


def test_get_stage_description_says_map_image_is_display_only() -> None:
    # §V86 (B94): the SVG is a display artifact, not a reasoning surface -- the tool
    # description says so and points at include_map's tile_grid for reasoning.
    def _no_conn():  # type: ignore[no-untyped-def]
        raise RuntimeError("no connection needed for description inspection")

    desc = build_get_stage_spec(_no_conn).description
    assert "display only" in desc
    assert "tile_grid" in desc


# --- pure renderer: §V22 bounded, fail closed ---------------------------------


def test_oversize_board_by_cell_count_omits_image_with_limitation() -> None:
    # §V22: a pathological board (1_000_000 cells) is refused before a huge string
    # is built -- no image, a caption instead.
    res = render_stage_map(width=1000, height=1000, cells=[], routes=[])
    assert res.image is None
    assert res.limitation is not None
    assert "map image omitted" in res.limitation


def test_oversize_by_stored_tile_count_omits_image() -> None:
    # §V22: more stored tiles than the cell cap -> refused.
    cells = [MapCell(i % 60, i // 60, "LOWLAND", "NONE", True) for i in range(MAX_MAP_CELLS + 1)]
    res = render_stage_map(width=None, height=None, cells=cells)
    assert res.image is None
    assert res.limitation is not None


def test_over_byte_budget_omits_image_with_limitation() -> None:
    # §V22 byte budget: a board within the cell cap whose rendered document would
    # still exceed the image budget is dropped here (with a caption) rather than
    # tripping the envelope cap and withholding the whole response.
    cells = [MapCell(x, y, "LOWLAND", "NONE", True) for y in range(63) for x in range(63)]
    assert len(cells) <= MAX_MAP_CELLS  # within the cell cap ...
    res = render_stage_map(width=63, height=63, cells=cells)
    assert res.image is None  # ... but over the byte budget
    assert res.limitation is not None
    assert "map image omitted" in res.limitation


def test_no_grid_data_renders_nothing_and_does_not_caption() -> None:
    # No dimensions and no tiles -> nothing to render, nothing to apologise for.
    res = render_stage_map(width=None, height=None, cells=[], routes=[])
    assert res.image is None
    assert res.limitation is None


# --- stored-shape adapters: start/end flat, checkpoints nested -----------------


def test_checkpoint_points_reads_the_nested_position_fragment() -> None:
    # §T20: a stored checkpoint is a {type, position: {col, row}, ...} object, unlike
    # the flat startPosition/endPosition -- the coordinate lives under `position`, so
    # reading a top-level col/row would silently drop every checkpoint (no polyline).
    decoded = [
        {"type": "MOVE", "time": 0.0, "position": {"col": 1, "row": 2}},
        {"type": "MOVE", "position": {"col": 3, "row": 4}},
    ]
    assert _checkpoint_points(decoded) == ((1, 2), (3, 4))


def test_checkpoint_points_accepts_a_bare_position_fallback() -> None:
    # A checkpoint already reduced to a bare {col, row} still resolves.
    assert _checkpoint_points([{"col": 5, "row": 6}]) == ((5, 6),)


def test_checkpoint_points_skips_a_positionless_checkpoint() -> None:
    # A malformed/positionless checkpoint is skipped, not fabricated (§V26).
    assert _checkpoint_points([{"type": "MOVE"}, {"position": {}}]) == ()
    # start/end fragments remain flat {col, row}.
    assert _point_xy({"col": 0, "row": 0}) == (0, 0)


def test_checkpoint_points_drops_wait_but_keeps_a_corner_move() -> None:
    # §V74(b)/B74: the render point reducer drops a typed WAIT marker but KEEPS a real
    # MOVE targeting grid corner (0, 0), so the renderer draws the genuine waypoint.
    decoded = [
        {"type": "MOVE", "position": {"col": 0, "row": 0}},  # real corner MOVE -> kept
        {"type": "WAIT", "position": {"col": 0, "row": 0}},  # WAIT marker -> dropped
        {"type": "MOVE", "position": {"col": 4, "row": 2}},
    ]
    assert _checkpoint_points(decoded) == ((0, 0), (4, 2))


# --- get_stage tool wiring (4-4 fixture) --------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Build the 4-4 fixture candidate read-only (full stage/map/route/spawn rows)."""
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    return open_read_only(path)


def _handler(conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    return build_get_stage_spec(lambda: conn).handler


def test_default_response_has_no_map_image(conn: sqlite3.Connection) -> None:
    # §V22: the render is opt-in -- absent unless include_map_image is set.
    data = _handler(conn)(server="en", stage_code="4-4").to_dict()["data"]
    assert isinstance(data, dict)
    assert "map_image" not in data


def test_include_map_image_renders_main_story_stage(conn: sqlite3.Connection) -> None:
    # Acceptance: render-own covers a MAIN-story stage (main_04-04) -- the exact
    # case the §V63 URL-ref path cannot serve (no main-story maps in the mirror).
    env = _handler(conn)(server="en", stage_code="4-4", include_map_image=True)
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert data["stage"]["game_id"] == "main_04-04"  # type: ignore[index]
    image = data["map_image"]
    assert image["format"] == "svg"  # type: ignore[index]
    assert image["media_type"] == "image/svg+xml"  # type: ignore[index]
    svg = image["content"]  # type: ignore[index]
    assert isinstance(svg, str) and svg.startswith("<svg")
    # The two stored tiles + the route start/end are drawn from the stage's own grid:
    # 3 rects = the board backdrop + one per stored tile.
    assert svg.count("<rect") == 3
    assert "class='rs'" in svg and "class='re'" in svg
    # §V16/§V63: a derived image, never third-party art or the URL-ref path.
    low = svg.lower()
    for marker in _ART_MARKERS:
        assert marker not in low
    # §V22: the whole envelope stays well under the response cap.
    assert serialized_size(env) <= MAX_RESPONSE_BYTES


def test_map_image_is_independent_of_include_map(conn: sqlite3.Connection) -> None:
    # §V22: the image is its own opt-in -- requesting it does not pull the compact
    # tile grid, and vice versa.
    data = _handler(conn)(server="en", stage_code="4-4", include_map_image=True).to_dict()["data"]
    assert isinstance(data, dict)
    assert "map_image" in data
    assert "map" not in data
    assert "tile_grid" not in data


def test_include_map_image_is_read_only(conn: sqlite3.Connection) -> None:
    # §V2: rendering only reads -- no writes recorded on the connection.
    before = conn.total_changes
    get_stage(conn, server="en", stage_code="4-4", include_map_image=True)
    assert conn.total_changes == before


def test_get_stage_input_carries_include_map_image_flag() -> None:
    # §V21 additive: the flag defaults off and rides the wire schema; unknown
    # params stay forbidden (§V18).
    assert GetStageInput(server="en", stage_code="4-4").include_map_image is False
    assert (
        GetStageInput(server="en", stage_code="4-4", include_map_image=True).include_map_image
        is True
    )
    schema = tool_input_schema(GetStageInput)
    assert "include_map_image" in schema["properties"]
    assert schema["additionalProperties"] is False
