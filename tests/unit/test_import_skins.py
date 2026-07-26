"""T182: skin-gallery importer (skin_table.json -> operator_skins; ADR 0015).

Parses the primary ``skin_table`` ``charSkins`` dict into the metadata-only named
skin gallery (§V88): identity/label fields only (no displaySkin prose, §V16/§V18),
token skins filtered, the alt-form link read from ``tmplId`` (base ``charId`` +
distinct ``tmplId``, ADR 0015 -- char_patch_table is NOT imported), soft-resolved to
an ``operator_pk`` when the operator is present else the raw char id with
``resolved = 0`` (an unresolvable skin never fails the build, §V3). A non-empty
operator-entry set yielding zero skins fails closed (§V30); an absent skin_table is
a legitimate empty build (B36). A duplicate skin id maps to a typed ImporterError
(§V33). Purge cascades the skin rows (§V32).

Fixture entries mirror the REAL upstream shape (§V29 class; verified live
2026-07-26): id-keyed ``charSkins``, ``displaySkin`` carrying skinName/skinGroupId/
skinGroupName alongside forbidden prose leaves, ``portraitId`` stems with ``#``/``+``.
"""

from __future__ import annotations

import json as _json
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.migrations import build_database
from arknights_mcp.db.purge import _purge_source_rows
from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.skins import (
    ParsedSkin,
    import_skins,
    insert_skins,
    parse_skins,
)
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

_SOURCE_ID = "local_snapshot"

# Prose that must never survive the metadata-only allowlist (§V16/§V18/ADR 0015).
_PROSE = "outfit flavor copy that must never be imported into the database"

_DEFAULT_E0 = {
    "skinId": "char_002_amiya#1",
    "charId": "char_002_amiya",
    "tmplId": "char_002_amiya",
    "avatarId": "char_002_amiya",
    "portraitId": "char_002_amiya_1",
    "isBuySkin": False,
    "displaySkin": {
        "skinName": None,
        "skinGroupId": "ILLUST_0",
        "skinGroupName": "Default Outfit",
        "content": _PROSE,
        "dialog": _PROSE,
        "usage": _PROSE,
        "description": _PROSE,
        "drawerList": ["artist name"],
        "modelName": "Amiya",
    },
}
_OUTFIT = {
    "skinId": "char_002_amiya@epoque#4",
    "charId": "char_002_amiya",
    "tmplId": "char_002_amiya",
    "portraitId": "char_002_amiya_epoque#4",
    "isBuySkin": True,
    "displaySkin": {
        "skinName": "Fresh Fastener",
        "skinGroupId": "2020#sale",
        "skinGroupName": "Test Collection/II",
        "content": _PROSE,
        "obtainApproach": "Event Reward",
    },
}
_ALT_FORM = {
    "skinId": "char_1001_amiya2#2",
    "charId": "char_002_amiya",  # BASE operator's char id (real upstream shape)
    "tmplId": "char_1001_amiya2",  # the alt-form discriminator (ADR 0015)
    "portraitId": "char_1001_amiya2_2",
    "isBuySkin": False,
    "displaySkin": {"skinName": None, "skinGroupId": "ILLUST_2", "skinGroupName": "Default Outfit"},
}
_TOKEN = {
    "skinId": "token_10000_silent_healrb#1",
    "charId": "token_10000_silent_healrb",
    # A REAL token entry can carry a portraitId, so the token filter must be the
    # guard that drops it -- a null portraitId here would let the missing-stem skip
    # mask a broken charId-prefix filter (two guards, one visible).
    "portraitId": "token_10000_silent_healrb_1",
    "displaySkin": {"skinName": None, "skinGroupId": "ILLUST_0"},
}


def _table(*entries: dict) -> dict:
    return {"charSkins": {e["skinId"]: e for e in entries}}


# --- pure parsing (no DB) ----------------------------------------------------


def test_token_skins_filtered_out() -> None:
    # ADR 0015: a non-char_ charId is a summon/token skin, not an operator gallery row.
    assert parse_skins(_table(_TOKEN)) == []
    assert [s.skin_id for s in parse_skins(_table(_TOKEN, _DEFAULT_E0))] == ["char_002_amiya#1"]


def test_named_outfit_parsed_with_name_group_and_portrait() -> None:
    [skin] = parse_skins(_table(_OUTFIT))
    assert skin.skin_id == "char_002_amiya@epoque#4"
    assert skin.char_id == "char_002_amiya"
    assert skin.portrait_id == "char_002_amiya_epoque#4"
    assert skin.display_name == "Fresh Fastener"
    assert skin.skin_group_id == "2020#sale"
    assert skin.skin_group_name == "Test Collection/II"
    assert skin.is_buy_skin is True


def test_default_skin_name_is_none() -> None:
    # Default E0/E1/E2 art carries no outfit name; None, never a fabricated label (§V26).
    [skin] = parse_skins(_table(_DEFAULT_E0))
    assert skin.display_name is None
    assert skin.skin_group_id == "ILLUST_0"
    assert skin.is_buy_skin is False


def test_prose_is_never_kept() -> None:
    # §V16/§V18: displaySkin prose/credit leaves never survive the sub-allowlist, so
    # they appear in no provenance record (the only place a raw fragment could ride in).
    parsed = parse_skins(_table(_DEFAULT_E0, _OUTFIT, _ALT_FORM))
    all_blob = _json.dumps([s.provenance_record for s in parsed])
    assert _PROSE not in all_blob
    for forbidden in ("content", "dialog", "usage", "description", "drawerList", "modelName"):
        assert forbidden not in all_blob
    assert "obtainApproach" not in all_blob


def test_alt_form_tmpl_id_differs_from_char_id() -> None:
    # ADR 0015: the alt-form skin carries the BASE charId + a distinct tmplId.
    [skin] = parse_skins(_table(_ALT_FORM))
    assert skin.char_id == "char_002_amiya"
    assert skin.tmpl_id == "char_1001_amiya2"


def test_entry_missing_portrait_id_skipped() -> None:
    # No art stem -> the §V63 URL cannot derive; skipped fail-closed, never fabricated.
    no_portrait = {**_OUTFIT, "portraitId": None}
    assert parse_skins(_table(no_portrait)) == []


def test_entry_missing_skin_id_skipped() -> None:
    entry = {k: v for k, v in _OUTFIT.items() if k != "skinId"}
    assert parse_skins({"charSkins": {"x": entry}}) == []


def test_control_char_skin_name_sanitized_to_none() -> None:
    # §V18 sanitize: an all-control-char name is stripped empty -> None (B52 lesson).
    dirty = {**_OUTFIT, "displaySkin": {**_OUTFIT["displaySkin"], "skinName": "\x00\x08\x1f"}}
    [skin] = parse_skins(_table(dirty))
    assert skin.display_name is None


def test_parse_order_is_deterministic() -> None:
    # Key-sorted iteration: same table -> same order regardless of dict insert order.
    a = parse_skins({"charSkins": {_OUTFIT["skinId"]: _OUTFIT, _DEFAULT_E0["skinId"]: _DEFAULT_E0}})
    b = parse_skins({"charSkins": {_DEFAULT_E0["skinId"]: _DEFAULT_E0, _OUTFIT["skinId"]: _OUTFIT}})
    assert [s.skin_id for s in a] == [s.skin_id for s in b]


def test_skin_table_without_charskins_parses_empty() -> None:
    assert parse_skins({"charSkins": []}) == []
    assert parse_skins({}) == []


def test_non_object_skin_table_raises() -> None:
    with pytest.raises(ImporterError, match="not a JSON object"):
        parse_skins([1, 2, 3])


# --- DB insertion + soft-resolve ---------------------------------------------


def _conn_with_operator(tmp_path: Path, *, seed_operator: bool) -> tuple[sqlite3.Connection, str]:
    """Build a candidate; seed the primary source snapshot and optionally base Amiya."""
    conn = build_database(tmp_path / "cand.sqlite")
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) VALUES "
        "(?, 'gd', 'o', 'https://x/', 'game_data_repository', '[\"en\"]', '0', 'reviewed', "
        "'reviewed', 'derived', 'a', 1, '2026-07-21')",
        (_SOURCE_ID,),
    )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES "
        "('snap-en', ?, 'en', '2026-07-26T00:00:00+00:00', 'h', 'active', 'test')",
        (_SOURCE_ID,),
    )
    if seed_operator:
        prov = conn.execute(
            "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
            "record_hash, transform_version, field_policy_version) VALUES "
            "('snap-en', 'gamedata/excel/character_table.json', 'char_002_amiya', 'rh', "
            "'test', 'test')"
        ).lastrowid
        conn.execute(
            "INSERT INTO operators (server, game_id, display_name, provenance_id) "
            "VALUES ('en', 'char_002_amiya', 'Amiya', ?)",
            (prov,),
        )
    conn.commit()
    return conn, "snap-en"


def _insert(conn: sqlite3.Connection, snap: str, *entries: dict) -> object:
    return insert_skins(
        conn,
        parse_skins(_table(*entries)),
        server="en",
        snapshot_id=snap,
        source_path="gamedata/excel/skin_table.json",
    )


def test_skin_soft_resolves_present_operator(tmp_path: Path) -> None:
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        result = _insert(conn, snap, _OUTFIT)
        assert result.skins_inserted == 1
        assert result.skins_resolved == 1
        row = conn.execute("SELECT operator_pk, resolved FROM operator_skins").fetchone()
        assert row[0] is not None and row[1] == 1
    finally:
        conn.close()


def test_skin_stays_raw_when_operator_absent(tmp_path: Path) -> None:
    # B36: a combat-only snapshot (no operators) keeps the raw char id, resolved=0;
    # the unresolvable skin never fails the build (§V3).
    conn, snap = _conn_with_operator(tmp_path, seed_operator=False)
    try:
        result = _insert(conn, snap, _OUTFIT)
        assert result.skins_inserted == 1
        assert result.skins_resolved == 0
        row = conn.execute("SELECT operator_pk, char_id, resolved FROM operator_skins").fetchone()
        assert row == (None, "char_002_amiya", 0)
    finally:
        conn.close()


def test_alt_form_skin_resolves_to_base_operator(tmp_path: Path) -> None:
    # ADR 0015: the amiya2 skin's charId is base Amiya, so it attaches to base Amiya's
    # operator_pk while tmpl_id keeps the alt-form label.
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        _insert(conn, snap, _ALT_FORM)
        row = conn.execute(
            "SELECT o.game_id, s.tmpl_id, s.resolved FROM operator_skins s "
            "JOIN operators o ON o.operator_pk = s.operator_pk"
        ).fetchone()
        assert row == ("char_002_amiya", "char_1001_amiya2", 1)
    finally:
        conn.close()


def test_region_equals_server(tmp_path: Path) -> None:
    # §V5: region is the fact region; en and cn are never mixed.
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        _insert(conn, snap, _DEFAULT_E0, _OUTFIT)
        assert conn.execute("SELECT DISTINCT region FROM operator_skins").fetchall() == [("en",)]
    finally:
        conn.close()


def test_every_row_carries_provenance(tmp_path: Path) -> None:
    # §V17: each skin row FKs a record_provenance row keyed on its skin_id.
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        _insert(conn, snap, _OUTFIT)
        row = conn.execute(
            "SELECT p.source_record_key, p.source_path FROM operator_skins s "
            "JOIN record_provenance p ON p.provenance_id = s.provenance_id"
        ).fetchone()
        assert row == ("char_002_amiya@epoque#4", "gamedata/excel/skin_table.json")
    finally:
        conn.close()


def test_duplicate_skin_id_fails_closed(tmp_path: Path) -> None:
    # §V33: a duplicate skinId collides on UNIQUE(server, skin_id); the anomaly maps
    # to a typed ImporterError, not an uncaught IntegrityError.
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        dup = [
            ParsedSkin("DUP", "char_002_amiya", None, "p_1", None, None, None, None, {})
            for _ in range(2)
        ]
        with pytest.raises(ImporterError, match="UNIQUE constraint"):
            insert_skins(conn, dup, server="en", snapshot_id=snap, source_path="skin_table.json")
    finally:
        conn.close()


# --- adapter-driven import (tolerant-absent + §V30 guard) --------------------


def _adapter_with_skins(tmp_path: Path, payload: object | None) -> LocalSnapshotAdapter:
    root = tmp_path / "snap"
    excel = root / "gamedata" / "excel"
    excel.mkdir(parents=True)
    if payload is not None:
        (excel / "skin_table.json").write_text(_json.dumps(payload), encoding="utf-8")
    return LocalSnapshotAdapter(root, "en")


def test_import_tolerates_absent_skin_table(tmp_path: Path) -> None:
    # B36/§V41: a snapshot without skin_table.json imports zero skins, not a failure.
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        adapter = _adapter_with_skins(tmp_path, None)
        assert import_skins(conn, adapter, snap).skins_inserted == 0
    finally:
        conn.close()


def test_import_end_to_end(tmp_path: Path) -> None:
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        adapter = _adapter_with_skins(tmp_path, _table(_DEFAULT_E0, _OUTFIT, _ALT_FORM, _TOKEN))
        result = import_skins(conn, adapter, snap)
        # The token skin is filtered; the three operator skins all resolve to base Amiya.
        assert result.skins_inserted == 3
        assert result.skins_resolved == 3
        conn.commit()
    finally:
        conn.close()


def test_non_empty_charskins_yielding_zero_skins_fails_closed(tmp_path: Path) -> None:
    # §V30: operator entries that all lack a portraitId resolve to zero skins -> fail
    # closed (a shape/id mismatch is never a silent empty gallery). The domain is
    # savepoint-isolated in the pipeline, so the combat build still continues (§V58).
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        broken = _table({**_OUTFIT, "portraitId": None}, {**_DEFAULT_E0, "portraitId": ""})
        adapter = _adapter_with_skins(tmp_path, broken)
        with pytest.raises(ImporterError, match="silent empty skin gallery"):
            import_skins(conn, adapter, snap)
    finally:
        conn.close()


def test_all_token_table_is_legitimate_empty(tmp_path: Path) -> None:
    # Token entries are dropped BEFORE the §V30 candidate count, so an all-token
    # charSkins imports zero without error (an empty operator gallery is legitimate).
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        adapter = _adapter_with_skins(tmp_path, _table(_TOKEN))
        assert import_skins(conn, adapter, snap).skins_inserted == 0
    finally:
        conn.close()


# --- pipeline: optional fail-open domain (§V88/§V58 class) --------------------


def test_skin_failure_is_isolated_combat_promotes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # §V88/§V58: a skin ImporterError (§V30 non-empty operator charSkins -> 0 rows)
    # rolls back to the savepoint (zero skins), a warning is emitted, and the MANDATORY
    # combat/operator core is STILL built -- an OPTIONAL gallery cannot fail-close §V3.
    import logging
    import shutil

    from arknights_mcp.importers.pipeline import ServerImport, build_candidate
    from arknights_mcp.sources.registry import load_source_registry

    repo_root = Path(__file__).resolve().parents[2]
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    snap = tmp_path / "en"
    shutil.copytree(fixtures / "operator" / "en", snap)
    broken = {"charSkins": {"x": {"skinId": "x", "charId": "char_002_amiya", "portraitId": None}}}
    (snap / "gamedata" / "excel" / "skin_table.json").write_text(
        _json.dumps(broken), encoding="utf-8"
    )
    path = tmp_path / "cand.sqlite"
    with caplog.at_level(logging.WARNING):
        build_candidate(
            path,
            [ServerImport("en", LocalSnapshotAdapter(snap, "en", _SOURCE_ID), _SOURCE_ID)],
            registry=load_source_registry(repo_root / "config" / "data_sources.toml"),
        )
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM operator_skins").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM operators").fetchone()[0] >= 1
    finally:
        conn.close()
    assert any("skin gallery unavailable" in r.getMessage() for r in caplog.records)


# --- repository + service read path ------------------------------------------


def test_repo_skins_degrades_when_table_absent(tmp_path: Path) -> None:
    # §V21 backward compat: an ACTIVE database built before migration 0014 has no
    # operator_skins table; the repository must degrade to no rows (the tool then takes
    # the derived-fallback path) -- never surface `no such table` as an internal_error
    # on every get_operator call.
    from arknights_mcp.db.repositories.operators import OperatorRepository

    conn = build_database(tmp_path / "cand.sqlite")
    try:
        conn.execute("DROP TABLE operator_skins")
        assert OperatorRepository(conn).skins("en", 1) == []
    finally:
        conn.close()


def test_purge_tolerates_pre_0014_database(tmp_path: Path) -> None:
    # §V21/§V20: the purge candidate is a plain copy of the ACTIVE build, which may
    # predate migration 0014 -- the takedown path must degrade (0 skins) instead of
    # crashing with `no such table: operator_skins`.
    conn, _snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        conn.execute("DROP TABLE operator_skins")
        conn.commit()
        affected = _purge_source_rows(conn, _SOURCE_ID)
        conn.commit()
        assert affected["skins"] == 0
        assert affected["operators"] == 1
    finally:
        conn.close()


def test_service_loads_skins_only_when_flag_set(tmp_path: Path) -> None:
    # §T182: load_skins is wiring-driven -- skins are queried iff the emission gate
    # will emit them; the default response never pays the query.
    from arknights_mcp.services.operators import get_operator

    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        _insert(conn, snap, _DEFAULT_E0, _OUTFIT, _ALT_FORM)
        conn.commit()
        off = get_operator(conn, server="en", game_id="char_002_amiya")
        assert off.operator is not None and off.operator.skins == ()
        on = get_operator(conn, server="en", game_id="char_002_amiya", load_skins=True)
        assert on.operator is not None
        by_id = {s.skin_id: s for s in on.operator.skins}
        assert set(by_id) == {"char_002_amiya#1", "char_002_amiya@epoque#4", "char_1001_amiya2#2"}
        # ADR 0015: is_alt_form = tmpl_id != char_id, computed in the service mapper.
        assert by_id["char_1001_amiya2#2"].is_alt_form is True
        assert by_id["char_002_amiya#1"].is_alt_form is False
        assert by_id["char_002_amiya@epoque#4"].is_buy_skin is True
    finally:
        conn.close()


# --- purge cascade -----------------------------------------------------------


def test_purge_cascades_skin_rows(tmp_path: Path) -> None:
    # §V32: purging the source removes its operator_skins rows, leaving no dangling
    # foreign key (children-before-parents; skins delete ahead of operators).
    conn, snap = _conn_with_operator(tmp_path, seed_operator=True)
    try:
        adapter = _adapter_with_skins(tmp_path, _table(_DEFAULT_E0, _OUTFIT))
        import_skins(conn, adapter, snap)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM operator_skins").fetchone()[0] == 2
        _purge_source_rows(conn, _SOURCE_ID)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM operator_skins").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
