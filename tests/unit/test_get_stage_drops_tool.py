"""§T91 ``get_stage_drops`` tool tests (§V5/§V53/§V54/§V55/§V23; §I.tool).

The tool is the model -> service -> envelope bridge for one stage's penguin
drop-rate cache; these drive it end to end against the same production read-only
path (§V2) using the pinned 4-4 fixture (which imports the ``main_04-04`` stage
at ``sanity_cost=18``) plus a directly-seeded penguin drop cache with an explicit
``expires_at`` so the fresh/stale split is deterministic (no wall-clock coupling).
They assert:

* the §V5 region + provenance ride every delivered result, and en data is never
  surfaced under a cn query (en/cn never mixed);
* the §V53/§V54 penguin provenance chain (snapshot + fetched/expires) rides every
  drop, and a drop past its expiry flips the status to ``data_stale`` + adds a
  staleness limitation while still returning the drop (flagged, not withheld);
* ``include_efficiency`` surfaces the §T90 farming observations with every §V6
  field and no prescriptive verdict (§V55), and an expired cache downgrades the
  figure below the §V8 recommendation threshold;
* the typed §V23 envelope shape, including fail-closed ``not_found`` /
  ``database_unavailable`` / ``internal_error`` with no path/trace leak;
* the §I.tool wire contract: a read-only spec with a bounded input schema, present
  in the single shared registry both transports dispatch (§V14).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.support.drops import FUTURE_EXPIRY, PAST_EXPIRY, seed_stage_drop

from arknights_mcp.db.connection import DatabaseUnavailable, open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.envelopes import SCHEMA_VERSION
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.mcp.tools.drops import build_get_stage_drops_spec
from arknights_mcp.models.common import MAX_ID_LEN
from arknights_mcp.services.drops import get_stage_drops
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

_PROSCRIBED = ("best farm", "best-farm", "mandatory", "must ", "should ", "always farm")


def _candidate(tmp_path: Path) -> Path:
    """Build the 4-4 fixture candidate (imports the ``main_04-04`` stage)."""
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    return path


@pytest.fixture
def fresh_conn(tmp_path: Path) -> sqlite3.Connection:
    """4-4 with a fresh (far-future expiry) drop cache."""
    path = _candidate(tmp_path)
    seed_stage_drop(path, expires_at=FUTURE_EXPIRY)
    return open_read_only(path)


@pytest.fixture
def stale_conn(tmp_path: Path) -> sqlite3.Connection:
    """4-4 with an expired (past-expiry) drop cache."""
    path = _candidate(tmp_path)
    seed_stage_drop(path, expires_at=PAST_EXPIRY)
    return open_read_only(path)


@pytest.fixture
def bare_conn(tmp_path: Path) -> sqlite3.Connection:
    """4-4 with no drop cache at all (the stage exists, no penguin rows)."""
    return open_read_only(_candidate(tmp_path))


def _handler(conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    return build_get_stage_drops_spec(lambda: conn).handler


# --- drop facts + §V5 region + §V54 penguin provenance ------------------------


def test_ok_returns_drop_facts_with_penguin_provenance(fresh_conn: sqlite3.Connection) -> None:
    env = _handler(fresh_conn)(server="en", stage_code="4-4")
    assert env.status == "ok"
    assert env.schema_version == SCHEMA_VERSION
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    # §V66.2: the penguin provenance shared by every drop is hoisted to one block; no
    # per-drop provenance repetition, no efficiency block without the flag.
    assert set(data) == {"stage", "drop_provenance", "drops"}
    assert data["stage"]["sanity_cost"] == 18  # type: ignore[index]
    # §V54/§V66.2: the shared penguin provenance chain (snapshot + stamps) rides once.
    prov = data["drop_provenance"]
    assert prov == {  # type: ignore[comparison-overlap]
        "snapshot_id": "pg:en",
        "fetched_at": "2026-07-19T00:00:00+00:00",
        "expires_at": FUTURE_EXPIRY,
    }
    drops = data["drops"]
    assert isinstance(drops, list) and len(drops) == 1
    drop = drops[0]
    assert drop["item_game_id"] == "sugar"
    assert drop["region"] == "en"
    assert drop["drop_rate"] == 0.25
    assert drop["times"] == 5000
    # §V66.2: the shared provenance is NOT repeated on the row; §V67: a fresh drop
    # omits ``expired`` (its absence = not expired), so the deviant row stays visible.
    for hoisted in ("snapshot_id", "fetched_at", "expires_at"):
        assert hoisted not in drop
    assert "expired" not in drop


def test_drop_rate_rounded_to_4dp_on_wire(tmp_path: Path) -> None:
    # §V76: a penguin ``quantity / times`` sample statistic (here 1/3) is emitted
    # rounded to 4dp -- never the raw 17-digit ``repr`` float, whose digits over-state
    # the sample's real significance.
    path = _candidate(tmp_path)
    seed_stage_drop(path, expires_at=FUTURE_EXPIRY, drop_rate=1 / 3, times=3000)
    conn = open_read_only(path)
    drops = _handler(conn)(server="en", stage_code="4-4").to_dict()["data"]["drops"]  # type: ignore[index]
    assert drops[0]["drop_rate"] == 0.3333
    # the raw ratio is never on the wire.
    assert drops[0]["drop_rate"] != 1 / 3


def test_ok_carries_region_and_provenance(fresh_conn: sqlite3.Connection) -> None:
    # §V5: every delivered fact carries region + provenance.
    prov = _handler(fresh_conn)(server="en", stage_code="4-4").to_dict()["provenance"]
    assert isinstance(prov, list) and len(prov) == 1
    assert prov[0]["server"] == "en"
    assert prov[0]["snapshot_id"] and prov[0]["imported_at"]


def test_wrong_region_is_not_found(fresh_conn: sqlite3.Connection) -> None:
    # §V5: en drops are not surfaced under a cn query -- en/cn never mixed.
    assert _handler(fresh_conn)(server="cn", stage_code="4-4").status == "not_found"


def test_stage_without_drops_is_not_found(bare_conn: sqlite3.Connection) -> None:
    # §V24: a stage with no drop cache reports absent + a suggested admin action,
    # never an empty ``ok`` that reads as "this stage drops nothing".
    env = _handler(bare_conn)(server="en", stage_code="4-4")
    assert env.status == "not_found"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    action = data["suggested_action"]
    # §V24: never a query-time download/scrape fallback.
    assert "download" not in str(action).lower() and "scrape" not in str(action).lower()


# --- §V53 expiry -> data_stale ------------------------------------------------


def test_expired_drop_is_data_stale_but_still_returned(stale_conn: sqlite3.Connection) -> None:
    env = _handler(stale_conn)(server="en", stage_code="4-4")
    assert env.status == "data_stale"
    # The drop is flagged, not withheld (§V53): the data payload still carries it.
    data = env.to_dict()["data"]
    # §V66.2: the (past) expiry rides the hoisted shared block; §V67: the expired drop
    # carries ``expired:true`` so it stays visible, never presented as fresh.
    assert data["drop_provenance"]["expires_at"] == PAST_EXPIRY  # type: ignore[index]
    assert data["drops"][0]["expired"] is True  # type: ignore[index]
    # A staleness limitation names the refresh action; never presented as fresh.
    assert any("expiry" in lim or "stale" in lim for lim in env.limitations)
    # §V5: provenance still rides a stale-but-delivered fact.
    assert env.to_dict()["provenance"][0]["server"] == "en"  # type: ignore[index]


# --- §V55 include_efficiency --------------------------------------------------


def test_include_efficiency_emits_single_ranked_observation(fresh_conn: sqlite3.Connection) -> None:
    env = _handler(fresh_conn)(server="en", stage_code="4-4", include_efficiency=True)
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert "efficiency" in data
    # §V66.1: ONE ranked observation, not a list of per-drop observations.
    ob = data["efficiency"]["observation"]  # type: ignore[index]
    assert isinstance(ob, dict)
    # §V6: the identity fields are stated once at the observation level.
    assert set(ob) >= {"rule_id", "ranking", "confidence", "limitations", "analyzer_version"}
    assert ob["rule_id"] == "farming.sanity_per_item"
    assert ob["confidence"] >= 0.5  # fresh + well-sampled -> stable baseline
    # §V66.1: per-entity data lives in ranking rows that reference the drops facts.
    ranking = ob["ranking"]
    assert isinstance(ranking, list) and len(ranking) == 1
    row = ranking[0]
    assert row["id"] == "sugar"  # references the sibling drops list
    assert row["sanity_per_item"] == 72.0  # 18 / 0.25
    # §V66.1: a non-deviating (fresh + well-sampled) row omits its own confidence/
    # limitation, and the numbers already in the drops list are NOT re-copied onto it.
    assert "confidence" not in row and "limitations" not in row
    for reinstated in ("drop_rate", "sanity_cost", "sample_size", "times"):
        assert reinstated not in row
    # §V6: the analyzer version rides the envelope too.
    assert env.analyzer_version is not None
    # §V7/§V55: facts + observation only, never a prescriptive verdict.
    assert not any(word in str(data["efficiency"]).lower() for word in _PROSCRIBED)


def test_efficiency_omitted_without_the_flag(fresh_conn: sqlite3.Connection) -> None:
    env = _handler(fresh_conn)(server="en", stage_code="4-4")
    assert "efficiency" not in env.to_dict()["data"]
    assert env.analyzer_version is None


def test_expired_efficiency_downgraded_below_recommendation(
    stale_conn: sqlite3.Connection,
) -> None:
    # §V53/§V55: an expired cache downgrades the figure below the §V8 threshold, so
    # the row reads as a limitation, never a fresh recommendation.
    env = _handler(stale_conn)(server="en", stage_code="4-4", include_efficiency=True)
    assert env.status == "data_stale"
    ob = env.to_dict()["data"]["efficiency"]["observation"]  # type: ignore[index]
    row = ob["ranking"][0]
    assert row["confidence"] < 0.5
    assert any("expired" in lim.lower() for lim in row["limitations"])


# --- §V23 typed failures ------------------------------------------------------


def test_database_unavailable_envelope() -> None:
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: cand.sqlite")

    env = build_get_stage_drops_spec(boom).handler(server="en", stage_code="4-4")
    assert env.status == "database_unavailable"
    data = env.to_dict()["data"]
    assert data["message"] == "the active database is unavailable"  # type: ignore[index]
    assert "cand.sqlite" not in str(data)  # §V23: no local path / file name leak


def test_unexpected_error_fails_closed_to_internal_error() -> None:
    def boom() -> sqlite3.Connection:
        raise RuntimeError("secret path /home/ubuntu/db.sqlite blew up")

    env = build_get_stage_drops_spec(boom).handler(server="en", stage_code="4-4")
    assert env.status == "internal_error"
    assert str(env.to_dict()["data"]).find("/home/ubuntu") == -1
    assert "blew up" not in str(env.to_dict()["data"])


# --- §V18 input gate ----------------------------------------------------------


def test_unknown_parameter_rejected(fresh_conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(fresh_conn)(server="en", stage_code="4-4", include_map=True)


def test_bad_region_rejected(fresh_conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(fresh_conn)(server="jp", stage_code="4-4")


def test_both_selectors_rejected(fresh_conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(fresh_conn)(server="en", stage_code="4-4", game_id="main_04-04")


def test_neither_selector_rejected(fresh_conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(fresh_conn)(server="en")


def test_over_length_selector_rejected(fresh_conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(fresh_conn)(server="en", game_id="x" * (MAX_ID_LEN + 1))


# --- §V2 read-only / §I.tool wire contract / §V14 shared registry -------------


def test_service_is_read_only(fresh_conn: sqlite3.Connection) -> None:
    before = fresh_conn.total_changes
    get_stage_drops(fresh_conn, server="en", stage_code="4-4", include_efficiency=True)
    assert fresh_conn.total_changes == before


def test_spec_registers_read_only_with_bounded_schema(fresh_conn: sqlite3.Connection) -> None:
    reg = ToolRegistry()
    spec = reg.register(build_get_stage_drops_spec(lambda: fresh_conn))
    assert reg.names() == ("get_stage_drops",)
    assert spec.read_only is True
    tool = spec.to_mcp_tool()
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True
    assert tool.inputSchema["additionalProperties"] is False
    assert "server" in tool.inputSchema["required"]
    props = tool.inputSchema["properties"]
    # §V5: both selectors are on the wire (the model enforces exactly-one); the
    # §V18 id cap rides each (game_id is optional -> an anyOf(str<=cap, null)).
    assert {"stage_code", "game_id"} <= set(props)
    assert "include_efficiency" in props
    game_id_cap = next(
        opt["maxLength"] for opt in props["game_id"]["anyOf"] if opt.get("type") == "string"
    )
    assert game_id_cap == MAX_ID_LEN


def test_tool_registered_in_shared_registry(fresh_conn: sqlite3.Connection) -> None:
    # §V14: both transports dispatch this one registry; the tool must be in it.
    reg = build_tool_registry(
        lambda: fresh_conn, registry=load_source_registry(REGISTRY), mode="stdio"
    )
    assert "get_stage_drops" in reg.names()
