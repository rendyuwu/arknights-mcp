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
from typing import Any

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.db.migrations import build_database, open_writable
from arknights_mcp.importers.extra_locale_aliases import import_locale_aliases
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.importers.search_index import build_search_index, rebuild_search_index
from arknights_mcp.services.search import MAX_LIMIT, search_entities, search_stages
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"


class _FakeFetcher:
    """In-memory extra-locale fetcher (no network, §V1) for the T100 rebuild test."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def fetch(self) -> dict[str, Any]:
        return self._payload


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


# --- §T100 / §V57: locale-tagged aliases in the rebuilt FTS index --------------


def _build_with_jp_alias(tmp_path: Path) -> Path:
    """Build the en 4-4 candidate, attach a jp NAME alias to an existing enemy, and
    rebuild the FTS index so it carries the locale-tagged alias (§T100/§V37).

    Exercises the single §V37 rebuild home (:func:`rebuild_search_index`): the jp
    alias is inserted into ``enemy_aliases`` AFTER the in-pipeline build, then the
    index is rebuilt from the surviving rows so the ja alias becomes searchable --
    the exact T100 "rebuild ``entity_fts`` with locale-tagged aliases" flow.
    """
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    writer = open_writable(path)
    try:
        # The fixture 4-4 carries enemy_1105_drone (en display "Drone"); attach its
        # katakana NAME as a jp locale alias via the T99 importer, then rebuild.
        fetcher = _FakeFetcher(
            {
                "character_table": {},
                "enemy_handbook": {"enemyData": {"enemy_1105_drone": {"name": "ドローン"}}},
            }
        )
        import_locale_aliases(writer, fetcher, region="jp")
        rebuild_search_index(writer)
        writer.commit()
    finally:
        writer.close()
    return path


def test_jp_alias_is_searchable_after_rebuild(tmp_path: Path) -> None:
    # §T100/§V57: the rebuilt index carries the locale-tagged ja alias, so the
    # katakana NAME resolves the en enemy -- and the hit returns the entity's OWN
    # en region (an alias match never widens/relabels region, §V5/§V57).
    path = _build_with_jp_alias(tmp_path)
    with open_read_only(path) as conn:
        hits = search_entities(conn, query="ドローン").hits
        drone = next(h for h in hits if h.game_id == "enemy_1105_drone")
        assert drone.server == "en"


def test_locale_filter_narrows_to_locale_alias(tmp_path: Path) -> None:
    # §V57: a locale filter keeps only entities carrying an alias in that locale.
    # The drone got a ja alias, so it survives a locale=ja filter; there is no ko
    # alias in this build, so locale=ko yields nothing for the same query.
    path = _build_with_jp_alias(tmp_path)
    with open_read_only(path) as conn:
        ja = search_entities(conn, query="drone", locale="ja").hits
        assert any(h.game_id == "enemy_1105_drone" for h in ja)
        ko = search_entities(conn, query="drone", locale="ko")
        assert ko.status == "not_found"
        assert ko.hits == ()


def test_locale_none_is_unchanged_from_prior_behavior(tmp_path: Path) -> None:
    # §V21: locale defaults to None -> the search is byte-for-byte the prior path.
    path = _build_with_jp_alias(tmp_path)
    with open_read_only(path) as conn:
        assert (
            search_entities(conn, query="drone").hits
            == search_entities(conn, query="drone", locale=None).hits
        )


def test_locale_does_not_widen_region_availability(tmp_path: Path) -> None:
    # §V50/§V57: an extra-locale search NEVER widens region availability. This build
    # has an en snapshot only. A ja-locale search scoped to cn (no cn snapshot) is
    # still ``data_stale`` -- the region gate runs independently of locale, so a
    # jp/kr alias can never make a region look present when it is not.
    path = _build_with_jp_alias(tmp_path)
    with open_read_only(path) as conn:
        stale = search_entities(conn, query="drone", locale="ja", server="cn")
        assert stale.status == "data_stale"
        assert stale.hits == ()
        # Scoped to the region that DOES have the snapshot, the ja alias resolves and
        # the facts stay en (§V57: alias match returns the entity's OWN region).
        ok = search_entities(conn, query="drone", locale="ja", server="en")
        assert ok.status == "ok"
        assert all(h.server == "en" for h in ok.hits)


def test_locale_filter_excludes_stages(tmp_path: Path) -> None:
    # §V57: stages have no alias table, so a locale-scoped search never returns a
    # stage. 4-4 matches by stage_code unfiltered, but drops under any locale filter.
    path = _build_with_jp_alias(tmp_path)
    with open_read_only(path) as conn:
        assert any(h.entity_type == "stage" for h in search_entities(conn, query="4-4").hits)
        assert search_entities(conn, query="4-4", locale="ja").hits == ()


def test_search_locale_domain_matches_field_policy_maps() -> None:
    # §V37/B50: the ``SearchLocale`` filter domain is kept in lock-step with the single
    # ``field_policy`` extra-locale map rather than re-declared. The searchable locales
    # are EXACTLY the extra-locale alias tags (EXTRA_LOCALE_FOR_REGION values, jp/kr ->
    # ja/ko) -- the fact-region locales (en/zh) are excluded (degenerate ≈ ``server=``
    # + asymmetric-broken, B50). A new extra locale added to the map without widening
    # ``SearchLocale`` (or vice versa) fails here.
    from typing import get_args

    from arknights_mcp.importers.field_policy import EXTRA_LOCALE_FOR_REGION
    from arknights_mcp.models.common import SearchLocale

    assert set(get_args(SearchLocale)) == set(EXTRA_LOCALE_FOR_REGION.values())
