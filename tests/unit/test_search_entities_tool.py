"""``search_entities`` tool tests.

The tool is the model -> service -> envelope bridge; these drive it end-to-end
against the same production read-only path the service tests use, asserting
the typed envelope shape, the bounded window (rejected at the model gate + honored
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
from arknights_mcp.mcp.tools._shared import (
    SEARCH_COVERAGE_ENTRIES,
    SEARCH_COVERAGE_POINTER,
)
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


# --- typed envelope: ok result ------------------------------------------------


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
    # Region travels per row; the locator carries its typed identity. A
    # non-stage locator (enemy Slug) omits the stage-only stage_code/difficulty
    # keys rather than emitting them as an ambiguous null.
    for row in _handler(conn)(query="slug").to_dict()["data"]["results"]:  # type: ignore[index]
        assert row["server"] == "en"
        assert set(row) == {"entity_type", "server", "game_id", "display_name"}


def test_non_stage_locator_omits_stage_code_and_difficulty(
    conn: sqlite3.Connection,
) -> None:
    # stage_code + difficulty are stage-only; an enemy locator carries
    # neither key (not a bare null). zone_display_name joins that stage-only
    # set -- an enemy belongs to no zone, so the key must not appear on it either.
    rows = _handler(conn)(query="drone", entity_type="enemy").to_dict()["data"]["results"]
    assert rows  # sanity: the enemy is indexed
    for row in rows:  # type: ignore[union-attr]
        assert "stage_code" not in row
        assert "difficulty" not in row
        assert "zone_display_name" not in row


def test_stage_locator_keeps_stage_code_and_difficulty(
    conn: sqlite3.Connection,
) -> None:
    # A stage locator DOES carry the stage-only keys (positive case).
    rows = _handler(conn)(query="4-4", entity_type="stage").to_dict()["data"]["results"]
    stage = next(r for r in rows if r["game_id"] == "main_04-04")  # type: ignore[index,union-attr]
    assert stage["stage_code"] == "4-4"
    assert "difficulty" in stage


def test_zone_matched_locator_names_the_zone(conn: sqlite3.Connection) -> None:
    # A zone-name query returns stages whose own names say nothing about
    # it. Without the zone on the wire the hit is unattributable -- the client cannot
    # tell why the stage came back, nor group mixed results by zone. The 4-4 fixture
    # stage sits in "Chapter 4", so that query reaches it only through the alias.
    rows = _handler(conn)(query="Chapter", entity_type="stage").to_dict()["data"]["results"]
    stage = next(r for r in rows if r["game_id"] == "main_04-04")  # type: ignore[index,union-attr]
    assert stage["zone_display_name"] == "Chapter 4"
    assert "Chapter" not in (stage["display_name"] or "")


def test_server_filter_scopes_region(conn: sqlite3.Connection) -> None:
    # The en Slug is not surfaced under a cn-scoped search.
    assert _handler(conn)(query="slug", server="en").status == "ok"
    # No active snapshot for cn in this en-only build, so a
    # cn-scoped search is ``data_stale`` -- never a bare ``not_found`` that would
    # wrongly claim the entity is absent from cn.
    assert _handler(conn)(query="slug", server="cn").status == "data_stale"


def test_entity_type_filter(conn: sqlite3.Connection) -> None:
    assert _handler(conn)(query="drone", entity_type="enemy").status == "ok"
    filtered_out = _handler(conn)(query="drone", entity_type="stage")
    # The filter excluded everything, which is an empty ANSWER, not a failure.
    assert filtered_out.status == "ok"
    assert filtered_out.to_dict()["data"] == {"query": "drone", "count": 0, "results": []}


# --- typed envelope: an empty set is a delivered ``ok`` -----------------------


def test_empty_result_is_ok_with_an_empty_list_and_a_reason(conn: sqlite3.Connection) -> None:
    # This used to be ``not_found``, so a client branching on status read "no name
    # matched" as a failed request while reading get_announcements' empty window as a
    # success. The reason + retry guidance MOVED from the error body to the limitation;
    # nothing a client could read before was dropped.
    env = _handler(conn)(query="zzzznotanentity")
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert data == {"query": "zzzznotanentity", "count": 0, "results": []}
    assert any("No indexed entity matched" in lim for lim in env.limitations)
    # An empty answer never suggests a query-time download/scrape either.
    assert all("download" not in lim.lower() for lim in env.limitations)


def test_metacharacter_only_query_reports_its_own_empty_reason(conn: sqlite3.Connection) -> None:
    # A query of only FTS metacharacters holds no word token -> nothing to search. It is
    # still an ``ok``, but with a DIFFERENT sentence: telling the client its
    # query matched nothing would claim a search ran that never did.
    env = _handler(conn)(query="*:^()")
    assert env.status == "ok"
    assert any("no letters or digits" in lim for lim in env.limitations)
    assert all("No indexed entity matched" not in lim for lim in env.limitations)


# --- region availability gate -------------------------------------------------


def test_region_without_snapshot_is_data_stale_envelope(conn: sqlite3.Connection) -> None:
    # No active snapshot for cn in this en-only build. A cn search
    # is ``data_stale`` with a suggested admin action -- never a bare ``not_found``.
    env = _handler(conn)(query="drone", server="cn")
    assert env.status == "data_stale"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert data["message"] == "no active snapshot for the requested region in the active build"
    # The suggested action is an admin sync/import, never a query-time download.
    action = data["suggested_action"]
    assert isinstance(action, str)
    assert "arknights-mcp sync" in action
    assert "download" not in action.lower()


# --- the extra-locale (ja/ko) NAME-alias filter is RETIRED ---------------------


def test_locale_param_rejected_at_gate(conn: sqlite3.Connection) -> None:
    # The `locale` filter is gone (founder 2026-07-23, EN+CN only).
    # The bounded input model is `extra="forbid"`, so a client still sending
    # `locale` is rejected at the model gate (a protocol-level ValidationError),
    # never silently accepted or ignored.
    with pytest.raises(ValidationError):
        _handler(conn)(query="drone", locale="ja")


# --- bounded window -----------------------------------------------------------


def test_out_of_range_limit_rejected_at_gate(conn: sqlite3.Connection) -> None:
    # The model gate *rejects* an out-of-range limit; the tool never runs a
    # silently widened/narrowed search. Mirrors the service-level rejection.
    handler = _handler(conn)
    for bad in (0, -1, MAX_LIMIT + 1, 100):
        with pytest.raises(ValidationError):
            handler(query="drone", limit=bad)


def test_unknown_parameter_rejected(conn: sqlite3.Connection) -> None:
    # extra="forbid" -> a crafted request cannot smuggle an unknown field.
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
    # Even asking for the max, the tool returns at most MAX_LIMIT rows, and
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


# --- fail-closed failures -----------------------------------------------------


def test_database_unavailable_envelope() -> None:
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: cand.sqlite")

    env = build_search_entities_spec(boom).handler(query="drone")
    assert env.status == "database_unavailable"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    # No local path / file name leaks into the client-facing message.
    assert data["message"] == "the active database is unavailable"
    assert "cand.sqlite" not in str(data)


def test_unexpected_error_fails_closed_to_internal_error() -> None:
    def boom() -> sqlite3.Connection:
        raise RuntimeError("secret path /home/ubuntu/db.sqlite blew up")

    env = build_search_entities_spec(boom).handler(query="drone")
    assert env.status == "internal_error"
    # The fixed message carries no exception text / stack trace / local path.
    assert str(env.to_dict()["data"]).find("/home/ubuntu") == -1
    assert "blew up" not in str(env.to_dict()["data"])


# --- wire contract ------------------------------------------------------------


def test_spec_registers_read_only_with_bounded_schema(conn: sqlite3.Connection) -> None:
    reg = ToolRegistry()
    spec = reg.register(build_search_entities_spec(lambda: conn))
    assert reg.names() == ("search_entities",)
    assert spec.read_only is True
    tool = spec.to_mcp_tool()
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True
    # The bounded model's limit + caps ride the wire in inputSchema.
    assert tool.inputSchema["properties"]["limit"]["maximum"] == MAX_LIMIT
    assert tool.inputSchema["additionalProperties"] is False


# --- search coverage docs -----------------------------------------------------


def test_description_states_coverage_and_region_order(conn: sqlite3.Connection) -> None:
    # Coverage limits + region order are client contract, stated where the
    # client reads them, in this sibling too.
    desc = build_search_entities_spec(lambda: conn).description
    assert "English and Chinese only" in desc
    assert "Japanese or Korean" in desc
    assert "fuzzy" in desc
    # Zone names now ride stage documents as aliases -- the description states
    # the coverage instead of the retired "not indexed" caveat.
    assert "A zone name (for example Gavial's Footprints)" in desc
    assert "matches the stages in that zone" in desc
    # The client is told alias-driven stages rank below own-name matches,
    # and that such a hit is attributable via zone_display_name.
    # The alias RANKING rule and the zone_display_name
    # attribution are still client contract, but their home is now the coverage-guide
    # resource this description points at -- byte-identical in both siblings, they were
    # half of the 788-char block that must not be duplicated across two descriptions.
    guide = dict(SEARCH_COVERAGE_ENTRIES)
    assert (
        "listed after every entity that matched on its own name" in guide["zone_and_event_ranking"]
    )
    assert "zone_display_name" in guide["zone_and_event_attribution"]
    assert SEARCH_COVERAGE_POINTER in desc
    assert "en results are listed before cn" in desc
    assert "pass server" in desc
    # The exact-stage-code ranking divergence cross-ref stays.
    assert "search_stages" in desc


def test_ja_query_empty_limitation_states_encn_only(conn: sqlite3.Connection) -> None:
    # A Japanese-name query comes back empty; the guidance must say names are indexed
    # in English and Chinese only, not just "check the spelling". The text moved
    # from the not_found suggested_action to the ``ok`` limitation; the requirement on it
    # is unchanged -- a ja query must still learn WHY it can never match.
    env = _handler(conn)(query="シルバーアッシュ")
    assert env.status == "ok"
    assert any("English and Chinese only" in lim for lim in env.limitations)
