"""§T34 ``get_stage`` tool tests (§V19/§V22/§V23; §V5; §I.tool).

The tool is the model -> service -> envelope bridge for a single stage lookup;
these drive it end to end against the same production read-only path (§V2) using
the pinned 4-4 fixture. They assert:

* the §V22 default is compact facts + provenance -- the heavy map/routes/spawns
  sections stay off unless their include flag is set;
* each opted-in section is a *bounded page* (§V19): a small ``page_size`` returns
  a page with ``has_more`` and never an unbounded slice, and an out-of-range
  ``page_size`` is *rejected* at both the model gate and the service;
* the §V5 region + provenance ride every ``ok`` result, en/cn never mixed;
* the typed §V23 envelope shape, including fail-closed ``not_found`` /
  ``database_unavailable`` / ``internal_error`` with no path/trace leak.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from arknights_mcp.db.connection import DatabaseUnavailable, open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.envelopes import SCHEMA_VERSION
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools.stage import build_get_stage_spec
from arknights_mcp.models.common import PAGE_SIZE_MAX
from arknights_mcp.services.stages import get_stage
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"


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


# --- §V22 default: compact facts only -----------------------------------------


def test_default_response_is_facts_only(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", stage_code="4-4")
    assert env.status == "ok"
    assert env.schema_version == SCHEMA_VERSION
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    # §V22: heavy sections are opt-in -- absent by default.
    assert set(data) == {"stage"}
    stage = data["stage"]
    assert stage["game_id"] == "main_04-04"  # type: ignore[index]
    assert stage["stage_code"] == "4-4"  # type: ignore[index]
    assert stage["sanity_cost"] == 18  # type: ignore[index]
    assert stage["zone_game_id"] == "main_4"  # type: ignore[index]


def test_ok_carries_region_and_provenance(conn: sqlite3.Connection) -> None:
    # §V5: every factual response carries region + provenance.
    env = _handler(conn)(server="en", stage_code="4-4")
    prov = env.to_dict()["provenance"]
    assert isinstance(prov, list) and len(prov) == 1
    assert prov[0]["server"] == "en"
    assert prov[0]["snapshot_id"]
    assert prov[0]["imported_at"]


def test_lookup_by_game_id_matches_code(conn: sqlite3.Connection) -> None:
    by_code = _handler(conn)(server="en", stage_code="4-4").to_dict()["data"]
    by_id = _handler(conn)(server="en", game_id="main_04-04").to_dict()["data"]
    assert by_code == by_id


# --- opt-in sections ----------------------------------------------------------


def test_include_map_returns_header_and_paged_tiles(conn: sqlite3.Connection) -> None:
    data = _handler(conn)(server="en", stage_code="4-4", include_map=True).to_dict()["data"]
    stage_map = data["map"]  # type: ignore[index]
    assert stage_map["width"] == 8  # type: ignore[index]
    assert stage_map["height"] == 5  # type: ignore[index]
    tiles = stage_map["tiles"]  # type: ignore[index]
    assert {(t["x"], t["y"]) for t in tiles} == {(0, 0), (7, 4)}  # type: ignore[index]
    page = stage_map["tiles_page"]  # type: ignore[index]
    assert page == {"page": 1, "page_size": 50, "total": 2, "has_more": False}


def test_include_routes(conn: sqlite3.Connection) -> None:
    data = _handler(conn)(server="en", stage_code="4-4", include_routes=True).to_dict()["data"]
    routes = data["routes"]  # type: ignore[index]
    assert len(routes) == 1
    assert routes[0]["route_index"] == 0  # type: ignore[index]
    assert routes[0]["start_position"] == {"row": 0, "col": 0}  # type: ignore[index]
    assert data["routes_page"]["total"] == 1  # type: ignore[index]


def test_include_spawns(conn: sqlite3.Connection) -> None:
    data = _handler(conn)(server="en", stage_code="4-4", include_spawns=True).to_dict()["data"]
    spawns = data["spawns"]  # type: ignore[index]
    by_enemy = {s["enemy_game_id"]: s for s in spawns}  # type: ignore[index]
    assert set(by_enemy) == {"enemy_1007_slime", "enemy_1105_drone"}
    drone = by_enemy["enemy_1105_drone"]
    assert drone["wave_index"] == 0
    assert drone["count"] == 2
    assert drone["spawn_time"] == 8.0
    assert drone["route_index"] == 0
    assert data["spawns_page"]["total"] == 2  # type: ignore[index]


def test_sections_are_independent(conn: sqlite3.Connection) -> None:
    # Only the requested section appears; the others stay off (§V22).
    data = _handler(conn)(server="en", stage_code="4-4", include_spawns=True).to_dict()["data"]
    assert set(data) == {"stage", "spawns", "spawns_page"}


# --- §V19 bounded pagination --------------------------------------------------


def test_tile_pagination_bounds_and_has_more(conn: sqlite3.Connection) -> None:
    # §V19: a small page_size yields a bounded slice + has_more, never a dump.
    handler = _handler(conn)
    p1 = handler(
        server="en", stage_code="4-4", include_map=True, page={"page": 1, "page_size": 1}
    ).to_dict()["data"]["map"]
    assert len(p1["tiles"]) == 1  # type: ignore[index]
    assert p1["tiles_page"] == {"page": 1, "page_size": 1, "total": 2, "has_more": True}  # type: ignore[index]
    p2 = handler(
        server="en", stage_code="4-4", include_map=True, page={"page": 2, "page_size": 1}
    ).to_dict()["data"]["map"]
    assert len(p2["tiles"]) == 1  # type: ignore[index]
    assert p2["tiles_page"]["has_more"] is False  # type: ignore[index]
    # The two pages are disjoint -- deterministic (y, x) ordering.
    assert p1["tiles"][0] != p2["tiles"][0]  # type: ignore[index]


def test_page_size_out_of_range_rejected_at_gate(conn: sqlite3.Connection) -> None:
    # §V19: the model gate *rejects* an out-of-range page_size before any query.
    handler = _handler(conn)
    for bad in (0, -1, PAGE_SIZE_MAX + 1):
        with pytest.raises(ValidationError):
            handler(server="en", stage_code="4-4", include_map=True, page={"page_size": bad})
    with pytest.raises(ValidationError):
        handler(server="en", stage_code="4-4", page={"page": 0})


def test_service_rejects_out_of_range_page_size(conn: sqlite3.Connection) -> None:
    # §V19 mirrored at the service: a direct caller gets the same rejection, not a
    # silent clamp (parallels the search-service limit contract, B23).
    with pytest.raises(ValueError, match="page_size"):
        get_stage(conn, server="en", stage_code="4-4", page_size=PAGE_SIZE_MAX + 1)
    with pytest.raises(ValueError, match="page"):
        get_stage(conn, server="en", stage_code="4-4", page=0)


def test_unknown_parameter_rejected(conn: sqlite3.Connection) -> None:
    # §V18: extra="forbid" -> a crafted request cannot smuggle an unknown field.
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", stage_code="4-4", include_tiles=True)


def test_selector_must_be_exactly_one(conn: sqlite3.Connection) -> None:
    handler = _handler(conn)
    with pytest.raises(ValidationError):
        handler(server="en")  # neither
    with pytest.raises(ValidationError):
        handler(server="en", stage_code="4-4", game_id="main_04-04")  # both


# --- §V23 / §V5 typed failures ------------------------------------------------


def test_not_found_envelope(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", stage_code="9-9")
    assert env.status == "not_found"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert data["message"] == "no stage matched the given region and selector"
    # §V24: a not_found never suggests a query-time download/scrape.
    assert "download" not in data["suggested_action"].lower()  # type: ignore[union-attr]


def test_wrong_region_is_not_found(conn: sqlite3.Connection) -> None:
    # §V5: en data is not surfaced under a cn query.
    assert _handler(conn)(server="cn", stage_code="4-4").status == "not_found"


def test_database_unavailable_envelope() -> None:
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: cand.sqlite")

    env = build_get_stage_spec(boom).handler(server="en", stage_code="4-4")
    assert env.status == "database_unavailable"
    data = env.to_dict()["data"]
    # §V23: no local path / file name leaks into the client-facing message.
    assert data["message"] == "the active database is unavailable"  # type: ignore[index]
    assert "cand.sqlite" not in str(data)


def test_unexpected_error_fails_closed_to_internal_error() -> None:
    def boom() -> sqlite3.Connection:
        raise RuntimeError("secret path /home/ubuntu/db.sqlite blew up")

    env = build_get_stage_spec(boom).handler(server="en", stage_code="4-4")
    assert env.status == "internal_error"
    # §V23: the fixed message carries no exception text / stack trace / local path.
    assert str(env.to_dict()["data"]).find("/home/ubuntu") == -1
    assert "blew up" not in str(env.to_dict()["data"])


# --- §V2 read-only / §I.tool wire contract ------------------------------------


def test_service_is_read_only(conn: sqlite3.Connection) -> None:
    # §V2: the service only reads -- no writes recorded on the connection.
    before = conn.total_changes
    get_stage(conn, server="en", stage_code="4-4", include_map=True, include_spawns=True)
    assert conn.total_changes == before


def test_spec_registers_read_only_with_bounded_schema(conn: sqlite3.Connection) -> None:
    reg = ToolRegistry()
    spec = reg.register(build_get_stage_spec(lambda: conn))
    assert reg.names() == ("get_stage",)
    assert spec.read_only is True
    tool = spec.to_mcp_tool()
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True
    # §V18/§V19: unknown params forbidden + the page_size bound rides the wire.
    assert tool.inputSchema["additionalProperties"] is False
    assert "page" in tool.inputSchema["properties"]
    page_schema = tool.inputSchema["$defs"]["PageParams"]
    assert page_schema["properties"]["page_size"]["maximum"] == PAGE_SIZE_MAX
    assert page_schema["additionalProperties"] is False
