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
from arknights_mcp.services.search import MAX_LIMIT, search_entities, search_stages
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


# --- §V50/§V24 region availability gate (B42) ---------------------------------


def test_cn_without_snapshot_is_data_stale_not_not_found(conn: sqlite3.Connection) -> None:
    # §V50/§V24: this build has an en snapshot only. A cn-scoped search must honor
    # region availability BEFORE asserting absence: no cn snapshot -> ``data_stale``,
    # never a bare ``not_found`` (which would wrongly claim the entity absent on cn).
    entities = search_entities(conn, query="drone", server="cn")
    assert entities.status == "data_stale"
    assert entities.hits == ()
    stages = search_stages(conn, query="4-4", server="cn")
    assert stages.status == "data_stale"
    assert stages.hits == ()


def test_unsupported_region_is_unsupported_server(conn: sqlite3.Connection) -> None:
    # §V50/§V5: a region outside {en, cn} is ``unsupported_server`` -- not a
    # ``not_found`` and not a silent empty result. The service enforces this even
    # though the MCP input model also rejects a non-Region ``server`` (§V14 depth).
    assert search_entities(conn, query="drone", server="jp").status == "unsupported_server"
    assert search_stages(conn, query="4-4", server="jp").status == "unsupported_server"


def test_supported_region_with_snapshot_still_asserts_absence(conn: sqlite3.Connection) -> None:
    # §V50: ``not_found`` is legitimate once the region index is confirmed present --
    # an en snapshot exists, so a genuinely-absent en entity is ``not_found``.
    result = search_entities(conn, query="zzzznotanentity", server="en")
    assert result.status == "not_found"


def test_empty_index_unscoped_search_is_data_stale(tmp_path: Path) -> None:
    # §V50: an unscoped search against a build with NO active snapshot at all is
    # ``data_stale`` (the whole index is empty) -- not a bare ``not_found``.
    path = tmp_path / "empty.sqlite"
    writer = build_database(path)
    build_search_index(writer)
    writer.commit()
    writer.close()
    with open_read_only(path) as empty:
        assert search_entities(empty, query="drone").status == "data_stale"
        assert search_stages(empty, query="4-4").status == "data_stale"


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
        # Ask for the max window and get exactly it, even though more rows match.
        assert len(search_entities(conn, query="sarkaz", limit=MAX_LIMIT).hits) == MAX_LIMIT
        # Default caps at 10 without an explicit limit.
        assert len(search_entities(conn, query="sarkaz").hits) == 10


def test_out_of_range_limit_rejected(conn: sqlite3.Connection) -> None:
    # §V19: the service *rejects* an out-of-range limit rather than silently
    # clamping -- the same contract SearchEntitiesInput enforces at the MCP gate,
    # so a caller reaching the service directly gets no silent widening/narrowing.
    for bad in (0, -1, MAX_LIMIT + 1, 100):
        with pytest.raises(ValueError):
            search_entities(conn, query="drone", limit=bad)


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


# --- §T142 / §V73: item domain in the shared FTS index (B67) -------------------


def _seed_item(tmp_path: Path) -> Path:
    """Build a DB carrying one synthetic item row + its FTS document (§V37 home)."""
    path = tmp_path / "item.sqlite"
    writer = build_database(path)
    provenance_id = _seed_provenance(writer)
    writer.execute(
        "INSERT INTO items (server, game_id, display_name, provenance_id) VALUES (?,?,?,?)",
        ("en", "30073", "Loxic Kohl", provenance_id),
    )
    build_search_index(writer)
    writer.commit()
    writer.close()
    return path


def test_item_searchable_by_name_and_game_id(tmp_path: Path) -> None:
    # §V73/B67: an item is resolvable by name -> game_id so get_item_drops has a real
    # name->id path (the FTS locator's game_id is exactly items.game_id).
    with open_read_only(_seed_item(tmp_path)) as conn:
        by_name = search_entities(conn, query="Loxic").hits
        hit = next(h for h in by_name if h.entity_type == "item")
        assert hit.game_id == "30073"
        assert hit.server == "en"
        assert any(h.game_id == "30073" for h in search_entities(conn, query="30073").hits)


def test_item_entity_type_filter(tmp_path: Path) -> None:
    # §V73: the item domain narrows via entity_type, like the other domains.
    with open_read_only(_seed_item(tmp_path)) as conn:
        assert search_entities(conn, query="Loxic", entity_type="item").hits
        assert search_entities(conn, query="Loxic", entity_type="enemy").hits == ()


def test_item_locator_feeds_get_item_drops(tmp_path: Path) -> None:
    # §V73/B67: the item locator's game_id is the key get_item_drops resolves items by
    # ((server, game_id)), so a search hit is a live name->id bridge, not a dead end.
    from arknights_mcp.db.repositories.drops import DropRepository

    with open_read_only(_seed_item(tmp_path)) as conn:
        hit = next(h for h in search_entities(conn, query="Loxic").hits if h.entity_type == "item")
        resolved = DropRepository(conn).item_by_game_id(hit.server, hit.game_id)
        assert resolved is not None
