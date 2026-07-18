"""T31: the FTS5 entity index + the ``search_entities`` service (§V2, §V5, §V19).

Two DB shapes are exercised, both through the production read-only path (§V2):

* the pinned 4-4 fixture built via :func:`build_candidate` (which populates the
  index in-pipeline) -- covers name / game_id / stage_code search, region
  scoping (§V5), entity-type narrowing, and FTS/SQL metacharacter safety;
* a synthetic build -- covers the §V19 result bound and the operator ``tags`` /
  ``aliases`` indexed columns that no importer populates yet (M4).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.importers.search_index import build_search_index
from arknights_mcp.services.search import MAX_LIMIT, search_entities
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


# --- name / id / code search --------------------------------------------------


def test_search_by_display_name(conn: sqlite3.Connection) -> None:
    hits = search_entities(conn, query="drone").hits
    assert any(h.entity_type == "enemy" and h.game_id == "enemy_1105_drone" for h in hits)


def test_search_prefix_matches(conn: sqlite3.Connection) -> None:
    # "origin" is a prefix of "Originium" (Slug) -- prefix MATCH finds it.
    hits = search_entities(conn, query="origin").hits
    assert any(h.game_id == "enemy_1007_slime" for h in hits)


def test_search_by_game_id(conn: sqlite3.Connection) -> None:
    hits = search_entities(conn, query="enemy_1007_slime").hits
    assert any(h.game_id == "enemy_1007_slime" for h in hits)


def test_search_by_stage_code(conn: sqlite3.Connection) -> None:
    hits = search_entities(conn, query="4-4").hits
    stage = next(h for h in hits if h.entity_type == "stage")
    assert stage.game_id == "main_04-04"
    assert stage.stage_code == "4-4"


def test_no_match_is_not_found(conn: sqlite3.Connection) -> None:
    result = search_entities(conn, query="zzzznotanentity")
    assert result.status == "not_found"
    assert result.hits == ()


# --- region scoping (§V5) -----------------------------------------------------


def test_hits_carry_region(conn: sqlite3.Connection) -> None:
    for hit in search_entities(conn, query="slug").hits:
        assert hit.server == "en"


def test_server_filter_scopes_region(conn: sqlite3.Connection) -> None:
    # §V5: the en Slug is not surfaced under a cn-scoped search.
    assert search_entities(conn, query="slug", server="en").hits
    assert search_entities(conn, query="slug", server="cn").hits == ()


# --- entity-type narrowing ----------------------------------------------------


def test_entity_type_filter(conn: sqlite3.Connection) -> None:
    assert search_entities(conn, query="drone", entity_type="enemy").hits
    assert search_entities(conn, query="drone", entity_type="stage").hits == ()


# --- §V2 / §V18 query safety --------------------------------------------------


def test_query_metacharacters_are_safe(conn: sqlite3.Connection) -> None:
    # A query of only FTS metacharacters holds no word token -> nothing to search.
    assert search_entities(conn, query="*:^()").status == "not_found"
    # A stray FTS operator / paren is stripped; the real token still matches and
    # the MATCH never sees an injected operator or syntax error (§V2/§V18).
    assert any(h.game_id == "enemy_1105_drone" for h in search_entities(conn, query="drone)").hits)
    # Raw quotes / a NEAR keyword are parsed as literal tokens, never operators.
    search_entities(conn, query='" NEAR drone')  # must not raise


def test_search_is_read_only(conn: sqlite3.Connection) -> None:
    # §V2: the service only reads -- no writes recorded on the connection.
    before = conn.total_changes
    search_entities(conn, query="drone")
    assert conn.total_changes == before


# --- §V19 result bound + operator tags/aliases --------------------------------


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


def test_result_bounded_to_v19_max(tmp_path: Path) -> None:
    # §V19: search returns at most MAX_LIMIT even when more rows match.
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
        assert len(search_entities(conn, query="sarkaz", limit=100).hits) == MAX_LIMIT
        # Default caps at 10 without an explicit limit.
        assert len(search_entities(conn, query="sarkaz").hits) == 10


def test_operator_tags_and_aliases_indexed(tmp_path: Path) -> None:
    # The operator importer lands in M4; the index builder already covers the
    # tags + aliases columns (§T31) -- verify against a synthetic operator row.
    path = tmp_path / "op.sqlite"
    writer = build_database(path)
    provenance_id = _seed_provenance(writer)
    cur = writer.execute(
        "INSERT INTO operators (server, game_id, display_name, tag_json, provenance_id) "
        "VALUES (?,?,?,?,?)",
        ("en", "char_002_amiya", "Amiya", '["Caster", "DPS"]', provenance_id),
    )
    operator_pk = int(cur.lastrowid)
    writer.execute(
        "INSERT INTO operator_aliases (operator_pk, alias) VALUES (?,?)",
        (operator_pk, "Rhodes Island Leader"),
    )
    build_search_index(writer)
    writer.commit()
    writer.close()
    with open_read_only(path) as conn:
        by_tag = search_entities(conn, query="Caster").hits
        by_alias = search_entities(conn, query="Rhodes").hits
        assert any(h.game_id == "char_002_amiya" for h in by_tag)
        assert any(h.game_id == "char_002_amiya" for h in by_alias)
