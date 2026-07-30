"""§V108/§V79 view-routing pins (§T203, B153).

A threat rule works from a SUMMARY of the stage -- a route-record count, tile tallies,
aggregated spawn bounds -- while the per-record detail sits on ``get_stage`` behind an
``include_`` flag. B153 is what happens when a rule states only the summary's limit: a
live client read "geometry not clustered" as a claim about the SERVER, concluded the
route data did not exist, and offered a community wiki -- the one source class this
project refuses. §V108 is the rule that stops it: a bounded view says what it omits AND
names the tool + flag holding the fuller one.

These tests pin four things the fix has to keep true:

* every deliberately coarse observation carries a routing limitation (not just the one
  B153 named);
* the flag it names is a REAL ``get_stage`` input -- a pointer to a flag that does not
  exist is a worse answer than no pointer, and prose alone cannot catch that;
* §V79's cross-ref is BIDIRECTIONAL, and names the flags rather than just the sibling;
* the §V71 (f) budget was paid by MOVING the level_variant note, not deleting it
  (§V111 b) -- so the note must be GONE from the description and PRESENT on the depth
  that emits the key it decodes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.analyzers.base import EnemyOccurrence, StageThreatContext, StageTiles
from arknights_mcp.analyzers.rules._common import fuller_view_note
from arknights_mcp.analyzers.rules.lane_route import LaneRouteRule
from arknights_mcp.analyzers.rules.pressure_spike import PressureSpikeRule
from arknights_mcp.analyzers.rules.tiles_deploy import TilesDeployRule
from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.tool_registry import MAX_TOOL_DESCRIPTION_CHARS
from arknights_mcp.mcp.tools._shared import LEVEL_VARIANT_NOTE
from arknights_mcp.mcp.tools.stage import (
    _ANALYZE_TOOL_DESCRIPTION,
    _TOOL_DESCRIPTION,
    ANALYZE_SIBLING_NOTE,
    STAGE_FACTS_SIBLING_NOTE,
    build_analyze_stage_spec,
)
from arknights_mcp.models.stages import GetStageInput
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: The include_ flags ``get_stage`` really publishes -- read off the input model, so a
#: rule that routes to an invented flag fails here instead of on a client's next call.
_REAL_FLAGS = {name for name in GetStageInput.model_fields if name.startswith("include_")}


def _occ(game_id: str, **kw: object) -> EnemyOccurrence:
    base: dict[str, object] = {
        "game_id": game_id,
        "display_name": game_id,
        "motion_type": "WALK",
        "damage_types": ("PHYSIC",),
        "total_count": 8,
    }
    base.update(kw)
    return EnemyOccurrence(**base)  # type: ignore[arg-type]


def _ctx(**kw: object) -> StageThreatContext:
    base: dict[str, object] = {
        "server": "en",
        "stage_code": "7-4",
        "stage_game_id": "main_07-03",
        "occurrences": (),
    }
    base.update(kw)
    return StageThreatContext(**base)  # type: ignore[arg-type]


def test_real_flags_are_discovered() -> None:
    # §V96: if the model stopped publishing include_ flags, every routing assertion
    # below would pass vacuously against an empty set.
    assert {"include_map", "include_routes", "include_spawns"} <= _REAL_FLAGS


# --- every coarse observation routes ------------------------------------------


def _lane_result() -> tuple[str, ...]:
    result = LaneRouteRule().evaluate(_ctx(route_count=63))
    assert result.observation is not None
    return result.observation.limitations


def _tiles_result() -> tuple[str, ...]:
    tiles = StageTiles(total=88, buildable_melee=2, buildable_ranged=1)
    result = TilesDeployRule().evaluate(_ctx(tiles=tiles))
    assert result.observation is not None
    return result.observation.limitations


def _spike_result() -> tuple[str, ...]:
    occ = _occ("enemy_1000_gopro", total_count=13, first_spawn_time=1.0, last_spawn_time=6.0)
    result = PressureSpikeRule().evaluate(_ctx(occurrences=(occ,)))
    assert result.observation is not None
    return result.observation.limitations


@pytest.mark.parametrize(
    ("limitations_of", "flag", "subject"),
    [
        (_lane_result, "include_routes", "route"),
        (_tiles_result, "include_map", "tile"),
        (_spike_result, "include_spawns", "spawn"),
    ],
)
def test_coarse_observation_routes_to_the_fuller_view(
    limitations_of: object, flag: str, subject: str
) -> None:
    # §V108: the coarse view names the tool AND the flag that returns the fine one.
    limitations = limitations_of()  # type: ignore[operator]
    routing = [lim for lim in limitations if "get_stage" in lim]
    assert routing, f"no limitation routes to get_stage: {limitations}"
    assert len(routing) == 1, f"§V66: one route per observation, got {routing}"
    assert flag in routing[0]
    assert subject in routing[0].lower()


@pytest.mark.parametrize(
    "limitations_of", [_lane_result, _tiles_result, _spike_result], ids=["lane", "tiles", "spike"]
)
def test_routing_limitation_names_a_real_flag(limitations_of: object) -> None:
    # §V101 class: a routing pointer to a flag get_stage does not accept sends the
    # client to a rejected call -- worse than the silence B153 reported.
    limitations = limitations_of()  # type: ignore[operator]
    routing = next(lim for lim in limitations if "get_stage" in lim)
    named = {flag for flag in _REAL_FLAGS if flag in routing}
    assert named, f"routes to no known include_ flag: {routing}"


def test_lane_route_no_longer_claims_a_server_wide_absence() -> None:
    # B153 verbatim: the shipped text was "geometry not clustered", full stop -- true of
    # the analysis, read as true of the server.
    routing = next(lim for lim in _lane_result() if "get_stage" in lim)
    assert routing != "raw route count != distinct lanes; geometry not clustered"
    assert "include_routes" in routing


def test_spawn_route_rides_once_not_per_enemy() -> None:
    # §V66: the route is the same sentence for every enemy in the stage, so N burst
    # enemies must not produce N copies of one pointer.
    occs = tuple(
        _occ(f"enemy_100{i}_x", total_count=13, first_spawn_time=1.0, last_spawn_time=6.0)
        for i in range(4)
    )
    result = PressureSpikeRule().evaluate(_ctx(occurrences=occs))
    assert result.observation is not None
    routing = [lim for lim in result.observation.limitations if "get_stage" in lim]
    assert len(routing) == 1


def test_count_only_spike_does_not_route_to_the_timeline() -> None:
    # The count-only arm computed NO window, so it has nothing for the timeline to
    # qualify -- routing there would answer a question this arm never raised.
    occ = _occ("enemy_1000_gopro", total_count=13, first_spawn_time=None, last_spawn_time=None)
    result = PressureSpikeRule().evaluate(_ctx(occurrences=(occ,)))
    assert result.observation is not None
    assert not [lim for lim in result.observation.limitations if "get_stage" in lim]


def test_routing_note_is_client_facing_text() -> None:
    # §V71 (b): no internal cite or jargon reaches the client.
    note = fuller_view_note(this_view="A is coarse.", flag="include_map", fuller="the fine view")
    for banned in ("§V", "§T", "§B", "B153", "degenerate"):
        assert banned not in note


# --- §V79 bidirectional cross-ref ---------------------------------------------


def test_get_stage_description_points_at_analyze_stage() -> None:
    # §V79: the sibling is named, and what it is FOR is stated (when to prefer it).
    assert ANALYZE_SIBLING_NOTE in _TOOL_DESCRIPTION
    assert "analyze_stage" in _TOOL_DESCRIPTION


def test_analyze_stage_description_points_back_and_names_the_flags() -> None:
    # §V79/§V108: naming only the sibling would leave the caller guessing WHICH flag
    # carries routes -- the exact gap that let B153's client conclude the data was gone.
    assert STAGE_FACTS_SIBLING_NOTE in _ANALYZE_TOOL_DESCRIPTION
    assert "get_stage" in _ANALYZE_TOOL_DESCRIPTION
    for flag in ("include_map", "include_routes", "include_spawns"):
        assert flag in _ANALYZE_TOOL_DESCRIPTION


@pytest.mark.parametrize(
    "description", [_TOOL_DESCRIPTION, _ANALYZE_TOOL_DESCRIPTION], ids=["get_stage", "analyze"]
)
def test_stage_descriptions_stay_within_budget(description: str) -> None:
    # §V71 (f)/§V111 (d): the cross-ref had to be PAID for, not appended.
    assert len(description) <= MAX_TOOL_DESCRIPTION_CHARS


# --- §V111 (b): the budget was paid by MOVING, not deleting --------------------


def test_level_variant_note_left_the_analyze_description() -> None:
    # §V111 (b): the note was moved off a description with 10 chars of headroom. If it
    # is still here, the cross-ref was paid for some other way.
    assert LEVEL_VARIANT_NOTE not in _ANALYZE_TOOL_DESCRIPTION


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """The 4-4 fixture candidate, read-only (stage + two enemies, one flyer)."""
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    return open_read_only(path)


def test_moved_level_variant_note_arrives_at_detailed_depth(conn: sqlite3.Connection) -> None:
    # §V111 (b): a move that drops the text is a deletion with extra steps. The note has
    # to REACH the client at its new home -- the one depth that emits level_variant.
    handler = build_analyze_stage_spec(lambda: conn).handler
    detailed = handler(server="en", stage_code="4-4", depth="detailed").to_dict()
    assert LEVEL_VARIANT_NOTE in detailed["limitations"]
    assert detailed["data"]["occurrences"]  # type: ignore[index]
    assert any(
        "level_variant" in occ
        for occ in detailed["data"]["occurrences"]  # type: ignore[index]
    )


@pytest.mark.parametrize("depth", ["summary", "standard"])
def test_shallower_depths_do_not_pay_for_the_note(conn: sqlite3.Connection, depth: str) -> None:
    # §V66 economy: neither depth emits level_variant, so neither owes a gloss decoding
    # it -- which is exactly why the description was the wrong home for it.
    envelope = build_analyze_stage_spec(lambda: conn).handler(
        server="en", stage_code="4-4", depth=depth
    )
    assert LEVEL_VARIANT_NOTE not in envelope.to_dict()["limitations"]
