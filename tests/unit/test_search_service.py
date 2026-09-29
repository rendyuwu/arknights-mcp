"""The FTS5 entity index + the ``search_entities`` service.

Two DB shapes are exercised, both through the production read-only path:

* the pinned 4-4 fixture built via :func:`build_candidate` (which populates the
  index in-pipeline) -- covers name / game_id / stage_code search, region
  scoping, entity-type narrowing, and FTS/SQL metacharacter safety;
* a synthetic build -- covers the result bound and the operator ``tags`` /
  ``aliases`` indexed columns that no importer populates yet (M4).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.db.migrations import build_database
from arknights_mcp.db.repositories.search import SearchRepository
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


def test_no_match_is_an_empty_ok_with_a_reason(conn: sqlite3.Connection) -> None:
    # A search is a SET query, so zero hits is a delivered empty answer,
    # not a failed request. The typed reason is what lets the tool say WHY without the
    # domain owning any client-facing wording.
    result = search_entities(conn, query="zzzznotanentity")
    assert result.status == "ok"
    assert result.hits == ()
    assert result.empty_reason == "no_match"


# --- region scoping -----------------------------------------------------------


def test_hits_carry_region(conn: sqlite3.Connection) -> None:
    for hit in search_entities(conn, query="slug").hits:
        assert hit.server == "en"


def test_server_filter_scopes_region(conn: sqlite3.Connection) -> None:
    # The en Slug is not surfaced under a cn-scoped search.
    assert search_entities(conn, query="slug", server="en").hits
    assert search_entities(conn, query="slug", server="cn").hits == ()


# --- region availability gate -------------------------------------------------


def test_cn_without_snapshot_is_data_stale_not_not_found(conn: sqlite3.Connection) -> None:
    # This build has an en snapshot only. A cn-scoped search must honor
    # region availability BEFORE asserting absence: no cn snapshot -> ``data_stale``,
    # never a bare ``not_found`` (which would wrongly claim the entity absent on cn).
    entities = search_entities(conn, query="drone", server="cn")
    assert entities.status == "data_stale"
    assert entities.hits == ()
    stages = search_stages(conn, query="4-4", server="cn")
    assert stages.status == "data_stale"
    assert stages.hits == ()


def test_unsupported_region_is_unsupported_server(conn: sqlite3.Connection) -> None:
    # A region outside {en, cn} is ``unsupported_server`` -- not a
    # ``not_found`` and not a silent empty result. The service enforces this even
    # though the MCP input model also rejects a non-Region ``server``.
    assert search_entities(conn, query="drone", server="jp").status == "unsupported_server"
    assert search_stages(conn, query="4-4", server="jp").status == "unsupported_server"


def test_supported_region_with_snapshot_reports_a_real_absence(conn: sqlite3.Connection) -> None:
    # Once the region index is confirmed present, absence is a real answer --
    # and that answer is ``ok`` + ``no_match``, never a ``data_stale`` gate.
    # The distinction is the point: "this region has no data" and "this region has
    # data and none of it matched" must not arrive as the same status.
    result = search_entities(conn, query="zzzznotanentity", server="en")
    assert result.status == "ok"
    assert result.empty_reason == "no_match"


def test_empty_index_unscoped_search_is_data_stale(tmp_path: Path) -> None:
    # An unscoped search against a build with NO active snapshot at all is
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


# --- query safety -------------------------------------------------------------


def test_query_metacharacters_are_safe(conn: sqlite3.Connection) -> None:
    # A query of only FTS metacharacters holds no word token -> nothing to search. That
    # is its OWN empty case: the query never ran, so it must not be reported as
    # a query that ran and matched nothing.
    metacharacters = search_entities(conn, query="*:^()")
    assert metacharacters.status == "ok"
    assert metacharacters.empty_reason == "no_searchable_tokens"
    # A stray FTS operator / paren is stripped; the real token still matches and
    # the MATCH never sees an injected operator or syntax error.
    assert any(h.game_id == "enemy_1105_drone" for h in search_entities(conn, query="drone)").hits)
    # Raw quotes / a NEAR keyword are parsed as literal tokens, never operators.
    search_entities(conn, query='" NEAR drone')  # must not raise


def test_search_is_read_only(conn: sqlite3.Connection) -> None:
    # The service only reads -- no writes recorded on the connection.
    before = conn.total_changes
    search_entities(conn, query="drone")
    assert conn.total_changes == before


# --- result bound + operator tags/aliases -------------------------------------


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
    # Search returns at most MAX_LIMIT even when more rows match.
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
    # The service *rejects* an out-of-range limit rather than silently
    # clamping -- the same contract SearchEntitiesInput enforces at the MCP gate,
    # so a caller reaching the service directly gets no silent widening/narrowing.
    for bad in (0, -1, MAX_LIMIT + 1, 100):
        with pytest.raises(ValueError):
            search_entities(conn, query="drone", limit=bad)


def test_operator_tags_and_aliases_indexed(tmp_path: Path) -> None:
    # The operator importer lands in M4; the index builder already covers the
    # tags + aliases columns -- verify against a synthetic operator row.
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


# --- item domain in the shared FTS index --------------------------------------


def _seed_item(tmp_path: Path) -> Path:
    """Build a DB carrying one synthetic item row + its FTS document."""
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
    # An item is resolvable by name -> game_id so get_item_drops has a real
    # name->id path (the FTS locator's game_id is exactly items.game_id).
    with open_read_only(_seed_item(tmp_path)) as conn:
        by_name = search_entities(conn, query="Loxic").hits
        hit = next(h for h in by_name if h.entity_type == "item")
        assert hit.game_id == "30073"
        assert hit.server == "en"
        assert any(h.game_id == "30073" for h in search_entities(conn, query="30073").hits)


def test_item_entity_type_filter(tmp_path: Path) -> None:
    # The item domain narrows via entity_type, like the other domains.
    with open_read_only(_seed_item(tmp_path)) as conn:
        assert search_entities(conn, query="Loxic", entity_type="item").hits
        assert search_entities(conn, query="Loxic", entity_type="enemy").hits == ()


def test_item_locator_feeds_get_item_drops(tmp_path: Path) -> None:
    # The item locator's game_id is the key get_item_drops resolves items by
    # ((server, game_id)), so a search hit is a live name->id bridge, not a dead end.
    from arknights_mcp.db.repositories.drops import DropRepository

    with open_read_only(_seed_item(tmp_path)) as conn:
        hit = next(h for h in search_entities(conn, query="Loxic").hits if h.entity_type == "item")
        resolved = DropRepository(conn).item_by_game_id(hit.server, hit.game_id)
        assert resolved is not None


# --- zone/event display name as stage alias ------------------------------------


def test_zone_name_finds_stage_via_pipeline(conn: sqlite3.Connection) -> None:
    # The fixture stage's zone is "Chapter 4" (zone_table.json); the
    # in-pipeline index build carries the zone name on the stage document, so a
    # zone-name query surfaces the stage in both search tools.
    hits = search_entities(conn, query="Chapter").hits
    assert any(h.entity_type == "stage" and h.game_id == "main_04-04" for h in hits)
    stage_hits = search_stages(conn, query="Chapter").hits
    assert any(h.game_id == "main_04-04" for h in stage_hits)


def _seed_zone_and_stage(
    writer: sqlite3.Connection,
    provenance_id: int,
    *,
    server: str,
    zone_game_id: str,
    zone_name: str | None,
    stage_game_id: str,
    stage_name: str,
    zone_server: str | None = None,
) -> None:
    # ``zone_server`` (default: the stage's server) lets the region-guard test seed
    # the corrupt cross-region FK state without duplicating these INSERTs.
    cur = writer.execute(
        "INSERT INTO zones (server, game_id, display_name, zone_type) VALUES (?,?,?,?)",
        (zone_server or server, zone_game_id, zone_name, "ACTIVITY"),
    )
    writer.execute(
        "INSERT INTO stages (server, game_id, stage_code, display_name, zone_pk, "
        "provenance_id) VALUES (?,?,?,?,?,?)",
        (server, stage_game_id, "XX-1", stage_name, int(cur.lastrowid), provenance_id),
    )


def test_event_name_finds_only_its_stages(tmp_path: Path) -> None:
    # An event display name matches the stages in that zone -- and only
    # those; a stage in a different zone stays out of the result set.
    path = tmp_path / "zone.sqlite"
    writer = build_database(path)
    provenance_id = _seed_provenance(writer)
    _seed_zone_and_stage(
        writer,
        provenance_id,
        server="en",
        zone_game_id="act33side",
        zone_name="Lone Trail",
        stage_game_id="act33side_01",
        stage_name="Frontier",
    )
    _seed_zone_and_stage(
        writer,
        provenance_id,
        server="en",
        zone_game_id="act34side",
        zone_name="Other Event",
        stage_game_id="act34side_01",
        stage_name="Elsewhere",
    )
    build_search_index(writer)
    writer.commit()
    writer.close()
    with open_read_only(path) as conn:
        hits = search_entities(conn, query="Lone Trail").hits
        assert [h.game_id for h in hits] == ["act33side_01"]
        assert hits[0].entity_type == "stage"


def test_zone_alias_is_region_guarded(tmp_path: Path) -> None:
    # The index join is guarded on z.server = s.server -- a stage whose
    # zone_pk (wrongly) points at the other region's zone row must not borrow
    # that region's zone name into its search document.
    path = tmp_path / "zone_region.sqlite"
    writer = build_database(path)
    provenance_id = _seed_provenance(writer)
    _seed_zone_and_stage(
        writer,
        provenance_id,
        server="en",
        zone_game_id="act33side",
        zone_name="Lone Trail",
        stage_game_id="act33side_01",
        stage_name="Frontier",
        zone_server="cn",
    )
    build_search_index(writer)
    writer.commit()
    writer.close()
    with open_read_only(path) as conn:
        assert search_entities(conn, query="Lone Trail", server="en").hits == ()


# --- deterministic region order ------------------------------------------------


@pytest.fixture
def two_region_conn(tmp_path: Path) -> sqlite3.Connection:
    """Both regions built from the same 4-4 fixture: identical FTS documents per
    region, so every bm25 rank ties across en/cn and only the deterministic
    region order decides who comes first."""
    path = tmp_path / "cand2.sqlite"
    build_candidate(
        path,
        [
            ServerImport(
                "en", LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot"), "local_snapshot"
            ),
            ServerImport(
                "cn", LocalSnapshotAdapter(FIXTURE_ROOT, "cn", "local_snapshot"), "local_snapshot"
            ),
        ],
        registry=load_source_registry(REGISTRY),
    )
    return open_read_only(path)


def test_unfiltered_search_lists_en_before_cn(two_region_conn: sqlite3.Connection) -> None:
    # An unfiltered search previously broke bm25 ties by insert order, so
    # results[0] could be either region. Order is now region-major: every en hit
    # precedes every cn hit, deterministically.
    hits = search_entities(two_region_conn, query="drone").hits
    servers = [h.server for h in hits]
    assert "en" in servers and "cn" in servers
    assert servers == sorted(servers, key=lambda s: 0 if s == "en" else 1)


def test_unfiltered_stage_search_ranks_en_exact_code_first(
    two_region_conn: sqlite3.Connection,
) -> None:
    # Unfiltered "4-4" must surface the en exact-code row first, never the cn
    # twin; the exact-code-first contract is preserved, region orders within
    # the exact group.
    hits = search_stages(two_region_conn, query="4-4").hits
    assert hits[0].server == "en"
    assert hits[0].stage_code == "4-4"
    exact = [h for h in hits if (h.stage_code or "").lower() == "4-4"]
    assert any(h.server == "cn" for h in exact)
    assert [h.server for h in exact] == sorted(
        (h.server for h in exact), key=lambda s: 0 if s == "en" else 1
    )


# --- membership vs display: region order never evicts a better match -----------


@pytest.fixture
def saturated_conn() -> sqlite3.Connection:
    """A scratch index where >= limit weak en docs compete with one strong cn doc.

    12 en documents carry the query token buried in a long name (weak bm25); the
    single cn document IS the token (short doc, strongest bm25). Membership in the
    bounded result set must be best-match-first, so the cn row survives; display is
    then region-major (en before cn). The stub ``stages`` / ``zones`` tables satisfy the
    difficulty and zone-name LEFT JOINs.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE VIRTUAL TABLE entity_fts USING fts5(
            game_id, name, aliases, stage_code, tags,
            entity_type UNINDEXED, server UNINDEXED, entity_pk UNINDEXED,
            tokenize = 'unicode61'
        );
        CREATE TABLE stages (
            stage_pk INTEGER, server TEXT, difficulty TEXT, zone_pk INTEGER
        );
        CREATE TABLE zones (zone_pk INTEGER, server TEXT, display_name TEXT);
        """
    )
    for i in range(12):
        conn.execute(
            "INSERT INTO entity_fts (game_id, name, aliases, stage_code, tags, "
            "entity_type, server, entity_pk) VALUES (?, ?, '', '', '', 'item', 'en', ?)",
            (f"en_item_{i:02d}", f"chip catalyst pack alpha beta gamma delta {i:02d}", i + 1),
        )
    conn.execute(
        "INSERT INTO entity_fts (game_id, name, aliases, stage_code, tags, "
        "entity_type, server, entity_pk) VALUES "
        "('cn_item_chip', 'chip', '', '', '', 'item', 'cn', 100)"
    )
    return conn


def test_region_order_never_evicts_stronger_cn_match(saturated_conn: sqlite3.Connection) -> None:
    # Deterministic ORDER, not membership, was the requirement: with >= limit weak en
    # matches, the strongest hit (cn) must stay IN the bounded set -- membership is
    # bm25-first -- while the returned set still lists en before cn.
    rows = SearchRepository(saturated_conn).search(
        '"chip"', server=None, entity_type=None, limit=10
    )
    assert len(rows) == 10
    servers = [r.server for r in rows]
    assert "cn" in servers  # the strongest match is never displaced by weaker en hits
    assert servers == sorted(servers, key=lambda s: 0 if s == "en" else 1)
    assert rows[-1].game_id == "cn_item_chip"


def test_stage_region_order_never_evicts_stronger_cn_match() -> None:
    # Same membership rule for the stage query's non-exact group: the exact-code
    # group leads, then bm25 decides membership and region only decides display.
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE VIRTUAL TABLE entity_fts USING fts5(
            game_id, name, aliases, stage_code, tags,
            entity_type UNINDEXED, server UNINDEXED, entity_pk UNINDEXED,
            tokenize = 'unicode61'
        );
        CREATE TABLE stages (
            stage_pk INTEGER, server TEXT, difficulty TEXT, zone_pk INTEGER
        );
        CREATE TABLE zones (zone_pk INTEGER, server TEXT, display_name TEXT);
        """
    )
    for i in range(12):
        conn.execute(
            "INSERT INTO entity_fts (game_id, name, aliases, stage_code, tags, "
            "entity_type, server, entity_pk) VALUES (?, ?, '', ?, '', 'stage', 'en', ?)",
            (
                f"en_stage_{i:02d}",
                f"lone trail outskirts approach segment part {i:02d}",
                f"LT-{i:02d}",
                i + 1,
            ),
        )
    conn.execute(
        "INSERT INTO entity_fts (game_id, name, aliases, stage_code, tags, "
        "entity_type, server, entity_pk) VALUES "
        "('cn_stage_trail', 'trail', '', 'TR-1', '', 'stage', 'cn', 100)"
    )
    rows = SearchRepository(conn).search_stages(
        '"trail"', exact_code="trail", server=None, limit=10
    )
    assert len(rows) == 10
    assert any(r.game_id == "cn_stage_trail" for r in rows)


# --- zone-alias hardening (own-name precedence) --------------------------------


#: The "Gavial's Footprints" member stages, TRANSCRIBED from the shipped build
#: ``2026-07-27T093847Z-en-cn.sqlite`` (en) rather than invented. That matters: the
#: first draft of this fixture named them "Unrelated Stage Name 00".. and the guard
#: passed BEFORE the fix, because bm25 length-normalizes -- long invented names scored
#: the alias-only docs down until they no longer outranked anything, and the fixture
#: quietly stopped reproducing the defect. The real names are short ("Feint", "Torrent"),
#: which is precisely why their documents beat the own-name hits on the real corpus.
#: Same synthetic-fixture escape as the earlier ones; see the non-degeneracy assertion below.
_FOOTPRINTS_MEMBERS: tuple[tuple[str, str, str], ...] = (
    ("act12d0_ex01", "RI-EX-1", "Feint"),
    ("act12d0_ex02", "RI-EX-2", "Wanderlust"),
    ("act12d0_ex03", "RI-EX-3", "Mushroom Overgrowth"),
    ("act12d0_ex04", "RI-EX-4", "No Gain Without Loss"),
    ("act12d0_ex05", "RI-EX-5", "Trap Within the Leaves"),
    ("act12d0_ex06", "RI-EX-6", "Torrent"),
    ("act12d0_ex07", "RI-EX-7", "Soft Terrain"),
    ("act12d0_ex08", "RI-EX-8", "Rampage"),
    ("act12d0_ex01#f#", "RI-EX-1", "Feint"),
    ("act12d0_ex02#f#", "RI-EX-2", "Wanderlust"),
    ("act12d0_ex03#f#", "RI-EX-3", "Mushroom Overgrowth"),
    ("act12d0_ex06#f#", "RI-EX-6", "Torrent"),
    ("act12d0_ex08#f#", "RI-EX-8", "Rampage"),
)

#: Every document in the fixture that matches "Gavial" through its OWN name -- whether
#: in ``name`` (the operator, the stage "Gavial's Fist") or in an ``operator_aliases``
#: row holding that same operator's name in another language. Everything else matching
#: the query does so only through a zone name that belongs to a different entity.
_OWN_NAME_GAME_IDS = frozenset({"char_187_ccheal", "char_1026_gvial2", "act12d0_08"})


def _seed_collision(path: Path, *, member_stages: int = len(_FOOTPRINTS_MEMBERS)) -> None:
    """The collision, reduced from the real corpus: one event named after an operator.

    Four kinds of document, all transcribed from the shipped en build:

    * ``char_187_ccheal`` "Gavial" -- the short, unambiguous own-name hit;
    * ``char_1026_gvial2`` -- display name is the Chinese one, so it matches ONLY
      through its ``operator_aliases`` row. This is the carve-out: the ``aliases``
      column holds this entity's OWN name in another language, so it must keep full
      ranking, which is why the fix is scoped to stage documents instead of
      down-weighting the column globally;
    * ``act12d0_08`` "Gavial's Fist" -- a stage matching on its own name, from a zone
      named something else entirely ("Great Chief's Path");
    * ``member_stages`` stages of "Gavial's Footprints", matching ONLY through the
      zone alias, their own names and codes saying nothing about the query.
    """
    writer = build_database(path)
    provenance_id = _seed_provenance(writer)
    writer.execute(
        "INSERT INTO operators (server, game_id, display_name, provenance_id) VALUES (?,?,?,?)",
        ("en", "char_187_ccheal", "Gavial", provenance_id),
    )
    cur = writer.execute(
        "INSERT INTO operators (server, game_id, display_name, provenance_id) VALUES (?,?,?,?)",
        ("en", "char_1026_gvial2", "百炼嘉维尔", provenance_id),
    )
    writer.execute(
        "INSERT INTO operator_aliases (operator_pk, alias) VALUES (?,?)",
        (int(cur.lastrowid), "Gavial the Invincible 百炼嘉维尔"),
    )
    cur = writer.execute(
        "INSERT INTO zones (server, game_id, display_name, zone_type) VALUES (?,?,?,?)",
        ("en", "act12d0", "Gavial's Footprints", "ACTIVITY"),
    )
    event_zone_pk = int(cur.lastrowid)
    for game_id, stage_code, display_name in _FOOTPRINTS_MEMBERS[:member_stages]:
        writer.execute(
            "INSERT INTO stages (server, game_id, stage_code, display_name, zone_pk, "
            "provenance_id) VALUES (?,?,?,?,?,?)",
            ("en", game_id, stage_code, display_name, event_zone_pk, provenance_id),
        )
    cur = writer.execute(
        "INSERT INTO zones (server, game_id, display_name, zone_type) VALUES (?,?,?,?)",
        ("en", "act12d0_main", "Great Chief's Path", "ACTIVITY"),
    )
    writer.execute(
        "INSERT INTO stages (server, game_id, stage_code, display_name, zone_pk, "
        "provenance_id) VALUES (?,?,?,?,?,?)",
        ("en", "act12d0_08", "RI-8", "Gavial's Fist", int(cur.lastrowid), provenance_id),
    )
    build_search_index(writer)
    writer.commit()
    writer.close()


def test_collision_fixture_still_reproduces_b113(tmp_path: Path) -> None:
    """NON-DEGENERACY guard: the fixture must still exercise the bug.

    Ranked the old way -- plain bm25 with no own-name group -- an alias-only member
    stage has to come out ahead of at least one own-name document, or the tests below
    would pass on the unfixed code and prove nothing. This assertion is what the first
    draft of the fixture silently failed.
    """
    path = tmp_path / "degenerate.sqlite"
    _seed_collision(path)
    with open_read_only(path) as conn:
        unfixed = [
            row[0]
            for row in conn.execute(
                "SELECT entity_fts.game_id FROM entity_fts WHERE entity_fts MATCH ? "
                "ORDER BY rank, entity_fts.game_id",
                ('"Gavial"*',),
            )
        ]
    alias_only = [i for i, gid in enumerate(unfixed) if gid not in _OWN_NAME_GAME_IDS]
    own_name = [i for i, gid in enumerate(unfixed) if gid in _OWN_NAME_GAME_IDS]
    assert min(alias_only) < max(own_name), (
        "fixture no longer inverts under plain bm25; the guards below are vacuous"
    )


def test_zone_alias_never_outranks_an_own_name_match(tmp_path: Path) -> None:
    # The operator and the stage NAMED "Gavial's Fist" matched the query in
    # their own names; the event's member stages matched only through the zone alias.
    # Every own-name hit must precede every alias-only hit -- on the real corpus this
    # ordering was inverted, with seven alias-only stages ahead of both.
    path = tmp_path / "collision.sqlite"
    _seed_collision(path)
    with open_read_only(path) as conn:
        hits = search_entities(conn, query="Gavial", limit=MAX_LIMIT).hits
        positions = {h.game_id: i for i, h in enumerate(hits)}
        assert set(positions) >= _OWN_NAME_GAME_IDS, "own-name entities missing from the results"
        alias_only = [i for gid, i in positions.items() if gid not in _OWN_NAME_GAME_IDS]
        assert alias_only, "fixture no longer exercises the defect: no alias-only hits"
        assert max(positions[gid] for gid in _OWN_NAME_GAME_IDS) < min(alias_only)


def test_zone_alias_never_evicts_an_own_name_match_from_the_bounded_set(
    tmp_path: Path,
) -> None:
    # The membership half of the defect (the part a display-only sort cannot fix): with more
    # alias-only member stages than the whole result window, an own-name match must
    # still be IN the bounded set. 13 members vs limit 10 -- under the old ordering the
    # cn operator and "Gavial's Fist" fell to #17 and #14 on the real corpus.
    path = tmp_path / "evict.sqlite"
    _seed_collision(path)
    with open_read_only(path) as conn:
        hits = search_entities(conn, query="Gavial", limit=10).hits
        assert len(hits) == 10
        assert {h.game_id for h in hits} >= _OWN_NAME_GAME_IDS


def test_stage_search_alias_precedence_sits_under_exact_code(tmp_path: Path) -> None:
    # Exact-code-first is not weakened by own-name precedence: an exact stage-code match
    # still wins outright, and only below it does own-name beat alias-only. Both rules
    # apply in search_stages.
    path = tmp_path / "stage_precedence.sqlite"
    _seed_collision(path)
    with open_read_only(path) as conn:
        hits = search_stages(conn, query="RI-EX-3", limit=MAX_LIMIT).hits
        assert hits[0].stage_code == "RI-EX-3"
        gavial = search_stages(conn, query="Gavial", limit=MAX_LIMIT).hits
        assert gavial[0].game_id == "act12d0_08"


def test_alias_only_hit_carries_the_zone_name_that_matched(tmp_path: Path) -> None:
    # The second half of the fix: an alias hit was unattributable on the wire. A stage whose
    # own name and code say nothing about "Gavial" now ships the string that did match.
    path = tmp_path / "attribution.sqlite"
    _seed_collision(path, member_stages=3)
    with open_read_only(path) as conn:
        hits = search_entities(conn, query="Gavial", limit=MAX_LIMIT).hits
        alias_hit = next(h for h in hits if h.game_id.startswith("act12d0_ex"))
        assert "Gavial" not in (alias_hit.display_name or "")
        assert alias_hit.zone_display_name == "Gavial's Footprints"
        operator = next(h for h in hits if h.entity_type == "operator")
        assert operator.zone_display_name is None


def test_zone_name_absent_leaves_zone_display_name_none(tmp_path: Path) -> None:
    # A stage whose zone carries no name in source (416 of 3264 en stages on the
    # 2026-07-27 build) yields None here, and the tool layer omits the key -- never a
    # null a client cannot tell from "this zone is called nothing".
    path = tmp_path / "unnamed_zone.sqlite"
    writer = build_database(path)
    provenance_id = _seed_provenance(writer)
    _seed_zone_and_stage(
        writer,
        provenance_id,
        server="en",
        zone_game_id="tower_n_01",
        zone_name=None,
        stage_game_id="lt_01_01",
        stage_name="Mountainous Maze",
    )
    build_search_index(writer)
    writer.commit()
    writer.close()
    with open_read_only(path) as conn:
        hit = next(h for h in search_entities(conn, query="Mountainous").hits)
        assert hit.zone_display_name is None
