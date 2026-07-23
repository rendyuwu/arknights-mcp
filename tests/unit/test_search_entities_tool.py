"""§T32 ``search_entities`` tool tests (§V19/§V23; §I.tool).

The tool is the model -> service -> envelope bridge; these drive it end-to-end
against the same production read-only path the service tests use (§V2), asserting
the typed §V23 envelope shape, the §V19 bound (rejected at the model gate + honored
through the tool), and that failures fail closed to a safe envelope with no leaked
detail.
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
from arknights_mcp.mcp.tools.search import build_search_entities_spec
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
    return build_search_entities_spec(lambda: conn).handler


# --- §V23 typed envelope: ok result -------------------------------------------


def test_ok_envelope_shape(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(query="drone")
    assert env.status == "ok"
    assert env.schema_version == SCHEMA_VERSION
    body = env.to_dict()["data"]
    assert isinstance(body, dict)
    assert body["query"] == "drone"
    assert body["count"] == len(body["results"])  # type: ignore[arg-type]
    assert any(
        row["game_id"] == "enemy_1105_drone" and row["server"] == "en"
        for row in body["results"]  # type: ignore[union-attr]
    )


def test_results_carry_region_and_type(conn: sqlite3.Connection) -> None:
    # §V5 region travels per row; the locator carries its typed identity. The
    # §V70 difficulty variant tag is always keyed (null for a non-stage hit).
    for row in _handler(conn)(query="slug").to_dict()["data"]["results"]:  # type: ignore[index]
        assert row["server"] == "en"
        assert set(row) == {
            "entity_type",
            "server",
            "game_id",
            "display_name",
            "stage_code",
            "difficulty",
        }


def test_server_filter_scopes_region(conn: sqlite3.Connection) -> None:
    # §V5: the en Slug is not surfaced under a cn-scoped search.
    assert _handler(conn)(query="slug", server="en").status == "ok"
    # §V50/§V24 (B42): cn has no active snapshot in this en-only build, so a
    # cn-scoped search is ``data_stale`` -- never a bare ``not_found`` that would
    # wrongly claim the entity is absent from cn.
    assert _handler(conn)(query="slug", server="cn").status == "data_stale"


def test_entity_type_filter(conn: sqlite3.Connection) -> None:
    assert _handler(conn)(query="drone", entity_type="enemy").status == "ok"
    assert _handler(conn)(query="drone", entity_type="stage").status == "not_found"


# --- §V23 typed envelope: not_found -------------------------------------------


def test_not_found_envelope(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(query="zzzznotanentity")
    assert env.status == "not_found"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert data["message"] == "no indexed entity matched the search query"
    assert "suggested_action" in data
    # §V24: a not_found never suggests a query-time download/scrape.
    assert "download" not in data["suggested_action"].lower()  # type: ignore[union-attr]


def test_metacharacter_only_query_is_not_found(conn: sqlite3.Connection) -> None:
    # A query of only FTS metacharacters holds no word token -> nothing to search.
    # §V50: the region index is present (en snapshot), so absence is assertable.
    assert _handler(conn)(query="*:^()").status == "not_found"


# --- §V50/§V24 region availability gate (B42) ---------------------------------


def test_region_without_snapshot_is_data_stale_envelope(conn: sqlite3.Connection) -> None:
    # §V50/§V24 (B42): cn has no active snapshot in this en-only build. A cn search
    # is ``data_stale`` with a suggested admin action -- never a bare ``not_found``.
    env = _handler(conn)(query="drone", server="cn")
    assert env.status == "data_stale"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert data["message"] == "no active snapshot for the requested region in the active build"
    # §V24: the suggested action is an admin sync/import, never a query-time download.
    action = data["suggested_action"]
    assert isinstance(action, str)
    assert "arknights-mcp sync" in action
    assert "download" not in action.lower()


# --- §V50/§V57 locale-availability gate (B66) ---------------------------------


def test_locale_without_alias_data_is_locale_specific_data_stale(conn: sqlite3.Connection) -> None:
    # §V50/§V57 (B66): this en-only fixture build imported NO extra-locale aliases, so
    # a locale=ja search must not be a bare ``not_found`` ("check the spelling"). It is
    # a ``data_stale`` envelope carrying the locale-specific message so a client can
    # tell "alias data never imported" from "no such alias", with an admin sync action.
    env = _handler(conn)(query="drone", locale="ja")
    assert env.status == "data_stale"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert data["message"] == "locale aliases not imported in this build"
    action = data["suggested_action"]
    assert isinstance(action, str)
    assert "arknights-mcp sync" in action
    # §V24: the suggested action is an admin step, never a query-time download.
    assert "download" not in action.lower()
    # §V71: no internal spec cite / jargon leaks into client-facing text.
    assert "§" not in str(data)


# --- §V57/§V73 locale-not-applicable gate (B77) -------------------------------


def test_locale_on_item_type_is_invalid_input(conn: sqlite3.Connection) -> None:
    # §V57/§V73 (B77): items carry no locale-alias table, so a locale filter scoped to
    # entity_type=item can never match. The verdict is ``invalid_input`` (a client
    # mistake -- drop the filter or pick operator/enemy), NOT a bare ``not_found`` that
    # would wrongly imply the item is absent. The global operator ja aliases (if any)
    # must not let this pass the gate.
    env = _handler(conn)(query="orirock", entity_type="item", locale="ja")
    assert env.status == "invalid_input"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert data["message"] == "the locale filter applies only to operator and enemy searches"
    action = data["suggested_action"]
    assert isinstance(action, str)
    assert "locale" in action
    # §V71: no internal spec cite / jargon leaks into client-facing text.
    assert "§" not in str(data)


def test_locale_on_stage_type_is_invalid_input(conn: sqlite3.Connection) -> None:
    # §V57/§V73 (B77): stages likewise have no locale-alias table. A locale filter
    # explicitly scoped to entity_type=stage is inapplicable, not a not_found.
    env = _handler(conn)(query="4-4", entity_type="stage", locale="ja")
    assert env.status == "invalid_input"


# --- §V19: bounded window -----------------------------------------------------


def test_out_of_range_limit_rejected_at_gate(conn: sqlite3.Connection) -> None:
    # §V19: the model gate *rejects* an out-of-range limit; the tool never runs a
    # silently widened/narrowed search. Mirrors the service-level rejection.
    handler = _handler(conn)
    for bad in (0, -1, MAX_LIMIT + 1, 100):
        with pytest.raises(ValidationError):
            handler(query="drone", limit=bad)


def test_unknown_parameter_rejected(conn: sqlite3.Connection) -> None:
    # §V18: extra="forbid" -> a crafted request cannot smuggle an unknown field.
    with pytest.raises(ValidationError):
        _handler(conn)(query="drone", limitt=5)


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


def test_limit_bound_honored_through_tool(tmp_path: Path) -> None:
    # §V19: even asking for the max, the tool returns at most MAX_LIMIT rows, and
    # the default caps at 10 -- no bulk dump escapes the bound end-to-end.
    path = tmp_path / "many.sqlite"
    writer = build_database(path)
    provenance_id = _seed_provenance(writer)
    for i in range(MAX_LIMIT + 10):
        writer.execute(
            "INSERT INTO enemies (server, game_id, display_name, provenance_id) VALUES (?,?,?,?)",
            ("en", f"enemy_{i:04d}_sarkaz", "Sarkaz Trooper", provenance_id),
        )
    build_search_index(writer)
    writer.commit()
    writer.close()
    with open_read_only(path) as conn:
        handler = _handler(conn)
        assert handler(query="sarkaz", limit=MAX_LIMIT).to_dict()["data"]["count"] == MAX_LIMIT
        assert handler(query="sarkaz").to_dict()["data"]["count"] == 10


# --- §V23 fail-closed failures ------------------------------------------------


def test_database_unavailable_envelope() -> None:
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: cand.sqlite")

    env = build_search_entities_spec(boom).handler(query="drone")
    assert env.status == "database_unavailable"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    # §V23: no local path / file name leaks into the client-facing message.
    assert data["message"] == "the active database is unavailable"
    assert "cand.sqlite" not in str(data)


def test_unexpected_error_fails_closed_to_internal_error() -> None:
    def boom() -> sqlite3.Connection:
        raise RuntimeError("secret path /home/ubuntu/db.sqlite blew up")

    env = build_search_entities_spec(boom).handler(query="drone")
    assert env.status == "internal_error"
    # §V23: the fixed message carries no exception text / stack trace / local path.
    assert str(env.to_dict()["data"]).find("/home/ubuntu") == -1
    assert "blew up" not in str(env.to_dict()["data"])


# --- §I.tool / §V14 wire contract ---------------------------------------------


def test_spec_registers_read_only_with_bounded_schema(conn: sqlite3.Connection) -> None:
    reg = ToolRegistry()
    spec = reg.register(build_search_entities_spec(lambda: conn))
    assert reg.names() == ("search_entities",)
    assert spec.read_only is True
    tool = spec.to_mcp_tool()
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True
    # The bounded model's §V19 limit + §V18 caps ride the wire in inputSchema.
    assert tool.inputSchema["properties"]["limit"]["maximum"] == MAX_LIMIT
    assert tool.inputSchema["additionalProperties"] is False
