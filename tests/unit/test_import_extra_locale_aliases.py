"""T99: the extra-locale alias importer (jp/kr canonical NAMES -> locale aliases).

Covers the §T99 contract with an in-memory fake fetcher (no live network, §V1):
the NAME-only allowlist (§V57/§V18 -- prose/description dropped), the locale tag
(§V57 -- ``ja``/``ko``, not a fact region), a name attaching to every en/cn row of
its game_id, the fact region + facts staying unchanged, the §V30 non-empty-but-no-
match fail-closed guard, and the one shared insert helper covering both the operator
and enemy domains (§V37).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.extra_locale_aliases import (
    LOCALE_NAME_ALLOWLIST,
    import_locale_aliases,
    parse_character_names,
    parse_enemy_handbook_names,
)


class _FakeFetcher:
    """Returns a preset payload; records that it was called."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls = 0

    def fetch(self) -> dict[str, Any]:
        self.calls += 1
        return self._payload


def _seed_sources(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) VALUES "
        "('arknights_assets_gamedata', 'AK', 'owner', 'https://x', 'game_data', "
        "'[\"en\"]', '1', 'reviewed', 'permitted', 'code_only', 'attribution', 1, '2026-07-20')"
    )


def _prov(conn: sqlite3.Connection, region: str, game_id: str) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES "
        "(?, 'arknights_assets_gamedata', ?, '2026-07-20T00:00:00+00:00', 'h', 'imported', '1')",
        (f"{region}:ak", region),
    )
    return int(
        conn.execute(
            "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
            "record_hash, transform_version, field_policy_version) "
            "VALUES (?, 'character_table', ?, 'rh', '1', '1')",
            (f"{region}:ak", game_id),
        ).lastrowid
    )


def _seed_operator(conn: sqlite3.Connection, *, region: str, game_id: str, name: str) -> None:
    prov = _prov(conn, region, game_id)
    conn.execute(
        "INSERT INTO operators (server, game_id, display_name, provenance_id) VALUES (?, ?, ?, ?)",
        (region, game_id, name, prov),
    )


def _seed_enemy(conn: sqlite3.Connection, *, region: str, game_id: str, name: str) -> None:
    prov = _prov(conn, region, game_id)
    conn.execute(
        "INSERT INTO enemies (server, game_id, display_name, provenance_id) VALUES (?, ?, ?, ?)",
        (region, game_id, name, prov),
    )


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_sources(conn)
    return conn


# --- pure parsing: NAME-only allowlist (§V57/§V18) ----------------------------


def test_parse_keeps_name_drops_prose() -> None:
    # §V18/§V57: only the canonical NAME survives; description/appellation dropped.
    raw = {
        "char_002_amiya": {
            "name": "アーミヤ",  # katakana "Amiya"
            "appellation": "Amiya",
            "description": "<prose that must never be stored>",
            "itemUsage": "more prose",
        }
    }
    assert parse_character_names(raw) == {"char_002_amiya": "アーミヤ"}


def test_parse_strips_control_chars_in_name() -> None:
    # §V18: a kept NAME is sanitized (control + bidi-format chars stripped). The
    # dirty chars are built via escapes so no raw control byte lands in this source.
    dirty = "ケ" + "‮" + "ルシ" + "\x00" + "ー"
    parsed = parse_character_names({"char_x": {"name": dirty}})
    assert "‮" not in parsed["char_x"]
    assert "\x00" not in parsed["char_x"]


def test_allowlist_is_name_only() -> None:
    # The allowlist is exactly {"name"} -- no other field can ride in (§V57).
    assert frozenset({"name"}) == LOCALE_NAME_ALLOWLIST


def test_enemy_handbook_unwraps_enemydata() -> None:
    # The real handbook wraps id-keyed entries under enemyData (§V29); both shapes parse.
    wrapped = {"enemyData": {"enemy_1007_slime": {"name": "スライム"}}}
    bare = {"enemy_1007_slime": {"name": "スライム"}}
    assert parse_enemy_handbook_names(wrapped) == {"enemy_1007_slime": "スライム"}
    assert parse_enemy_handbook_names(bare) == {"enemy_1007_slime": "スライム"}


def test_entry_without_name_is_skipped() -> None:
    assert parse_character_names({"char_x": {"appellation": "no name here"}}) == {}


# --- import: locale tag + both regions (§V57) ---------------------------------


def test_import_attaches_ja_alias_to_both_regions(tmp_path: Path) -> None:
    # §V57: a jp NAME attaches to EVERY en/cn row sharing the game_id, tagged locale
    # `ja` -- NOT a fact region. The entity's own server + display_name are unchanged.
    conn = _db(tmp_path)
    ja_name = "アーミヤ"  # katakana "Amiya"
    try:
        _seed_operator(conn, region="en", game_id="char_002_amiya", name="Amiya")
        _seed_operator(conn, region="cn", game_id="char_002_amiya", name="阿米娅")
        _seed_enemy(conn, region="en", game_id="enemy_1007_slime", name="Originium Slug")

        fetcher = _FakeFetcher(
            {
                "character_table": {"char_002_amiya": {"name": ja_name}},
                "enemy_handbook": {"enemyData": {"enemy_1007_slime": {"name": "ゲル"}}},
            }
        )
        result = import_locale_aliases(conn, fetcher, region="jp")
        assert result.locale == "ja"
        # 1 name x 2 operator rows (en + cn); 1 enemy name x 1 enemy row.
        assert result.operator_aliases_inserted == 2
        assert result.enemy_aliases_inserted == 1
        assert result.candidate_names == 2

        alias_rows = conn.execute(
            "SELECT o.server, a.alias, a.alias_type, a.locale, a.normalized_alias "
            "FROM operator_aliases a JOIN operators o ON o.operator_pk = a.operator_pk "
            "ORDER BY o.server"
        ).fetchall()
        assert alias_rows == [
            ("cn", ja_name, "locale_name", "ja", ja_name.casefold()),
            ("en", ja_name, "locale_name", "ja", ja_name.casefold()),
        ]

        # §V57: fact region + facts untouched -- the operator is still en/cn with its
        # own display_name; the jp NAME is only an alias.
        ops = conn.execute("SELECT server, display_name FROM operators ORDER BY server").fetchall()
        assert ops == [("cn", "阿米娅"), ("en", "Amiya")]
    finally:
        conn.close()


def test_kr_locale_tag(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        _seed_operator(conn, region="en", game_id="char_002_amiya", name="Amiya")
        fetcher = _FakeFetcher(
            {
                "character_table": {"char_002_amiya": {"name": "아미야"}},
                "enemy_handbook": {},
            }
        )
        result = import_locale_aliases(conn, fetcher, region="kr")
        assert result.locale == "ko"
        locale = conn.execute("SELECT locale FROM operator_aliases").fetchone()[0]
        assert locale == "ko"
    finally:
        conn.close()


def test_game_id_absent_from_entity_table_attaches_nothing(tmp_path: Path) -> None:
    # A name whose game_id is not among the imported entities contributes no alias,
    # but as long as SOME name matched the build is fine (no §V30 trip).
    conn = _db(tmp_path)
    try:
        _seed_operator(conn, region="en", game_id="char_002_amiya", name="Amiya")
        fetcher = _FakeFetcher(
            {
                "character_table": {
                    "char_002_amiya": {"name": "アーミヤ"},
                    "char_999_ghost": {"name": "ゴースト"},  # no such entity
                },
                "enemy_handbook": {},
            }
        )
        result = import_locale_aliases(conn, fetcher, region="jp")
        assert result.candidate_names == 2
        assert result.operator_aliases_inserted == 1  # only amiya matched
        assert conn.execute("SELECT COUNT(*) FROM operator_aliases").fetchone()[0] == 1
    finally:
        conn.close()


# --- §V30 fail-closed + empty source ------------------------------------------


def test_non_empty_source_matching_nothing_fails_closed(tmp_path: Path) -> None:
    # §V30: candidate names present but ZERO matched an existing entity (a game_id
    # scheme mismatch) -> ImporterError, not a silent empty build.
    conn = _db(tmp_path)
    try:
        _seed_operator(conn, region="en", game_id="char_002_amiya", name="Amiya")
        fetcher = _FakeFetcher(
            {
                "character_table": {"WRONG_ID_scheme": {"name": "アーミヤ"}},
                "enemy_handbook": {},
            }
        )
        with pytest.raises(ImporterError):
            import_locale_aliases(conn, fetcher, region="jp")
    finally:
        conn.close()


def test_per_domain_guard_enemy_mismatch_fails_closed_despite_operator_match(
    tmp_path: Path,
) -> None:
    # §V30/B51: the guard is PER DOMAIN, keyed on MATCHED game_ids -- a sibling
    # domain's success must NOT mask a per-domain game_id-scheme mismatch. Here the
    # operator name matches (op_matched > 0) but every enemy name is under a wrong
    # scheme (enemy_matched == 0). The old combined-count guard saw inserted > 0 and
    # promoted a build whose enemy aliases silently attached nothing; the per-domain
    # guard fails closed and names the missed domain.
    conn = _db(tmp_path)
    try:
        _seed_operator(conn, region="en", game_id="char_002_amiya", name="Amiya")
        _seed_enemy(conn, region="en", game_id="enemy_1007_slime", name="Originium Slug")
        fetcher = _FakeFetcher(
            {
                # operator matches; every enemy name is under a wrong game_id scheme.
                "character_table": {"char_002_amiya": {"name": "アーミヤ"}},
                "enemy_handbook": {"enemyData": {"WRONG_enemy_scheme": {"name": "ゲル"}}},
            }
        )
        with pytest.raises(ImporterError, match="enemy"):
            import_locale_aliases(conn, fetcher, region="jp")
    finally:
        conn.close()


def test_per_domain_guard_operator_mismatch_fails_closed_despite_enemy_match(
    tmp_path: Path,
) -> None:
    # §V30/B51: the mirror case -- enemy matches but the operator scheme mismatches.
    conn = _db(tmp_path)
    try:
        _seed_operator(conn, region="en", game_id="char_002_amiya", name="Amiya")
        _seed_enemy(conn, region="en", game_id="enemy_1007_slime", name="Originium Slug")
        fetcher = _FakeFetcher(
            {
                "character_table": {"WRONG_char_scheme": {"name": "アーミヤ"}},  # 0 match
                "enemy_handbook": {"enemyData": {"enemy_1007_slime": {"name": "ゲル"}}},  # matches
            }
        )
        with pytest.raises(ImporterError, match="operator"):
            import_locale_aliases(conn, fetcher, region="jp")
    finally:
        conn.close()


def test_empty_source_imports_zero_without_error(tmp_path: Path) -> None:
    # A genuinely empty source (no candidate names) is a legitimate empty import.
    conn = _db(tmp_path)
    try:
        _seed_operator(conn, region="en", game_id="char_002_amiya", name="Amiya")
        fetcher = _FakeFetcher({"character_table": {}, "enemy_handbook": {}})
        result = import_locale_aliases(conn, fetcher, region="jp")
        assert result.candidate_names == 0
        assert (result.operator_aliases_inserted, result.enemy_aliases_inserted) == (0, 0)
        assert conn.execute("SELECT COUNT(*) FROM operator_aliases").fetchone()[0] == 0
    finally:
        conn.close()


def test_unknown_extra_locale_region_rejected(tmp_path: Path) -> None:
    # §V57: en/cn are fact regions, never extra-locale alias regions.
    conn = _db(tmp_path)
    try:
        fetcher = _FakeFetcher({"character_table": {}, "enemy_handbook": {}})
        with pytest.raises(ImporterError):
            import_locale_aliases(conn, fetcher, region="en")
    finally:
        conn.close()


# --- T109 idempotency: re-import does not double-insert (§V57/0012) ------------


def test_reimport_is_idempotent_no_duplicate_alias_rows(tmp_path: Path) -> None:
    # §T109: the sync ride-along re-imports these aliases on every run. Migration 0012's
    # UNIQUE(entity_pk, alias, locale) + INSERT OR IGNORE make a re-run a no-op -- exactly
    # one alias row survives, so a re-sync/backfill never doubles the FTS GROUP_CONCAT
    # token (§V37/B22). The second run reports 0 newly-inserted rows (rowcount-based),
    # while candidate_names is unchanged (the names are still parsed + matched).
    conn = _db(tmp_path)
    try:
        _seed_operator(conn, region="en", game_id="char_002_amiya", name="Amiya")
        _seed_enemy(conn, region="en", game_id="enemy_1007_slime", name="Originium Slug")
        payload = {
            "character_table": {"char_002_amiya": {"name": "アーミヤ"}},
            "enemy_handbook": {"enemyData": {"enemy_1007_slime": {"name": "ゲル"}}},
        }

        first = import_locale_aliases(conn, _FakeFetcher(payload), region="jp")
        assert (first.operator_aliases_inserted, first.enemy_aliases_inserted) == (1, 1)

        second = import_locale_aliases(conn, _FakeFetcher(payload), region="jp")
        # Re-run: nothing new is inserted (OR IGNORE suppressed both), but the names were
        # still parsed -- candidate_names is unchanged.
        assert (second.operator_aliases_inserted, second.enemy_aliases_inserted) == (0, 0)
        assert second.candidate_names == first.candidate_names

        # Exactly one row per (entity, alias, locale) survives -- no duplication.
        assert conn.execute("SELECT COUNT(*) FROM operator_aliases").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM enemy_aliases").fetchone()[0] == 1
    finally:
        conn.close()


def test_reimport_matched_guard_does_not_false_trip_under_or_ignore(tmp_path: Path) -> None:
    # §V30/B51: the fail-closed guard keys on MATCHED game_ids, NOT the physical insert
    # count -- so a re-run whose inserts are all suppressed by OR IGNORE must NOT trip the
    # guard (the game_ids still match an existing entity). A regression to an
    # inserted-count guard would raise here on the second run.
    conn = _db(tmp_path)
    try:
        _seed_operator(conn, region="en", game_id="char_002_amiya", name="Amiya")
        payload = {
            "character_table": {"char_002_amiya": {"name": "アーミヤ"}},
            "enemy_handbook": {},
        }
        import_locale_aliases(conn, _FakeFetcher(payload), region="jp")
        # Second run: 0 inserted, but op matched > 0 -> no ImporterError.
        second = import_locale_aliases(conn, _FakeFetcher(payload), region="jp")
        assert second.operator_aliases_inserted == 0
        assert conn.execute("SELECT COUNT(*) FROM operator_aliases").fetchone()[0] == 1
    finally:
        conn.close()
