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
    assert set(data) == {"item", "stages"}  # no efficiency block without the flag
    assert data["item"]["game_id"] == "sugar"  # type: ignore[index]
    stages = data["stages"]
    assert isinstance(stages, list) and len(stages) == 2
    for stage in stages:
        assert stage["region"] == "en"
        # §V54: each stage carries its OWN penguin provenance chain.
        assert stage["snapshot_id"] == "pg:en"
        assert stage["fetched_at"] and stage["expires_at"] and stage["imported_at"]
        assert stage["sanity_cost"] is not None
        assert stage["expired"] is False


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
    obs = data["efficiency"]["observations"]  # type: ignore[index]
    assert isinstance(obs, list) and len(obs) == 3
    # §V6: every observation carries the five fields.
    for ob in obs:
        assert set(ob) >= {"rule_id", "evidence", "confidence", "limitations", "analyzer_version"}
        assert ob["rule_id"] == "farming.sanity_per_item"
    # §V60: ranked ascending by sanity per item -> stage a-1 (12) first, b-2 (120) last.
    refs = [ob["evidence"][0]["ref"] for ob in obs]
    assert refs == ["a-1", "4-4", "b-2"]
    # §V60: the mandatory comparison caveats ride the ranked result.
    blob = " ".join(data["efficiency"]["limitations"]).lower()  # type: ignore[index]
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
    # Only the en stage rides the comparison; the cn stage never leaks in.
    assert {s["region"] for s in data["stages"]} == {"en"}  # type: ignore[index]
    refs = [ob["evidence"][0]["ref"] for ob in data["efficiency"]["observations"]]  # type: ignore[index]
    assert refs == ["4-4"]
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
    obs = data["efficiency"]["observations"]  # type: ignore[index]
    refs = [ob["evidence"][0]["ref"] for ob in obs]
    assert "a-1" in refs
    # §V53/§V55: the expired figure is downgraded below the §V8 recommendation threshold.
    expired_ob = next(ob for ob in obs if ob["evidence"][0]["ref"] == "a-1")
    assert expired_ob["confidence"] < 0.5
    assert any("expired" in lim.lower() for lim in expired_ob["limitations"])
    # A staleness limitation names the refresh action; never presented as fresh.
    assert any("expiry" in lim or "stale" in lim for lim in env.limitations)


# --- §V24: absent item / no drop cache -> not_found, no fetch fallback ---------


def test_absent_item_is_not_found(tmp_path: Path) -> None:
    env = _handler(open_read_only(_candidate(tmp_path)))(server="en", game_id="nonexistent_item")
    assert env.status == "not_found"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    action = data["suggested_action"]
    # §V24: never a query-time download/scrape fallback.
    assert "download" not in str(action).lower() and "scrape" not in str(action).lower()


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
