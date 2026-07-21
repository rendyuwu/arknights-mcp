"""§T38 local MCP Inspector contract tests (§V14/§V23).

The MCP Inspector drives a server over a transport with two calls: ``tools/list``
(enumerate tools + their input schemas) then ``tools/call`` (invoke a tool and read
the result). These tests stand in for that flow against the *shared* registry both
transports dispatch from (:func:`build_tool_registry`, §V14) -- there is no live
transport yet (stdio wiring / Streamable HTTP land in later §T tasks), so we drive
the exact same registry -> spec.handler path a transport would.

Four request archetypes an Inspector operator exercises, mapped to the typed §V23
contract:

* **valid**     -- a well-formed call returns an ``ok`` envelope.
* **not_found** -- a well-formed call that resolves to nothing returns the typed
  ``not_found`` status (a domain outcome, not a protocol error) with a safe message.
* **ambiguous** -- an under/over-specified stage selector (both / neither of
  ``stage_code`` | ``game_id``) is rejected by the model's exactly-one guard; the
  §V23 vocabulary itself carries an ``ambiguous`` status for a future multi-match
  resolver to emit through the same envelope.
* **invalid**   -- a malformed call (unknown parameter, out-of-range bound, bad
  region, missing required field) is *rejected* at the model gate
  (``ValidationError`` -> protocol-level error), never silently coerced.

Across every archetype two invariants hold: dispatching through the assembled
registry yields the identical domain result as the tool's own spec (§V14 -- one
registry, no divergent logic), and every delivered envelope carries a typed status
from the §V23 vocabulary with no leaked stack trace or local path.

Local + offline: built from the pinned 4-4 fixture, so it runs under the default
``pytest -q`` (unlike the network-gated ``tests/contract`` upstream checks).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from mcp.types import Tool
from pydantic import ValidationError
from tests.support.drops import seed_stage_drop

from arknights_mcp.db.connection import DatabaseUnavailable, open_read_only
from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.pipeline import ServerImport, build_candidate, seed_data_sources
from arknights_mcp.mcp.envelopes import (
    SCHEMA_VERSION,
    STATUS_VALUES,
    ResponseEnvelope,
    error,
)
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.mcp.tools.enemy import build_get_enemy_spec
from arknights_mcp.mcp.tools.search import build_search_entities_spec
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
OPERATOR_ROOT = REPO_ROOT / "tests" / "fixtures" / "operator" / "en"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: The full §I.tool set the assembled registry exposes, in registration order
#: (§V14). The two data-metadata tools (§T77) register last.
_EXPECTED_TOOLS = (
    "search_entities",
    "search_stages",
    "get_stage",
    "get_enemy",
    "get_operator",
    "compare_operator_modules",
    "analyze_stage",
    "get_stage_drops",
    "get_item_drops",
    "get_announcements",
    "get_banners",
    "get_data_status",
    "get_data_sources",
)

#: One well-formed call per tool -> an ``ok`` result (the "valid" archetype). The
#: data-metadata tools take no parameters (they report server-side posture).
_VALID_CALLS: dict[str, dict[str, object]] = {
    "search_entities": {"query": "drone"},
    "search_stages": {"query": "4-4"},
    "get_stage": {"server": "en", "stage_code": "4-4"},
    "get_enemy": {"server": "en", "game_id": "enemy_1007_slime"},
    "get_operator": {"server": "en", "game_id": "char_002_amiya"},
    "compare_operator_modules": {"server": "en", "game_id": "char_002_amiya"},
    "analyze_stage": {"server": "en", "stage_code": "4-4"},
    "get_stage_drops": {"server": "en", "stage_code": "4-4"},
    "get_item_drops": {"server": "en", "game_id": "sugar"},
    "get_announcements": {"server": "en"},
    "get_banners": {"server": "en"},
    "get_data_status": {},
    "get_data_sources": {},
}

#: One well-formed-but-unmatched call per tool -> the typed ``not_found`` status.
#: The data-metadata tools have no ``not_found`` archetype: they report the active
#: build's own posture, so a well-formed call always yields a delivered status
#: (``ok``/``data_stale``), never a missing entity -- they are absent from this map.
_NOT_FOUND_CALLS: dict[str, dict[str, object]] = {
    "search_entities": {"query": "zzzznotanentity"},
    "search_stages": {"query": "zzzznotastage"},
    "get_stage": {"server": "en", "stage_code": "99-99"},
    "get_enemy": {"server": "en", "game_id": "enemy_9999_ghost"},
    "get_operator": {"server": "en", "game_id": "char_999_ghost"},
    "compare_operator_modules": {"server": "en", "game_id": "char_999_ghost"},
    "analyze_stage": {"server": "en", "stage_code": "99-99"},
    "get_stage_drops": {"server": "en", "stage_code": "99-99"},
}


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Build a read-only candidate from the 4-4 fixture (stage + two enemies) plus
    the en operator fixture (Amiya) + a seeded penguin drop cache, so every M-tier
    tool -- including get_stage_drops -- has a live target."""
    path = tmp_path / "cand.sqlite"
    build_candidate(
        path,
        [
            ServerImport(
                "en", LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot"), "local_snapshot"
            ),
            ServerImport(
                "en", LocalSnapshotAdapter(OPERATOR_ROOT, "en", "local_snapshot"), "local_snapshot"
            ),
        ],
        registry=load_source_registry(REGISTRY),
    )
    # A fresh (far-future expiry) drop so the get_stage_drops valid archetype -> ok.
    seed_stage_drop(path)
    return open_read_only(path)


@pytest.fixture
def registry(conn: sqlite3.Connection) -> ToolRegistry:
    """The shared registry both transports dispatch from (§V14)."""
    return build_tool_registry(lambda: conn, registry=load_source_registry(REGISTRY), mode="local")


def _call(registry: ToolRegistry, name: str, **params: object) -> ResponseEnvelope:
    """Invoke a tool exactly as a transport would: registry lookup -> handler."""
    return registry.get(name).handler(**params)


# --- Inspector tools/list: the enumerated, read-only, bounded tool set --------


def test_list_tools_exposes_full_m2_set(registry: ToolRegistry) -> None:
    # tools/list: the shared registry (§V14) enumerates exactly the M2 tool set,
    # in a deterministic order, so both transports show the same list.
    assert registry.names() == _EXPECTED_TOOLS


def test_listed_tools_are_read_only_with_bounded_schema(registry: ToolRegistry) -> None:
    tools = registry.to_mcp_tools()
    assert {t.name for t in tools} == set(_EXPECTED_TOOLS)
    for tool in tools:
        assert isinstance(tool, Tool)
        # §V2/§V28: every exposed tool is read-only + non-destructive.
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        # §V18: the bounded model's schema forbids unknown parameters on the wire.
        assert tool.inputSchema["type"] == "object"
        assert tool.inputSchema["additionalProperties"] is False


# --- valid -> ok --------------------------------------------------------------


@pytest.mark.parametrize("name", _EXPECTED_TOOLS)
def test_valid_call_returns_ok_envelope(registry: ToolRegistry, name: str) -> None:
    env = _call(registry, name, **_VALID_CALLS[name])
    assert env.status == "ok"
    assert env.schema_version == SCHEMA_VERSION
    assert isinstance(env.to_dict()["data"], dict)


def test_valid_factual_calls_carry_region_provenance(registry: ToolRegistry) -> None:
    # §V5: a factual tool result is region-attributed via provenance; en/cn are
    # never silently mixed. (Search returns region-tagged locators, not facts.)
    for name in (
        "get_stage",
        "get_enemy",
        "get_operator",
        "compare_operator_modules",
        "analyze_stage",
    ):
        env = _call(registry, name, **_VALID_CALLS[name])
        assert env.provenance and all(p.server == "en" for p in env.provenance)


def test_data_status_carries_per_snapshot_provenance(registry: ToolRegistry) -> None:
    # §V5 (finding #4): get_data_status is region-attributed too -- it emits one
    # provenance entry per active snapshot (server + snapshot_id + imported_at), so
    # a regression dropping provenance from the status envelope cannot pass green.
    env = _call(registry, "get_data_status", **_VALID_CALLS["get_data_status"])
    assert env.status == "ok"
    snapshots = env.to_dict()["data"]["snapshots"]  # type: ignore[index]
    assert isinstance(snapshots, list) and snapshots
    # One provenance entry per active snapshot, each fully populated + en-only.
    assert len(env.provenance) == len(snapshots)
    for prov in env.provenance:
        assert prov.server == "en"
        assert prov.snapshot_id
        assert prov.imported_at


# --- not_found -> typed status, safe copy -------------------------------------


@pytest.mark.parametrize("name", sorted(_NOT_FOUND_CALLS))
def test_not_found_call_returns_typed_status(registry: ToolRegistry, name: str) -> None:
    env = _call(registry, name, **_NOT_FOUND_CALLS[name])
    assert env.status == "not_found"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    message = data["message"]
    assert isinstance(message, str) and message
    # §V24: a not_found points at an admin action, never a query-time download/scrape.
    action = str(data.get("suggested_action", ""))
    assert "download" not in action.lower() and "scrape" not in action.lower()


# --- ambiguous -> exactly-one-selector guard + §V23 vocabulary -----------------


def test_ambiguous_stage_selector_rejected(registry: ToolRegistry) -> None:
    # Both selectors -> which key wins is ambiguous; the model's exactly-one guard
    # rejects it before any query runs.
    with pytest.raises(ValidationError, match="exactly one of stage_code or game_id"):
        _call(registry, "get_stage", server="en", stage_code="4-4", game_id="main_04-04")


def test_underspecified_stage_selector_rejected(registry: ToolRegistry) -> None:
    # Neither selector -> the lookup is under-specified; same exactly-one guard.
    with pytest.raises(ValidationError, match="exactly one of stage_code or game_id"):
        _call(registry, "get_stage", server="en")


def test_v23_vocabulary_carries_ambiguous_status() -> None:
    # §V23: the typed vocabulary includes ``ambiguous`` and the envelope layer can
    # carry it (a future multi-match resolver emits it through the same contract),
    # with no leaked detail.
    assert "ambiguous" in STATUS_VALUES
    env = error("ambiguous", "multiple stages matched the code", suggested_action="use game_id")
    assert env.status == "ambiguous"
    assert env.to_dict()["status"] == "ambiguous"


# --- invalid -> rejected at the model gate (protocol error) -------------------


@pytest.mark.parametrize(
    ("name", "params"),
    [
        # unknown parameter (extra="forbid") -- no smuggled field (§V18).
        ("search_entities", {"query": "drone", "bogus": 1}),
        # out-of-range limit -- rejected, never silently widened into a dump (§V19).
        ("search_entities", {"query": "drone", "limit": 0}),
        ("search_entities", {"query": "drone", "limit": 51}),
        ("search_stages", {"query": "4-4", "limit": 100}),
        # bad region (§V5 Literal).
        ("search_entities", {"query": "drone", "server": "jp"}),
        ("get_enemy", {"server": "jp", "game_id": "enemy_1007_slime"}),
        # empty required string (min_length).
        ("search_entities", {"query": ""}),
        ("get_enemy", {"server": "en", "game_id": ""}),
        # missing required field.
        ("get_enemy", {"game_id": "enemy_1007_slime"}),
        # out-of-range nested page bound (§V19).
        ("get_stage", {"server": "en", "stage_code": "4-4", "map_page": {"page_size": 101}}),
        # unknown parameter on a factual tool.
        ("get_stage", {"server": "en", "stage_code": "4-4", "bogus": 1}),
    ],
)
def test_invalid_input_is_rejected(
    registry: ToolRegistry, name: str, params: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        _call(registry, name, **params)


# --- §V14: the assembled registry adds no divergent logic ---------------------


def test_registry_dispatch_matches_direct_spec(conn: sqlite3.Connection) -> None:
    # §V14: dispatching through the shared registry yields the identical domain
    # result as the tool's own spec on the same DB + input -- there is no
    # per-registry logic for a transport to diverge on.
    registry = build_tool_registry(
        lambda: conn, registry=load_source_registry(REGISTRY), mode="local"
    )
    cases = (
        (build_get_enemy_spec, "get_enemy", {"server": "en", "game_id": "enemy_1007_slime"}),
        (build_search_entities_spec, "search_entities", {"query": "drone"}),
    )
    for build, name, params in cases:
        direct = build(lambda: conn).handler(**params).to_dict()
        via_registry = _call(registry, name, **params).to_dict()
        assert via_registry == direct


# --- §V23: every delivered envelope is typed + leak-free ----------------------


def test_every_delivered_status_is_in_vocabulary(registry: ToolRegistry) -> None:
    for name in _EXPECTED_TOOLS:
        # Every tool has a valid call; only the entity/analysis tools have a
        # not_found archetype (the data-metadata tools report server-side posture).
        call_sets = [_VALID_CALLS[name]]
        if name in _NOT_FOUND_CALLS:
            call_sets.append(_NOT_FOUND_CALLS[name])
        for params in call_sets:
            assert _call(registry, name, **params).status in STATUS_VALUES


def test_database_unavailable_fails_closed_through_registry() -> None:
    # §V23: a DB failure on the shared dispatch path fails closed to a fixed,
    # path/trace-free envelope -- no leaked file name reaches the client. Every tool
    # whose payload IS the build fails closed this way; get_data_sources is the one
    # exception (finding #1) -- its payload is the in-memory registry, so it degrades
    # to the registry-only projection (covered by its own test below).
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: /home/ubuntu/cand.sqlite")

    registry = build_tool_registry(boom, registry=load_source_registry(REGISTRY), mode="local")
    for name, params in _VALID_CALLS.items():
        if name == "get_data_sources":
            continue
        env = _call(registry, name, **params)
        assert env.status == "database_unavailable"
        body = str(env.to_dict()["data"])
        assert "/home/ubuntu" not in body
        assert "Traceback" not in body


def test_get_data_sources_degrades_to_registry_when_db_unavailable() -> None:
    # Finding #1 / §V27: get_data_sources' payload is the in-memory source registry;
    # the active build only enriches with the active snapshot per source. A missing/
    # unpromoted build must not withhold the source + license/attribution posture
    # (PRD §10.7/§13.10), so it degrades to the registry-only projection (ok, empty
    # active_snapshots) rather than failing closed. No path leaks in that body.
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: /home/ubuntu/cand.sqlite")

    registry = build_tool_registry(boom, registry=load_source_registry(REGISTRY), mode="local")
    env = _call(registry, "get_data_sources", **_VALID_CALLS["get_data_sources"])
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    sources = data["sources"]
    assert isinstance(sources, list) and sources  # registry still projected
    assert all(s["active_snapshots"] == [] for s in sources)  # no build => no enrichment
    body = str(data)
    assert "/home/ubuntu" not in body
    assert "Traceback" not in body


def test_data_status_data_stale_keeps_full_posture_body(tmp_path: Path) -> None:
    # Finding #6 / §V23: get_data_status is a posture tool -- a non-ok result
    # (``data_stale`` on an empty/unpromoted build) is a reported state, not a failed
    # request, so it keeps the full status body (warnings + suggested_action name the
    # admin action) instead of the ``{message}`` error-body shape. A client reads the
    # degraded posture from the same ``data`` keys as the ok case.
    path = tmp_path / "empty.sqlite"
    conn = build_database(path)
    seed_data_sources(conn, load_source_registry(REGISTRY))
    conn.commit()
    conn.close()
    ro = open_read_only(path)
    registry = build_tool_registry(
        lambda: ro, registry=load_source_registry(REGISTRY), mode="local"
    )

    env = _call(registry, "get_data_status")
    assert env.status == "data_stale"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    # Full posture body, not the error {message} shape.
    assert "message" not in data
    assert data["status"] == "data_stale"
    assert data["snapshots"] == []
    assert data["warnings"]  # names the empty-build condition
    assert data["suggested_action"]  # names the admin action (sync/import)
