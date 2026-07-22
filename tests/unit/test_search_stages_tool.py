"""§T33 ``search_stages`` tool tests (§V19/§V23; §I.tool).

The tool is the model -> service -> envelope bridge for the stage-scoped search;
these drive it end to end against the same production read-only path the service
tests use (§V2). They assert the typed §V23 envelope shape, the §V5 region
locator, the §V19 bound (rejected at the model gate + honored through the tool),
and the §T33 headline: an exact ``stage_code`` match is ranked first -- ahead of a
stage whose *name* merely contains the query.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from arknights_mcp.db.connection import DatabaseUnavailable, open_read_only
from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.importers.search_index import build_search_index
from arknights_mcp.mcp.envelopes import SCHEMA_VERSION
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools.search import build_search_stages_spec
from arknights_mcp.services.search import MAX_LIMIT
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Build the 4-4 fixture candidate (index populated in-pipeline) read-only."""
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    return open_read_only(path)


def _handler(conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    """The tool handler bound to a fixed connection provider."""
    return build_search_stages_spec(lambda: conn).handler


# --- §V23 typed envelope: ok result -------------------------------------------


def test_ok_envelope_shape(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(query="4-4")
    assert env.status == "ok"
    assert env.schema_version == SCHEMA_VERSION
    body = env.to_dict()["data"]
    assert isinstance(body, dict)
    assert body["query"] == "4-4"
    assert body["count"] == len(body["results"])  # type: ignore[arg-type]
    assert any(
        row["game_id"] == "main_04-04" and row["stage_code"] == "4-4" and row["server"] == "en"
        for row in body["results"]  # type: ignore[union-attr]
    )


def test_results_are_region_tagged_stage_locators(conn: sqlite3.Connection) -> None:
    # §V5 region travels per row; every hit is typed as a stage locator, and each
    # carries the §V70 difficulty variant tag (may be null when source omits it).
    for row in _handler(conn)(query="Combustion").to_dict()["data"]["results"]:  # type: ignore[index]
        assert row["server"] == "en"
        assert row["entity_type"] == "stage"
        assert set(row) == {
            "entity_type",
            "server",
            "game_id",
            "display_name",
            "stage_code",
            "difficulty",
        }


def test_matches_by_name_and_game_id(conn: sqlite3.Connection) -> None:
    # The stage is reachable by its human name and by its game id, not only code.
    assert _handler(conn)(query="Combustion").status == "ok"
    assert _handler(conn)(query="main_04-04").status == "ok"


def test_server_filter_scopes_region(conn: sqlite3.Connection) -> None:
    # §V5: the en 4-4 is not surfaced under a cn-scoped search.
    assert _handler(conn)(query="4-4", server="en").status == "ok"
    # §V50/§V24 (B42): cn has no active snapshot in this en-only build, so a
    # cn-scoped stage search is ``data_stale`` -- never a bare ``not_found``.
    assert _handler(conn)(query="4-4", server="cn").status == "data_stale"


# --- §T33 headline: exact stage_code ranked first -----------------------------


def _seed_provenance(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("s", "S", "O", "https://x", "t", '["en"]', "1", "l", "p", "r", "a", 1, "2026-07-17"),
    )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES (?,?,?,?,?,?,?)",
        ("en:abc", "s", "en", "2026-07-17", "mh", "imported", "1"),
    )
    cur = conn.execute(
        "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
        "record_hash, transform_version, field_policy_version) VALUES (?,?,?,?,?,?)",
        ("en:abc", "p", "k", "h", "1", "1"),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_stage(
    conn: sqlite3.Connection,
    game_id: str,
    stage_code: str,
    name: str,
    prov: int,
    difficulty: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO stages (server, game_id, stage_code, display_name, difficulty, "
        "provenance_id) VALUES (?,?,?,?,?,?)",
        ("en", game_id, stage_code, name, difficulty, prov),
    )


def test_exact_stage_code_ranked_first(tmp_path: Path) -> None:
    # §T33: a stage whose code is exactly "4-4" outranks stages that only share
    # the "4"/"-4" tokens -- even one whose *name* literally contains "4-4".
    path = tmp_path / "stages.sqlite"
    writer = build_database(path)
    prov = _seed_provenance(writer)
    _insert_stage(writer, "main_14-04", "14-4", "Decoy mentioning 4-4 in the name", prov)
    _insert_stage(writer, "main_04-14", "4-14", "Filler", prov)
    _insert_stage(writer, "main_04-04", "4-4", "Combustion", prov)
    _insert_stage(writer, "main_01-04", "1-4", "Filler", prov)
    build_search_index(writer)
    writer.commit()
    writer.close()
    with open_read_only(path) as conn:
        rows = _handler(conn)(query="4-4").to_dict()["data"]["results"]
    assert rows[0]["stage_code"] == "4-4"  # type: ignore[index]
    assert rows[0]["game_id"] == "main_04-04"  # type: ignore[index]


def test_exact_code_match_is_case_insensitive(tmp_path: Path) -> None:
    # An uppercase-lettered code ("GT-1") matches the query regardless of case,
    # and still ranks ahead of a token-only sibling.
    path = tmp_path / "gt.sqlite"
    writer = build_database(path)
    prov = _seed_provenance(writer)
    _insert_stage(writer, "act_gt_10", "GT-10", "Filler", prov)
    _insert_stage(writer, "act_gt_1", "GT-1", "Target", prov)
    build_search_index(writer)
    writer.commit()
    writer.close()
    with open_read_only(path) as conn:
        rows = _handler(conn)(query="gt-1").to_dict()["data"]["results"]
    assert rows[0]["stage_code"] == "GT-1"  # type: ignore[index]


# --- §V70/B59: variant stages distinguishable by the difficulty locator tag ----


def test_v70_variant_stages_distinguishable_by_difficulty(tmp_path: Path) -> None:
    # §V70/B59: a normal stage and its challenge variant share display_name +
    # stage_code and differ only by the game-data "#f#" game_id suffix. The
    # locator carries a `difficulty` variant tag (the same value get_stage
    # returns), so a client can tell the two apart in one result set without
    # parsing that jargon suffix -- no two indistinguishable locators.
    path = tmp_path / "variants.sqlite"
    writer = build_database(path)
    prov = _seed_provenance(writer)
    _insert_stage(writer, "main_04-04", "4-4", "Combustion", prov, difficulty="NORMAL")
    _insert_stage(writer, "main_04-04#f#", "4-4", "Combustion", prov, difficulty="FOUR_STAR")
    build_search_index(writer)
    writer.commit()
    writer.close()
    with open_read_only(path) as conn:
        rows = _handler(conn)(query="4-4").to_dict()["data"]["results"]  # type: ignore[index]

    by_game_id = {row["game_id"]: row for row in rows}
    # both variants surface, and they collide on display_name + stage_code (B59) ...
    assert set(by_game_id) == {"main_04-04", "main_04-04#f#"}
    assert {r["display_name"] for r in rows} == {"Combustion"}
    assert {r["stage_code"] for r in rows} == {"4-4"}
    # ... but the difficulty variant tag makes them distinguishable, carrying the
    # raw stage difficulty (the get_stage.difficulty domain).
    assert by_game_id["main_04-04"]["difficulty"] == "NORMAL"
    assert by_game_id["main_04-04#f#"]["difficulty"] == "FOUR_STAR"
    # §V70: no two locators in one result set are indistinguishable -- some field
    # beyond the raw game_id separates the pair.
    assert by_game_id["main_04-04"]["difficulty"] != by_game_id["main_04-04#f#"]["difficulty"]


def test_stage_without_difficulty_carries_null_tag(tmp_path: Path) -> None:
    # §V21 additive: a stage with no difficulty in source keeps the key present
    # with a null value (the outer join never drops the row).
    path = tmp_path / "nodiff.sqlite"
    writer = build_database(path)
    prov = _seed_provenance(writer)
    _insert_stage(writer, "main_04-04", "4-4", "Combustion", prov)  # difficulty omitted
    build_search_index(writer)
    writer.commit()
    writer.close()
    with open_read_only(path) as conn:
        rows = _handler(conn)(query="4-4").to_dict()["data"]["results"]  # type: ignore[index]
    assert rows[0]["difficulty"] is None
    assert "difficulty" in rows[0]


# --- §V23 typed envelope: not_found -------------------------------------------


def test_not_found_envelope(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(query="zzzznotastage")
    assert env.status == "not_found"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert data["message"] == "no indexed stage matched the search query"
    assert "suggested_action" in data
    # §V24: a not_found never suggests a query-time download/scrape.
    assert "download" not in data["suggested_action"].lower()  # type: ignore[union-attr]


def test_metacharacter_only_query_is_not_found(conn: sqlite3.Connection) -> None:
    # A query of only FTS metacharacters holds no word token -> nothing to search.
    assert _handler(conn)(query="*:^()").status == "not_found"


# --- §V19: bounded window -----------------------------------------------------


def test_out_of_range_limit_rejected_at_gate(conn: sqlite3.Connection) -> None:
    # §V19: the model gate *rejects* an out-of-range limit; the tool never runs a
    # silently widened/narrowed search. Mirrors the service-level rejection.
    handler = _handler(conn)
    for bad in (0, -1, MAX_LIMIT + 1, 100):
        with pytest.raises(ValidationError):
            handler(query="4-4", limit=bad)


def test_unknown_parameter_rejected(conn: sqlite3.Connection) -> None:
    # §V18: extra="forbid" -> a crafted request cannot smuggle an unknown field.
    with pytest.raises(ValidationError):
        _handler(conn)(query="4-4", entity_type="stage")


def test_limit_bound_honored_through_tool(tmp_path: Path) -> None:
    # §V19: even asking for the max, the tool returns at most MAX_LIMIT rows, and
    # the default caps at 10 -- no bulk dump escapes the bound end to end.
    path = tmp_path / "many.sqlite"
    writer = build_database(path)
    prov = _seed_provenance(writer)
    for i in range(MAX_LIMIT + 10):
        _insert_stage(writer, f"main_07-{i:03d}", f"7-{i}", "Sarkaz Outpost", prov)
    build_search_index(writer)
    writer.commit()
    writer.close()
    with open_read_only(path) as conn:
        handler = _handler(conn)
        assert handler(query="Sarkaz", limit=MAX_LIMIT).to_dict()["data"]["count"] == MAX_LIMIT
        assert handler(query="Sarkaz").to_dict()["data"]["count"] == 10


# --- §V23 fail-closed failures ------------------------------------------------


def test_database_unavailable_envelope() -> None:
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: cand.sqlite")

    env = build_search_stages_spec(boom).handler(query="4-4")
    assert env.status == "database_unavailable"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    # §V23: no local path / file name leaks into the client-facing message.
    assert data["message"] == "the active database is unavailable"
    assert "cand.sqlite" not in str(data)


def test_unexpected_error_fails_closed_to_internal_error() -> None:
    def boom() -> sqlite3.Connection:
        raise RuntimeError("secret path /home/ubuntu/db.sqlite blew up")

    env = build_search_stages_spec(boom).handler(query="4-4")
    assert env.status == "internal_error"
    # §V23: the fixed message carries no exception text / stack trace / local path.
    assert str(env.to_dict()["data"]).find("/home/ubuntu") == -1
    assert "blew up" not in str(env.to_dict()["data"])


# --- §I.tool / §V14 wire contract ---------------------------------------------


def test_spec_registers_read_only_with_bounded_schema(conn: sqlite3.Connection) -> None:
    reg = ToolRegistry()
    spec = reg.register(build_search_stages_spec(lambda: conn))
    assert reg.names() == ("search_stages",)
    assert spec.read_only is True
    tool = spec.to_mcp_tool()
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True
    # The bounded model's §V19 limit + §V18 caps ride the wire in inputSchema.
    assert tool.inputSchema["properties"]["limit"]["maximum"] == MAX_LIMIT
    assert tool.inputSchema["additionalProperties"] is False
