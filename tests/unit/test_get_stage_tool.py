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

import re
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from arknights_mcp.db.connection import DatabaseUnavailable, open_read_only
from arknights_mcp.db.repositories.stages import StageRouteRow
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.envelopes import SCHEMA_VERSION
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools._shared import LIST_FIELD_CONVENTION
from arknights_mcp.mcp.tools.stage import (
    _spawn_to_dict,
    _stage_absent_field_limitations,
    build_get_stage_spec,
)
from arknights_mcp.models.common import PAGE_SIZE_MAX
from arknights_mcp.services.stage_route_digest import (
    _digest_checkpoints,
    _distinct_routes,
    _route_truncated_limitation,
)
from arknights_mcp.services.stages import (
    SpawnFacts,
    StageFacts,
    StageProvenance,
    get_stage,
)
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


def test_include_map_returns_header_and_compact_tile_grid(conn: sqlite3.Connection) -> None:
    data = _handler(conn)(server="en", stage_code="4-4", include_map=True).to_dict()["data"]
    header = data["map"]  # type: ignore[index]
    assert header["width"] == 8  # type: ignore[index]
    assert header["height"] == 5  # type: ignore[index]
    # §V74 (c): the grid rides as ONE compact per-row block -- one string per grid row
    # (top row / highest y first), a legend decoding each character, and a reserved
    # absent symbol for a cell with no tile. No per-tile object list, no page cursor.
    assert "tiles" not in data and "tiles_page" not in data
    grid = data["tile_grid"]  # type: ignore[index]
    assert grid["absent_symbol"] == "."  # type: ignore[index]
    assert len(grid["rows"]) == 5  # one string per grid row (height)  # type: ignore[index]
    assert all(len(row) == 8 for row in grid["rows"])  # width chars each  # type: ignore[index]
    # Decode the two fixture tiles via the legend: tile_end at (7,4) is top-right,
    # tile_start at (0,0) is bottom-left (rows[0] is the top row, y == height - 1).
    by_symbol = {e["symbol"]: e for e in grid["legend"]}  # type: ignore[index]
    top_right = by_symbol[grid["rows"][0][7]]  # type: ignore[index]
    bottom_left = by_symbol[grid["rows"][4][0]]  # type: ignore[index]
    assert top_right["tile_key"] == "tile_end"
    assert bottom_left["tile_key"] == "tile_start"
    # Every legend entry carries the four typed tile fields (§V18 allowlisted), no more.
    for entry in grid["legend"]:  # type: ignore[index]
        assert set(entry) == {"symbol", "tile_key", "height_type", "buildable_type", "passable"}


def test_tile_grid_response_carries_forbidden_vs_passable_gloss(
    conn: sqlite3.Connection,
) -> None:
    # §V74 (d): the raw source pairs a forbidden/non-buildable tile_key with
    # passable:true, which reads as a contradiction; every grid response carries a
    # gloss that deployment and enemy-passability are separate properties. The same
    # gloss is stated in the tool description.
    env = _handler(conn)(server="en", stage_code="4-4", include_map=True)
    lims = " ".join(env.to_dict()["limitations"])  # type: ignore[arg-type]
    assert "passable" in lims and "deploy" in lims.lower()
    desc = build_get_stage_spec(lambda: conn).description
    assert "tile_forbidden" in desc and "passable" in desc


def test_include_routes(conn: sqlite3.Connection) -> None:
    # §V74 (a): routes are emitted as DISTINCT geometry + occurrence_count. The 4-4
    # fixture has a single route record -> one distinct geometry, occurrence 1.
    data = _handler(conn)(server="en", stage_code="4-4", include_routes=True).to_dict()["data"]
    routes = data["routes"]  # type: ignore[index]
    assert len(routes) == 1
    assert routes[0]["route_indices"] == [0]  # type: ignore[index]
    assert routes[0]["occurrence_count"] == 1  # type: ignore[index]
    assert routes[0]["start_position"] == {"row": 0, "col": 0}  # type: ignore[index]
    assert "route_index" not in routes[0]  # type: ignore[operator]
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


def test_include_routes_checkpoints_is_always_a_list(conn: sqlite3.Connection) -> None:
    # §V51/B44: checkpoints ride the wire as an array unconditionally; the 4-4
    # route has none, and the empty set must be `[]`, never the source's `{}`.
    data = _handler(conn)(server="en", stage_code="4-4", include_routes=True).to_dict()["data"]
    checkpoints = data["routes"][0]["checkpoints"]  # type: ignore[index]
    assert checkpoints == []
    assert isinstance(checkpoints, list)


def test_checkpoint_digest_normalizes_every_source_shape() -> None:
    # §V51: the checkpoint digest coerces any decoded fragment to a JSON array --
    # the source's empty-set `{}` and a NULL column both yield `[]` (B44: the wire
    # type must not vary row-to-row); the emitted list is always a list.
    assert _digest_checkpoints({})[0] == []  # source empty-set serialized as a dict
    assert _digest_checkpoints(None)[0] == []  # NULL / undecodable column
    assert _digest_checkpoints([])[0] == []  # already an empty array


def test_checkpoint_digest_drops_wait_placeholder_and_snake_cases_keys() -> None:
    # §V74 (b): a WAIT checkpoint carries the non-spatial (0,0) placeholder position
    # and is dropped from the emitted geometry. §V71 (d): the surviving checkpoint's
    # camelCase keys (reachOffset/randomizeReachOffset) are normalized to snake_case.
    decoded = [
        {"type": "MOVE", "position": {"col": 3, "row": 2}, "reachOffset": {"x": 0, "y": 1}},
        {"type": "WAIT", "position": {"col": 0, "row": 0}, "randomizeReachOffset": False},
    ]
    emit, positions = _digest_checkpoints(decoded)
    assert positions == ((3, 2),)  # the WAIT placeholder position is gone
    assert len(emit) == 1
    cp = emit[0]
    assert isinstance(cp, dict)
    assert cp["reach_offset"] == {"x": 0, "y": 1}  # camelCase key renamed
    assert "reachOffset" not in cp
    assert cp["type"] == "MOVE"  # value untouched; already-lowercase key kept


def _route_row(index: int, start: object, end: object, checkpoints: object) -> StageRouteRow:
    import json

    return StageRouteRow(
        route_index=index,
        start_position_json=json.dumps(start),
        end_position_json=json.dumps(end),
        checkpoints_json=json.dumps(checkpoints),
    )


def test_distinct_routes_collapses_identical_geometry() -> None:
    # §V74 (a): records sharing (start, end, checkpoint positions) collapse to one
    # distinct geometry carrying every contributing route_index + an occurrence_count.
    a = {"col": 0, "row": 0}
    b = {"col": 5, "row": 2}
    c = {"col": 9, "row": 4}
    records = [
        _route_row(0, a, b, []),
        _route_row(1, a, b, []),  # byte-identical to 0 -> same geometry
        _route_row(2, a, c, []),  # different end -> distinct
    ]
    distinct = _distinct_routes(records)
    assert len(distinct) == 2
    assert distinct[0].route_indices == (0, 1)
    assert distinct[0].occurrence_count == 2
    assert distinct[0].start_position == a and distinct[0].end_position == b
    assert distinct[1].route_indices == (2,)
    assert distinct[1].occurrence_count == 1


def test_distinct_routes_merges_records_differing_only_by_wait_placeholder() -> None:
    # §V74 (a)+(b): two records with the same spatial path but one carrying an extra
    # WAIT (0,0) placeholder collapse to one -- the placeholder is not geometry.
    a = {"col": 0, "row": 0}
    b = {"col": 5, "row": 2}
    move = {"type": "MOVE", "position": {"col": 3, "row": 1}}
    wait = {"type": "WAIT", "position": {"col": 0, "row": 0}}
    records = [
        _route_row(0, a, b, [move]),
        _route_row(1, a, b, [move, wait]),  # same path + a WAIT marker
    ]
    distinct = _distinct_routes(records)
    assert len(distinct) == 1
    assert distinct[0].route_indices == (0, 1)
    assert distinct[0].occurrence_count == 2
    assert len(distinct[0].checkpoints) == 1  # the WAIT marker is dropped from the emit


def test_checkpoint_digest_keeps_a_real_move_at_grid_corner() -> None:
    # §V74 (b)/B74: WAIT is detected by the typed `type` field, NOT a (0,0) position
    # coincidence. A real MOVE targeting grid corner (0,0) must be KEPT -- the earlier
    # position-only heuristic dropped it as if it were a WAIT placeholder.
    decoded = [
        {"type": "MOVE", "position": {"col": 0, "row": 0}},  # real corner MOVE -> kept
        {"type": "WAIT", "position": {"col": 0, "row": 0}},  # WAIT marker -> dropped
    ]
    emit, positions = _digest_checkpoints(decoded)
    assert positions == ((0, 0),)  # the corner MOVE survives; only the WAIT is gone
    assert len(emit) == 1
    assert emit[0]["type"] == "MOVE"  # type: ignore[index,call-overload]


def test_checkpoint_digest_falls_back_to_placeholder_only_when_type_absent() -> None:
    # §V26/B74: position (0,0) is corroboration ONLY -- consulted when a checkpoint
    # carries no typed `type` field. A typeless (0,0) is treated as a placeholder
    # (dropped); a typeless non-corner point offers a real waypoint (kept).
    decoded = [
        {"position": {"col": 0, "row": 0}},  # typeless corner -> placeholder fallback
        {"position": {"col": 2, "row": 3}},  # typeless real point -> kept
    ]
    _, positions = _digest_checkpoints(decoded)
    assert positions == ((2, 3),)


def test_distinct_routes_keep_routes_differing_only_by_a_corner_move() -> None:
    # §V74 (a)+(b)/B74: two routes differing ONLY by a real MOVE through grid corner
    # (0,0) are distinct geometry and must NOT collapse (the position-only heuristic
    # collapsed them, inflating occurrence_count + corrupting the polyline).
    a = {"col": 0, "row": 0}
    b = {"col": 5, "row": 2}
    move = {"type": "MOVE", "position": {"col": 3, "row": 1}}
    corner = {"type": "MOVE", "position": {"col": 0, "row": 0}}
    records = [
        _route_row(0, a, b, [move]),
        _route_row(1, a, b, [move, corner]),  # same path + a real corner MOVE
    ]
    distinct = _distinct_routes(records)
    assert len(distinct) == 2
    assert distinct[0].occurrence_count == 1
    assert distinct[1].occurrence_count == 1


def test_include_routes_truncation_disclosed_when_read_hits_cap(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # §V74 (a)/B73: the raw route read is capped at MAX_MAP_ROUTES BEFORE dedup, so a
    # raw count == cap means records past it were dropped and the distinct total may
    # under-report -- get_stage must say so, not silently undercount. Squeeze the cap
    # to 1 so the fixture's single route trips it (the same as a real 1000-record cap
    # hit) without seeding a thousand rows.
    monkeypatch.setattr("arknights_mcp.services.stages.MAX_MAP_ROUTES", 1)
    env = _handler(conn)(server="en", stage_code="4-4", include_routes=True)
    lims = " ".join(env.to_dict()["limitations"])  # type: ignore[arg-type]
    assert "truncated" in lims and "under-report" in lims


def test_include_routes_no_truncation_limitation_under_cap(
    conn: sqlite3.Connection,
) -> None:
    # Negative case: the fixture has 1 route, far under the real cap -> no truncation
    # say-so (the disclosure fires only on a genuine cap hit).
    env = _handler(conn)(server="en", stage_code="4-4", include_routes=True)
    lims = " ".join(env.to_dict()["limitations"])  # type: ignore[arg-type]
    assert "truncated" not in lims


def test_route_truncated_limitation_carries_no_spec_cite() -> None:
    # §V71 (b): a runtime client-facing string never leaks a §V/§T/B spec cite.
    text = _route_truncated_limitation()
    assert "§" not in text
    assert not re.search(r"\bB\d", text)


def test_sections_are_independent(conn: sqlite3.Connection) -> None:
    # Only the requested section appears; the others stay off (§V22).
    data = _handler(conn)(server="en", stage_code="4-4", include_spawns=True).to_dict()["data"]
    assert set(data) == {"stage", "spawns", "spawns_page"}


def test_sections_coexist_and_spawns_page_independently(conn: sqlite3.Connection) -> None:
    # §V19/§V74 (c): the tile grid rides whole (unpaged) while spawns still page on
    # their own cursor -- requesting both returns the full grid AND a bounded spawns
    # page, neither shifting the other.
    data = _handler(conn)(
        server="en",
        stage_code="4-4",
        include_map=True,
        include_spawns=True,
        spawns_page={"page": 1, "page_size": 1},
    ).to_dict()["data"]
    assert data["tile_grid"]["rows"]  # full grid present, no page cursor  # type: ignore[index]
    # spawns bounded to their own page (1 of 2), untouched by the map section.
    assert len(data["spawns"]) == 1  # type: ignore[index]
    assert data["spawns_page"] == {  # type: ignore[index]
        "page": 1,
        "page_size": 1,
        "total": 2,
        "has_more": True,
    }


# --- §V67/§V26 (B58) null discipline: omit always-null scalars + absent limitation --


def _spawn(**overrides: object) -> SpawnFacts:
    base: dict[str, object] = {
        "wave_index": 0,
        "enemy_game_id": "enemy_1007_slime",
        "enemy_level_variant": 0,
        "route_index": 0,
        "spawn_time": 1.0,
        "count": 1,
        "interval": 0.0,
        "spawn_group": None,
        "hidden": False,
        "variant_id": None,
    }
    base.update(overrides)
    return SpawnFacts(**base)  # type: ignore[arg-type]


def test_spawn_group_omitted_when_absent() -> None:
    # §V67: ``spawn_group`` is an always-optional scalar -- omitted when the source
    # carried none, emitted when present (never an ambiguous null).
    assert "spawn_group" not in _spawn_to_dict(_spawn(spawn_group=None))
    assert _spawn_to_dict(_spawn(spawn_group="g0"))["spawn_group"] == "g0"


def test_spawn_group_present_on_fixture_spawns(conn: sqlite3.Connection) -> None:
    # The 4-4 spawns DO carry a spawn group -> the key is present (positive case).
    data = _handler(conn)(server="en", stage_code="4-4", include_spawns=True).to_dict()["data"]
    groups = {s["spawn_group"] for s in data["spawns"]}  # type: ignore[index]
    assert groups == {"g0", "g1"}


def test_stage_absent_field_limitation_helper() -> None:
    # §V67/§V26 (B58): the helper names an absent expected scalar and emits nothing when
    # both are present.
    prov = StageProvenance(snapshot_id="en:x", imported_at="t")
    bare = StageFacts(
        server="en",
        game_id="bare",
        stage_code="B-1",
        display_name="Bare",
        zone_game_id=None,
        stage_type=None,
        difficulty=None,
        sanity_cost=10,
        recommended_level=None,
        max_life_points=None,
        provenance=prov,
    )
    lim = _stage_absent_field_limitations(bare)
    assert len(lim) == 1
    assert "recommended_level" in lim[0] and "max_life_points" in lim[0]
    present = replace(bare, recommended_level=45, max_life_points=3)
    assert _stage_absent_field_limitations(present) == ()


def test_4_4_stage_has_no_absent_field_limitation(conn: sqlite3.Connection) -> None:
    # 4-4 carries recommendedLevel (45) + maxLifePoints (3), so no "not present" caveat.
    env = _handler(conn)(server="en", stage_code="4-4")
    assert not any("not present" in lim.lower() for lim in env.limitations)


def test_bare_stage_names_absent_scalars_on_the_wire(tmp_path: Path) -> None:
    # §V67/§V26 (B58): a stage whose source omits recommended_level + max_life_points
    # surfaces a standing "not present in source" limitation naming them (end to end).
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    rw = sqlite3.connect(str(path))
    try:
        snap = rw.execute(
            "SELECT snapshot_id FROM source_snapshots WHERE server = 'en' LIMIT 1"
        ).fetchone()[0]
        prov = rw.execute(
            "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
            "record_hash, transform_version, field_policy_version) VALUES "
            "(?, 'stage_table', 'bare', 'rh', '1', '1')",
            (snap,),
        ).lastrowid
        rw.execute(
            "INSERT INTO stages (server, game_id, stage_code, display_name, sanity_cost, "
            "provenance_id) VALUES ('en', 'bare_stage', 'B-1', 'Bare', 10, ?)",
            (prov,),
        )
        rw.commit()
    finally:
        rw.close()

    env = build_get_stage_spec(lambda: open_read_only(path)).handler(
        server="en", game_id="bare_stage"
    )
    assert env.status == "ok"
    blob = " ".join(env.limitations).lower()
    assert "recommended_level" in blob and "max_life_points" in blob
    assert "not present" in blob
    # §V71: no internal cite/jargon in the client-facing limitation.
    assert all("§v" not in lim.lower() and "b58" not in lim.lower() for lim in env.limitations)


def test_description_states_list_field_convention(conn: sqlite3.Connection) -> None:
    # §V67: the []-vs-absent convention is stated in the tool description.
    assert LIST_FIELD_CONVENTION in build_get_stage_spec(lambda: conn).description


# --- §V19 bounded pagination --------------------------------------------------


def test_tile_grid_is_a_single_unpaged_block(conn: sqlite3.Connection) -> None:
    # §V74 (c): the compact grid replaces the paged per-tile list -- there is no map
    # page cursor and the whole board comes in one response. map_page is no longer an
    # accepted parameter (dropped with the paged shape).
    handler = _handler(conn)
    data = handler(server="en", stage_code="4-4", include_map=True).to_dict()["data"]
    assert "tile_grid" in data and "tiles_page" not in data
    with pytest.raises(ValidationError):
        handler(server="en", stage_code="4-4", include_map=True, map_page={"page": 1})


def test_page_size_out_of_range_rejected_at_gate(conn: sqlite3.Connection) -> None:
    # §V19: the model gate *rejects* an out-of-range page_size before any query
    # (asserted on the still-paged spawns section).
    handler = _handler(conn)
    for bad in (0, -1, PAGE_SIZE_MAX + 1):
        with pytest.raises(ValidationError):
            handler(
                server="en", stage_code="4-4", include_spawns=True, spawns_page={"page_size": bad}
            )
    with pytest.raises(ValidationError):
        handler(server="en", stage_code="4-4", spawns_page={"page": 0})


def test_service_rejects_out_of_range_page_size(conn: sqlite3.Connection) -> None:
    # §V19 mirrored at the service: a direct caller gets the same rejection, not a
    # silent clamp (parallels the search-service limit contract, B23).
    with pytest.raises(ValueError, match="page_size"):
        get_stage(conn, server="en", stage_code="4-4", spawns_page_size=PAGE_SIZE_MAX + 1)
    with pytest.raises(ValueError, match="page"):
        get_stage(conn, server="en", stage_code="4-4", spawns_page=0)


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
    props = tool.inputSchema["properties"]
    # §V74 (c): the tile grid is unpaged, so map_page is gone; routes/spawns still page.
    assert {"routes_page", "spawns_page"} <= set(props)
    assert "map_page" not in props
    page_schema = tool.inputSchema["$defs"]["PageParams"]
    assert page_schema["properties"]["page_size"]["maximum"] == PAGE_SIZE_MAX
    assert page_schema["additionalProperties"] is False
