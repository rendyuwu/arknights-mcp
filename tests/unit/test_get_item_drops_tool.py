"""§T104/§T105 ``get_item_drops`` tool tests (§V60/§V5/§V53/§V54/§V55/§V23; §I.tool).

The reverse of ``get_stage_drops``: the tool is the model -> service -> envelope
bridge for one item's drop-across-stages comparison. These drive it end to end
against the same production read-only path (§V2) using the pinned 4-4 fixture plus
several directly-seeded stages that all drop one item (each with its own sanity cost
/ drop rate / expiry) so the ranking + a per-stage fresh/stale split are
deterministic (no wall-clock coupling). They assert:

* §V60: ``include_efficiency`` ranks the stages ascending by sanity per item over ≥2
  stages, never a best-farm/mandatory verdict, and carries the mandatory
  availability / first-clear / byproduct comparison caveats;
* §V5: the item is resolved per region + provenance rides every delivered result;
  an en item's comparison never surfaces a cn stage (en/cn never mixed);
* §V53/§V54: each stage carries its OWN penguin provenance chain, and an expired
  stage flips the status to ``data_stale`` + adds a staleness limitation while
  staying in the ranking (downgraded below the §V8 threshold, not dropped);
* §V23: the typed envelope shape, incl. fail-closed ``not_found`` /
  ``database_unavailable`` / ``internal_error`` with no path/trace leak;
* §V18 input gate + the §I.tool wire contract: a read-only spec with a bounded input
  schema, present in the single shared registry both transports dispatch (§V14).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.support.drops import (
    FUTURE_EXPIRY,
    PAST_EXPIRY,
    StageDropSeed,
    seed_item_across_stages,
)

from arknights_mcp.db.connection import DatabaseUnavailable, open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.envelopes import SCHEMA_VERSION
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.mcp.tools.drops import build_get_item_drops_spec
from arknights_mcp.models.common import MAX_ID_LEN
from arknights_mcp.services.drops import get_item_drops
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

_PROSCRIBED = ("best farm", "best-farm", "mandatory", "must ", "should ", "always farm")


def _candidate(tmp_path: Path) -> Path:
    """Build the 4-4 fixture candidate (the penguin source is already registered)."""
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    return path


def _handler(conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    return build_get_item_drops_spec(lambda: conn).handler


# --- drop facts + §V5 region + §V54 penguin provenance ------------------------


def test_ok_returns_per_stage_facts_with_penguin_provenance(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4"), StageDropSeed("a-1")])
    env = _handler(open_read_only(path))(server="en", game_id="sugar")
    assert env.status == "ok"
    assert env.schema_version == SCHEMA_VERSION
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    # §V19 + §V66.2: the stages section + its bounded page + the hoisted shared
    # drop_provenance block; no efficiency block without the flag.
    assert set(data) == {"item", "drop_provenance", "stages", "stages_page"}
    assert data["item"]["game_id"] == "sugar"  # type: ignore[index]
    # §V54/§V66.2: the penguin provenance shared by both stages is hoisted once.
    prov = data["drop_provenance"]
    assert prov == {  # type: ignore[comparison-overlap]
        "snapshot_id": "pg:en",
        "fetched_at": "2026-07-19T00:00:00+00:00",
        "expires_at": FUTURE_EXPIRY,
        "imported_at": "2026-07-19T00:00:00+00:00",
    }
    # §V77/§V66 (B79): region stated ONCE on the parent item, never per stage row.
    assert data["item"]["server"] == "en"  # type: ignore[index]
    stages = data["stages"]
    assert isinstance(stages, list) and len(stages) == 2
    # §V22/§V19: a two-stage item fits page 1 with no further page.
    assert data["stages_page"] == {"page": 1, "page_size": 50, "total": 2, "has_more": False}  # type: ignore[index]
    for stage in stages:
        assert "region" not in stage
        assert stage["sanity_cost"] is not None
        # §V66.2: the shared provenance is NOT repeated per stage; §V67: a fresh stage
        # omits ``expired``.
        for hoisted in ("snapshot_id", "fetched_at", "expires_at", "imported_at"):
            assert hoisted not in stage
        assert "expired" not in stage


def test_drop_rate_rounded_to_4dp_on_wire(tmp_path: Path) -> None:
    # §V76: the per-stage penguin drop_rate (here 1/3) is emitted rounded to 4dp on the
    # reverse item->stage rows as well, never the raw 17-digit ``repr`` float.
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4", drop_rate=1 / 3, times=3000)])
    data = _handler(open_read_only(path))(server="en", game_id="sugar").to_dict()["data"]
    stages = data["stages"]  # type: ignore[index]
    assert stages[0]["drop_rate"] == 0.3333
    assert stages[0]["drop_rate"] != 1 / 3


def test_ok_carries_region_and_provenance(tmp_path: Path) -> None:
    # §V5: every delivered fact carries region + (penguin) provenance.
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4"), StageDropSeed("a-1")])
    prov = _handler(open_read_only(path))(server="en", game_id="sugar").to_dict()["provenance"]
    assert isinstance(prov, list) and len(prov) == 1  # one penguin snapshot for en
    assert prov[0]["server"] == "en"
    assert prov[0]["snapshot_id"] == "pg:en" and prov[0]["imported_at"]


def test_wrong_region_is_not_found(tmp_path: Path) -> None:
    # §V5: en item drops are not surfaced under a cn query -- en/cn never mixed.
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4"), StageDropSeed("a-1")])
    assert _handler(open_read_only(path))(server="cn", game_id="sugar").status == "not_found"


# --- §V60: ranked ascending over ≥2 stages ------------------------------------


def test_include_efficiency_ranks_ascending_by_sanity_per_item(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("4-4", sanity_cost=18, drop_rate=0.25),  # 72
            StageDropSeed("a-1", sanity_cost=6, drop_rate=0.5),  # 12
            StageDropSeed("b-2", sanity_cost=30, drop_rate=0.25),  # 120
        ],
    )
    env = _handler(open_read_only(path))(server="en", game_id="sugar", include_efficiency=True)
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert "efficiency" in data
    # §V66.1: ONE ranked observation over the stages, not a list of per-stage observations.
    ob = data["efficiency"]["observation"]  # type: ignore[index]
    assert isinstance(ob, dict)
    assert set(ob) >= {"rule_id", "ranking", "confidence", "limitations", "analyzer_version"}
    assert ob["rule_id"] == "farming.sanity_per_item"
    ranking = ob["ranking"]
    assert isinstance(ranking, list) and len(ranking) == 3
    # §V60: ranked ascending by sanity per item -> stage a-1 (12) first, b-2 (120) last.
    # §V68/B57: the row id is the unambiguous stage_game_id; the stage_code rides as name.
    assert [row["name"] for row in ranking] == ["a-1", "4-4", "b-2"]
    assert [row["sanity_per_item"] for row in ranking] == [12.0, 72.0, 120.0]
    # §V68: each ranking ref is the stage_game_id, joinable to the sibling stages list.
    stage_ids = {s["stage_game_id"] for s in data["stages"]}  # type: ignore[index]
    assert {row["id"] for row in ranking} <= stage_ids
    assert all(row["id"] != row["name"] for row in ranking)
    # §V60/§V66.1: the mandatory comparison caveats ride the observation-level limitations.
    blob = " ".join(ob["limitations"]).lower()
    assert "availability" in blob and "byproduct" in blob
    # §V6: the analyzer version rides the envelope too.
    assert env.analyzer_version is not None
    # §V7/§V55: an ordering + evidence, never a prescriptive verdict.
    text = str(data["efficiency"]).lower()
    assert not any(word in text for word in _PROSCRIBED)


def test_efficiency_omitted_without_the_flag(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4"), StageDropSeed("a-1")])
    env = _handler(open_read_only(path))(server="en", game_id="sugar")
    assert "efficiency" not in env.to_dict()["data"]
    assert env.analyzer_version is None


# --- §V68/B57: normal + tough share a stage_code -> DISTINCT joinable refs -----


def test_v68_normal_and_tough_same_code_get_distinct_joinable_refs(tmp_path: Path) -> None:
    # §V68/B57: two stages sharing stage_code "14-18" (a normal + tough pair) get
    # DISTINCT evidence refs -- each the unambiguous stage_game_id -- with the shared
    # code shown alongside as ``name``, so the refs join 1:1 to the sibling stages facts
    # list (which keys on stage_game_id) instead of colliding on one undecidable "14-18".
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("14-18", stage_game_id="main_10-09", sanity_cost=18, drop_rate=0.25),
            StageDropSeed("14-18", stage_game_id="tough_10-09", sanity_cost=36, drop_rate=0.25),
        ],
    )
    env = _handler(open_read_only(path))(server="en", game_id="sugar", include_efficiency=True)
    assert env.status == "ok"
    data = env.to_dict()["data"]
    ranking = data["efficiency"]["observation"]["ranking"]  # type: ignore[index]
    refs = [row["id"] for row in ranking]
    # main_10-09 = 18/0.25 = 72, tough_10-09 = 36/0.25 = 144 -> ascending main then tough.
    assert refs == ["main_10-09", "tough_10-09"]
    assert len(set(refs)) == 2  # two DISTINCT refs, not one ambiguous "14-18"
    # §V68: the shared stage_code rides alongside as the display name, never as the ref.
    assert all(row["name"] == "14-18" for row in ranking)
    assert "14-18" not in refs
    # §V68: every ref joins 1:1 to the sibling stages facts list.
    stage_ids = {s["stage_game_id"] for s in data["stages"]}  # type: ignore[index]
    assert set(refs) <= stage_ids
    assert {"main_10-09", "tough_10-09"} <= stage_ids


# --- §V5: region-scoped -- an en item never surfaces a cn stage ----------------


def test_comparison_is_region_scoped(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("4-4", region="en", sanity_cost=18, drop_rate=0.25),
            StageDropSeed("cn-1", region="cn", sanity_cost=6, drop_rate=0.5),
        ],
    )
    env = _handler(open_read_only(path))(server="en", game_id="sugar", include_efficiency=True)
    assert env.status == "ok"
    data = env.to_dict()["data"]
    # §V77/§V5 (B79): region stated ONCE on the parent item; the cn stage never leaks in
    # (only 4-4 is ranked below + the provenance is en-only).
    assert data["item"]["server"] == "en"  # type: ignore[index]
    assert all("region" not in s for s in data["stages"])  # type: ignore[index]
    ranking = data["efficiency"]["observation"]["ranking"]  # type: ignore[index]
    assert [row["name"] for row in ranking] == ["4-4"]
    assert {row["id"] for row in ranking} <= {s["stage_game_id"] for s in data["stages"]}  # type: ignore[index]
    # §V5: provenance is en-only.
    prov = env.to_dict()["provenance"]
    assert {p["server"] for p in prov} == {"en"}  # type: ignore[index]


# --- §V53/§V60: expired stage -> data_stale, downgraded but still ranked -------


def test_expired_stage_is_data_stale_but_still_ranked(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("4-4", sanity_cost=18, drop_rate=0.25, expires_at=FUTURE_EXPIRY),
            StageDropSeed("a-1", sanity_cost=6, drop_rate=0.5, expires_at=PAST_EXPIRY),
        ],
    )
    env = _handler(open_read_only(path))(server="en", game_id="sugar", include_efficiency=True)
    assert env.status == "data_stale"
    data = env.to_dict()["data"]
    # §V53: the expired stage is flagged, not withheld -- still in facts + ranking.
    expired_stage = next(s for s in data["stages"] if s["stage_code"] == "a-1")  # type: ignore[index]
    assert expired_stage["expired"] is True
    obs = data["efficiency"]["observation"]  # type: ignore[index]
    ranking = obs["ranking"]
    names = [row["name"] for row in ranking]
    assert "a-1" in names
    # §V53/§V55: the expired row is downgraded below the §V8 recommendation threshold.
    expired_row = next(row for row in ranking if row["name"] == "a-1")
    assert expired_row["confidence"] < 0.5
    assert any("expired" in lim.lower() for lim in expired_row["limitations"])
    # A staleness limitation names the refresh action; never presented as fresh.
    assert any("expiry" in lim or "stale" in lim for lim in env.limitations)


# --- §V66.2: provenance hoist -- shared block + only the deviant row carries its own -


def test_provenance_hoist_surfaces_only_the_deviant_stage(tmp_path: Path) -> None:
    # §V66.2: the penguin provenance shared by the stages is hoisted to one block; a
    # stage repeats a field ONLY where it deviates (here a different expiry), and a
    # fresh stage omits ``expired`` -- so the stale/deviant stage stays visible.
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("4-4", expires_at=FUTURE_EXPIRY),
            StageDropSeed("a-1", expires_at=PAST_EXPIRY),
        ],
    )
    env = _handler(open_read_only(path))(server="en", game_id="sugar")
    assert env.status == "data_stale"
    data = env.to_dict()["data"]
    # The shared block is the common (first-seen fresh) provenance.
    assert data["drop_provenance"]["expires_at"] == FUTURE_EXPIRY  # type: ignore[index]
    stages = {s["stage_code"]: s for s in data["stages"]}  # type: ignore[index]
    fresh, stale = stages["4-4"], stages["a-1"]
    # The fresh stage matches the shared block: no per-row provenance, no expired flag.
    for hoisted in ("snapshot_id", "fetched_at", "expires_at", "imported_at"):
        assert hoisted not in fresh
    assert "expired" not in fresh
    # The stale stage deviates: it carries its OWN (past) expiry + expired:true.
    assert stale["expires_at"] == PAST_EXPIRY
    assert stale["expired"] is True


# --- §V22/§V19: both growable lists are paged (B21) ---------------------------


def test_stages_are_paged(tmp_path: Path) -> None:
    # §V22/§V19: the per-stage facts page through their own bounded cursor so a common
    # item (dropping across many stages) never overflows the response cap (B21).
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path, [StageDropSeed("a-1"), StageDropSeed("b-2"), StageDropSeed("c-3")]
    )
    handler = _handler(open_read_only(path))
    env1 = handler(server="en", game_id="sugar", stages_page={"page": 1, "page_size": 2})
    data1 = env1.to_dict()["data"]
    # Ordered by stage_code; page 1 holds the first two + signals another bounded page.
    assert [s["stage_code"] for s in data1["stages"]] == ["a-1", "b-2"]  # type: ignore[index]
    assert data1["stages_page"] == {"page": 1, "page_size": 2, "total": 3, "has_more": True}  # type: ignore[index]
    env2 = handler(server="en", game_id="sugar", stages_page={"page": 2, "page_size": 2})
    data2 = env2.to_dict()["data"]
    assert [s["stage_code"] for s in data2["stages"]] == ["c-3"]  # type: ignore[index]
    assert data2["stages_page"]["has_more"] is False  # type: ignore[index]


def test_efficiency_observations_are_paged_over_global_ranking(tmp_path: Path) -> None:
    # §V60 + B21: the ranking is computed over the FULL set, THEN sliced -- page 1 is
    # the most-efficient N in GLOBAL order, never a per-page re-rank.
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("e1", sanity_cost=6, drop_rate=0.5),  # 12
            StageDropSeed("e2", sanity_cost=18, drop_rate=0.25),  # 72
            StageDropSeed("e3", sanity_cost=30, drop_rate=0.25),  # 120
            StageDropSeed("e4", sanity_cost=10, drop_rate=0.5),  # 20
            StageDropSeed("e5", sanity_cost=40, drop_rate=0.25),  # 160
        ],
    )
    handler = _handler(open_read_only(path))
    eff1 = handler(
        server="en",
        game_id="sugar",
        include_efficiency=True,
        efficiency_page={"page": 1, "page_size": 2},
    ).to_dict()["data"]["efficiency"]  # type: ignore[index]
    # §V66.1: ONE observation; its ``ranking`` rows are this page of the global ranking.
    # Global ascending: e1(12), e4(20), e2(72), e3(120), e5(160) -> page 1 = the two lowest.
    # §V68: the row id is the stage_game_id; assert order by the stage_code display name.
    assert [row["name"] for row in eff1["observation"]["ranking"]] == ["e1", "e4"]
    assert eff1["page"] == {"page": 1, "page_size": 2, "total": 5, "has_more": True}
    eff2 = handler(
        server="en",
        game_id="sugar",
        include_efficiency=True,
        efficiency_page={"page": 2, "page_size": 2},
    ).to_dict()["data"]["efficiency"]  # type: ignore[index]
    # Page 2 continues the SAME global ranking (not the two lowest of a fresh re-rank).
    assert [row["name"] for row in eff2["observation"]["ranking"]] == ["e2", "e3"]
    assert eff2["page"]["has_more"] is True
    # §V60/§V66.1: the mandatory comparison caveats ride the observation on every page.
    assert "availability" in " ".join(eff2["observation"]["limitations"]).lower()


def test_stale_holds_when_expired_stage_off_page(tmp_path: Path) -> None:
    # §V53 + B21: the stale verdict is computed over the FULL set, so data_stale holds
    # even when the only expired stage falls on a later page -- page 1 is never
    # presented as fresh just because its rows happen to be unexpired.
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("a-1", expires_at=FUTURE_EXPIRY),
            StageDropSeed("z-9", expires_at=PAST_EXPIRY),
        ],
    )
    env = _handler(open_read_only(path))(
        server="en", game_id="sugar", stages_page={"page": 1, "page_size": 1}
    )
    assert env.status == "data_stale"
    data = env.to_dict()["data"]
    # The returned page holds only the fresh stage; the expired one is off-page...
    assert [s["stage_code"] for s in data["stages"]] == ["a-1"]  # type: ignore[index]
    # §V67: the fresh page-1 stage omits ``expired`` (absence = not expired).
    assert "expired" not in data["stages"][0]  # type: ignore[operator]
    assert data["stages_page"]["has_more"] is True  # type: ignore[index]
    # ...yet the staleness posture holds (never presented as fresh).
    assert any("expiry" in lim or "stale" in lim for lim in env.limitations)


def test_out_of_range_page_size_rejected(tmp_path: Path) -> None:
    # §V19: an out-of-range page_size is rejected at the model gate, never silently
    # widened -- one contract, both places (mirrors get_stage).
    with pytest.raises(ValidationError):
        _handler(open_read_only(_candidate(tmp_path)))(
            server="en", game_id="sugar", stages_page={"page_size": 101}
        )


# --- §V24: absent item / no drop cache -> not_found, no fetch fallback ---------


def test_absent_item_is_not_found(tmp_path: Path) -> None:
    env = _handler(open_read_only(_candidate(tmp_path)))(server="en", game_id="nonexistent_item")
    assert env.status == "not_found"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    action = data["suggested_action"]
    # §V24: never a query-time download/scrape fallback.
    assert "download" not in str(action).lower() and "scrape" not in str(action).lower()
    # §V73/B67: the pointer is honest -- search_entities now resolves item name -> id,
    # so the not_found action names it (no longer a dead-end pointer).
    assert "search_entities" in str(action)


# --- §V23 typed failures ------------------------------------------------------


def test_database_unavailable_envelope() -> None:
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: cand.sqlite")

    env = build_get_item_drops_spec(boom).handler(server="en", game_id="sugar")
    assert env.status == "database_unavailable"
    data = env.to_dict()["data"]
    assert data["message"] == "the active database is unavailable"  # type: ignore[index]
    assert "cand.sqlite" not in str(data)  # §V23: no local path / file name leak


def test_unexpected_error_fails_closed_to_internal_error() -> None:
    def boom() -> sqlite3.Connection:
        raise RuntimeError("secret path /home/ubuntu/db.sqlite blew up")

    env = build_get_item_drops_spec(boom).handler(server="en", game_id="sugar")
    assert env.status == "internal_error"
    assert str(env.to_dict()["data"]).find("/home/ubuntu") == -1
    assert "blew up" not in str(env.to_dict()["data"])


# --- §V18 input gate ----------------------------------------------------------


def test_unknown_parameter_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _handler(open_read_only(_candidate(tmp_path)))(
            server="en", game_id="sugar", stage_code="4-4"
        )


def test_bad_region_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _handler(open_read_only(_candidate(tmp_path)))(server="jp", game_id="sugar")


def test_missing_game_id_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _handler(open_read_only(_candidate(tmp_path)))(server="en")


def test_over_length_game_id_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _handler(open_read_only(_candidate(tmp_path)))(server="en", game_id="x" * (MAX_ID_LEN + 1))


# --- §V2 read-only / §I.tool wire contract / §V14 shared registry -------------


def test_service_is_read_only(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4"), StageDropSeed("a-1")])
    conn = open_read_only(path)
    before = conn.total_changes
    get_item_drops(conn, server="en", game_id="sugar", include_efficiency=True)
    assert conn.total_changes == before


def test_spec_registers_read_only_with_bounded_schema(tmp_path: Path) -> None:
    conn = open_read_only(_candidate(tmp_path))
    reg = ToolRegistry()
    spec = reg.register(build_get_item_drops_spec(lambda: conn))
    assert reg.names() == ("get_item_drops",)
    assert spec.read_only is True
    tool = spec.to_mcp_tool()
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True
    assert tool.inputSchema["additionalProperties"] is False
    assert "server" in tool.inputSchema["required"]
    props = tool.inputSchema["properties"]
    # §V5: the item selector is on the wire; the §V18 id cap rides it (required ->
    # a direct string schema with a maxLength).
    assert "game_id" in props
    assert "include_efficiency" in props
    assert props["game_id"]["maxLength"] == MAX_ID_LEN


def test_tool_registered_in_shared_registry(tmp_path: Path) -> None:
    # §V14: both transports dispatch this one registry; the tool must be in it.
    conn = open_read_only(_candidate(tmp_path))
    reg = build_tool_registry(lambda: conn, registry=load_source_registry(REGISTRY), mode="stdio")
    assert "get_item_drops" in reg.names()
