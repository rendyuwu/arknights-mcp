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
    assert svg.count('class="tile"') == 2
    assert res.image.tile_count == 2
    assert 'class="board"' in svg
    assert 'class="grid"' in svg
    assert 'class="route-start"' in svg
    assert 'class="route-end"' in svg


def test_render_draws_checkpoint_polyline() -> None:
    res = render_stage_map(
        width=3,
        height=1,
        cells=[],
        routes=[MapRoute(start=(0, 0), end=(2, 0), checkpoints=((0, 0), (1, 0), (2, 0)))],
    )
    assert res.image is not None
    assert 'class="route-path"' in res.image.svg


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
    # The two stored tiles + the route start/end are drawn from the stage's own grid.
    assert svg.count('class="tile"') == 2
    assert 'class="route-start"' in svg and 'class="route-end"' in svg
    # §V16/§V63: a derived image, never third-party art or the URL-ref path.
    low = svg.lower()
    for marker in _ART_MARKERS:
        assert marker not in low
    # §V22: the whole envelope stays well under the response cap.
    assert serialized_size(env) <= MAX_RESPONSE_BYTES


def test_map_image_is_independent_of_include_map(conn: sqlite3.Connection) -> None:
    # §V22: the image is its own opt-in -- requesting it does not pull the paged
    # tile grid, and vice versa.
    data = _handler(conn)(server="en", stage_code="4-4", include_map_image=True).to_dict()["data"]
    assert isinstance(data, dict)
    assert "map_image" in data
    assert "map" not in data
    assert "tiles" not in data
    assert "tiles_page" not in data


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
