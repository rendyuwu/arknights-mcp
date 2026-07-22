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
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from arknights_mcp.db.connection import DatabaseUnavailable, open_read_only
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
from arknights_mcp.services.stages import (
    SpawnFacts,
    StageFacts,
    StageProvenance,
    _as_checkpoint_list,
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


def test_include_map_returns_header_and_paged_tiles(conn: sqlite3.Connection) -> None:
    data = _handler(conn)(server="en", stage_code="4-4", include_map=True).to_dict()["data"]
    header = data["map"]  # type: ignore[index]
    assert header["width"] == 8  # type: ignore[index]
    assert header["height"] == 5  # type: ignore[index]
    # Tiles ride as their own top-level paged section (symmetric with routes/spawns).
    tiles = data["tiles"]  # type: ignore[index]
    assert {(t["x"], t["y"]) for t in tiles} == {(0, 0), (7, 4)}  # type: ignore[index]
    assert data["tiles_page"] == {"page": 1, "page_size": 50, "total": 2, "has_more": False}  # type: ignore[index]


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


def test_include_routes_checkpoints_is_always_a_list(conn: sqlite3.Connection) -> None:
    # §V51/B44: checkpoints ride the wire as an array unconditionally; the 4-4
    # route has none, and the empty set must be `[]`, never the source's `{}`.
    data = _handler(conn)(server="en", stage_code="4-4", include_routes=True).to_dict()["data"]
    checkpoints = data["routes"][0]["checkpoints"]  # type: ignore[index]
    assert checkpoints == []
    assert isinstance(checkpoints, list)


def test_checkpoint_shaper_normalizes_every_source_shape() -> None:
    # §V51: the RouteFacts shaper coerces any decoded fragment to a JSON array --
    # the source's empty-set `{}` and a NULL column both become `[]`, a populated
    # list passes through unchanged (B44: the wire type must not vary row-to-row).
    assert _as_checkpoint_list({}) == []  # source empty-set serialized as a dict
    assert _as_checkpoint_list(None) == []  # NULL / undecodable column
    assert _as_checkpoint_list([]) == []  # already an empty array
    populated = [{"row": 1, "col": 2}]
    assert _as_checkpoint_list(populated) == populated  # populated passes through


def test_sections_are_independent(conn: sqlite3.Connection) -> None:
    # Only the requested section appears; the others stay off (§V22).
    data = _handler(conn)(server="en", stage_code="4-4", include_spawns=True).to_dict()["data"]
    assert set(data) == {"stage", "spawns", "spawns_page"}


def test_sections_page_independently(conn: sqlite3.Connection) -> None:
    # §V19 fix: each section has its own cursor -- paging the map to page 2 does
    # not shift the spawns section off (which keeps its own default page 1).
    data = _handler(conn)(
        server="en",
        stage_code="4-4",
        include_map=True,
        include_spawns=True,
        map_page={"page": 2, "page_size": 1},
    ).to_dict()["data"]
    assert len(data["tiles"]) == 1  # type: ignore[index]
    assert data["tiles_page"]["page"] == 2  # type: ignore[index]
    # spawns stay on their own page 1 -- both spawns still present, not emptied.
    assert {s["enemy_game_id"] for s in data["spawns"]} == {  # type: ignore[index]
        "enemy_1007_slime",
        "enemy_1105_drone",
    }
    assert data["spawns_page"]["page"] == 1  # type: ignore[index]


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


def test_tile_pagination_bounds_and_has_more(conn: sqlite3.Connection) -> None:
    # §V19: a small page_size yields a bounded slice + has_more, never a dump.
    handler = _handler(conn)
    d1 = handler(
        server="en", stage_code="4-4", include_map=True, map_page={"page": 1, "page_size": 1}
    ).to_dict()["data"]
    assert len(d1["tiles"]) == 1  # type: ignore[index]
    assert d1["tiles_page"] == {"page": 1, "page_size": 1, "total": 2, "has_more": True}  # type: ignore[index]
    d2 = handler(
        server="en", stage_code="4-4", include_map=True, map_page={"page": 2, "page_size": 1}
    ).to_dict()["data"]
    assert len(d2["tiles"]) == 1  # type: ignore[index]
    assert d2["tiles_page"]["has_more"] is False  # type: ignore[index]
    # The two pages are disjoint -- deterministic (y, x) ordering.
    assert d1["tiles"][0] != d2["tiles"][0]  # type: ignore[index]


def test_page_size_out_of_range_rejected_at_gate(conn: sqlite3.Connection) -> None:
    # §V19: the model gate *rejects* an out-of-range page_size before any query.
    handler = _handler(conn)
    for bad in (0, -1, PAGE_SIZE_MAX + 1):
        with pytest.raises(ValidationError):
            handler(server="en", stage_code="4-4", include_map=True, map_page={"page_size": bad})
    with pytest.raises(ValidationError):
        handler(server="en", stage_code="4-4", map_page={"page": 0})


def test_service_rejects_out_of_range_page_size(conn: sqlite3.Connection) -> None:
    # §V19 mirrored at the service: a direct caller gets the same rejection, not a
    # silent clamp (parallels the search-service limit contract, B23).
    with pytest.raises(ValueError, match="page_size"):
        get_stage(conn, server="en", stage_code="4-4", map_page_size=PAGE_SIZE_MAX + 1)
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
    assert {"map_page", "routes_page", "spawns_page"} <= set(props)
    page_schema = tool.inputSchema["$defs"]["PageParams"]
    assert page_schema["properties"]["page_size"]["maximum"] == PAGE_SIZE_MAX
    assert page_schema["additionalProperties"] is False
