"""T89: the Penguin Statistics drop importer (items + stage_drops).

Covers the §T89 contract with an in-memory fake fetcher (no live network, §V52):
the penguin server -> region map (US->en, CN->cn; jp/kr dropped, §V54), the field
allowlist + recursive sanitize (§V18), per-record provenance + a penguin snapshot
row (§V17), the §V53 fetched_at/expires_at/snapshot/region stamps, the fail-closed
skip of an unresolved stage/item, and the §V30 non-empty-or-fail guard.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.penguin_drops import (
    import_penguin_drops,
    parse_items,
    parse_matrix,
    region_for_penguin_server,
)

_FETCHED = datetime(2026, 7, 20, tzinfo=UTC)
_TTL = timedelta(days=7)


class _FakeFetcher:
    """Returns preset payloads per (server, endpoint); records that it was called."""

    def __init__(self, by_server: dict[str, dict[str, Any]]) -> None:
        self._by_server = by_server
        self.calls: list[tuple[str, str | None]] = []

    def fetch(self, endpoint: str, *, server: str | None = None) -> Any:
        self.calls.append((endpoint, server))
        assert server is not None
        return self._by_server[server][endpoint]


def _seed_sources(conn: sqlite3.Connection) -> None:
    for source_id, source_type in (
        ("arknights_assets_gamedata", "game_data"),
        ("penguin_statistics", "drop_rate"),
    ):
        conn.execute(
            "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
            "source_type, regions_json, adapter_version, license_status, permission_status, "
            "redistribution_status, attribution_text, enabled, last_reviewed_at) VALUES "
            "(?, ?, 'owner', 'https://x', ?, '[\"en\"]', '1', 'reviewed', 'permitted', "
            "'code_only', 'attribution', 1, '2026-07-20')",
            (source_id, source_id, source_type),
        )


def _seed_stage(conn: sqlite3.Connection, *, region: str, game_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES "
        "(?, 'arknights_assets_gamedata', ?, '2026-07-20T00:00:00+00:00', 'h', 'imported', '1')",
        (f"{region}:ak", region),
    )
    prov = conn.execute(
        "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, record_hash, "
        "transform_version, field_policy_version) VALUES (?, 'stage_table', ?, 'rh', '1', '1')",
        (f"{region}:ak", game_id),
    ).lastrowid
    conn.execute(
        "INSERT INTO stages (server, game_id, provenance_id) VALUES (?, ?, ?)",
        (region, game_id, prov),
    )


def _payload(matrix_rows: list[dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items, "result/matrix": {"matrix": matrix_rows}}


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_sources(conn)
    return conn


# --- server -> region map (§V54) ----------------------------------------------


def test_region_map_us_cn_and_dropped_jp_kr() -> None:
    assert region_for_penguin_server("US") == "en"
    assert region_for_penguin_server("CN") == "cn"
    assert region_for_penguin_server("JP") is None
    assert region_for_penguin_server("KR") is None


def test_dropped_server_imports_nothing_and_never_fetches(tmp_path: Path) -> None:
    # §V54: a jp/kr penguin server is dropped -- no fetch, no snapshot, empty result.
    conn = _db(tmp_path)
    try:
        fetcher = _FakeFetcher({})
        result = import_penguin_drops(conn, fetcher, penguin_server="JP", fetched_at=_FETCHED)
        assert result.region is None
        assert result.snapshot_id is None
        assert (result.items_inserted, result.drops_inserted, result.drops_skipped) == (0, 0, 0)
        assert fetcher.calls == []
        penguin_snaps = conn.execute(
            "SELECT COUNT(*) FROM source_snapshots WHERE source_id = 'penguin_statistics'"
        ).fetchone()[0]
        assert penguin_snaps == 0
    finally:
        conn.close()


# --- happy path + drop_rate ---------------------------------------------------


def test_happy_path_inserts_item_and_drop(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        _seed_stage(conn, region="en", game_id="main_04-04")
        fetcher = _FakeFetcher(
            {
                "US": _payload(
                    [{"stageId": "main_04-04", "itemId": "30012", "quantity": 42, "times": 100}],
                    [{"itemId": "30012", "name": "Orirock", "rarity": 0, "itemType": "MATERIAL"}],
                )
            }
        )
        result = import_penguin_drops(conn, fetcher, penguin_server="US", fetched_at=_FETCHED)
        assert result.region == "en"
        assert (result.items_inserted, result.drops_inserted, result.drops_skipped) == (1, 0 + 1, 0)

        row = conn.execute(
            "SELECT region, quantity, times, drop_rate, snapshot_id FROM stage_drops"
        ).fetchone()
        assert row == ("en", 42, 100, 42 / 100, result.snapshot_id)
        item = conn.execute(
            "SELECT server, game_id, display_name, rarity, item_type FROM items"
        ).fetchone()
        assert item == ("en", "30012", "Orirock", "0", "MATERIAL")
    finally:
        conn.close()


# --- provenance + snapshot (§V17) ---------------------------------------------


def test_items_and_drops_carry_provenance_and_penguin_snapshot(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        _seed_stage(conn, region="en", game_id="main_04-04")
        fetcher = _FakeFetcher(
            {
                "US": _payload(
                    [{"stageId": "main_04-04", "itemId": "30012", "quantity": 42, "times": 100}],
                    [{"itemId": "30012", "name": "Orirock"}],
                )
            }
        )
        result = import_penguin_drops(conn, fetcher, penguin_server="US", fetched_at=_FETCHED)

        # A penguin snapshot row exists for the region, distinct provenance chain (§V54).
        snap = conn.execute(
            "SELECT snapshot_id, source_id, server, fetched_at FROM source_snapshots "
            "WHERE source_id = 'penguin_statistics'"
        ).fetchone()
        assert snap == (result.snapshot_id, "penguin_statistics", "en", _FETCHED.isoformat())

        # §V17: item + drop both carry a provenance row pointing at the penguin snapshot.
        item_prov = conn.execute(
            "SELECT p.snapshot_id, p.source_path FROM items i "
            "JOIN record_provenance p ON p.provenance_id = i.provenance_id"
        ).fetchone()
        assert item_prov == (result.snapshot_id, "items")
        drop_prov = conn.execute(
            "SELECT p.snapshot_id, p.source_path, p.source_record_key FROM stage_drops d "
            "JOIN record_provenance p ON p.provenance_id = d.provenance_id"
        ).fetchone()
        assert drop_prov == (result.snapshot_id, "result/matrix", "main_04-04/30012")
    finally:
        conn.close()


# --- §V53 stale/attribution stamps --------------------------------------------


def test_stage_drop_stamps_fetched_expiry_and_region(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        _seed_stage(conn, region="en", game_id="main_04-04")
        fetcher = _FakeFetcher(
            {
                "US": _payload(
                    [{"stageId": "main_04-04", "itemId": "30012", "quantity": 1, "times": 10}],
                    [{"itemId": "30012", "name": "Orirock"}],
                )
            }
        )
        import_penguin_drops(conn, fetcher, penguin_server="US", fetched_at=_FETCHED, ttl=_TTL)
        fetched, expires, region, snap = conn.execute(
            "SELECT fetched_at, expires_at, region, snapshot_id FROM stage_drops"
        ).fetchone()
        assert fetched == _FETCHED.isoformat()
        assert expires == (_FETCHED + _TTL).isoformat()
        assert region == "en"
        assert snap is not None
    finally:
        conn.close()


# --- field allowlist + recursive sanitize (§V18) ------------------------------


def test_parse_items_drops_unallowlisted_fields() -> None:
    parsed = parse_items(
        [
            {
                "itemId": "30012",
                "name": "Orirock",
                "rarity": 0,
                "itemType": "MATERIAL",
                "iconId": "sprite",
                "description": "prose that must not be stored",
            }
        ],
        region="en",
    )
    assert len(parsed) == 1
    assert set(parsed[0].provenance_record) == {"itemId", "name", "rarity", "itemType"}


# --- B46/§V59: item display_name = region-locale name, not canonical Chinese ---


def _i18n_item() -> dict[str, Any]:
    """A penguin item as the live API ships it: canonical (Chinese) ``name`` plus a
    per-locale ``name_i18n`` dict. Reading ``name`` blind mislabels an en build."""
    return {
        "itemId": "30012",
        "name": "固源岩",
        "name_i18n": {"en": "Orirock Cube", "zh": "固源岩", "ja": "初級源岩", "ko": "원암 큐브"},
        "rarity": 1,
        "itemType": "MATERIAL",
    }


def test_v59_en_item_uses_english_locale_name() -> None:
    parsed = parse_items([_i18n_item()], region="en")
    assert parsed[0].display_name == "Orirock Cube"


def test_v59_cn_item_uses_chinese_locale_name() -> None:
    parsed = parse_items([_i18n_item()], region="cn")
    assert parsed[0].display_name == "固源岩"


def test_v59_missing_locale_falls_back_to_canonical_name() -> None:
    # No name_i18n at all -> canonical name is the only label available.
    bare = parse_items([{"itemId": "30012", "name": "Orirock"}], region="en")
    assert bare[0].display_name == "Orirock"
    # name_i18n present but missing the region's locale key -> same fallback.
    partial = parse_items(
        [{"itemId": "30012", "name": "固源岩", "name_i18n": {"ja": "初級源岩"}}],
        region="en",
    )
    assert partial[0].display_name == "固源岩"


def test_v59_en_item_end_to_end_stores_english_name(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        _seed_stage(conn, region="en", game_id="main_04-04")
        fetcher = _FakeFetcher(
            {
                "US": _payload(
                    [{"stageId": "main_04-04", "itemId": "30012", "quantity": 1, "times": 10}],
                    [_i18n_item()],
                )
            }
        )
        import_penguin_drops(conn, fetcher, penguin_server="US", fetched_at=_FETCHED)
        name = conn.execute("SELECT display_name FROM items").fetchone()[0]
        assert name == "Orirock Cube"
    finally:
        conn.close()


def test_item_name_control_chars_sanitized(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        _seed_stage(conn, region="en", game_id="main_04-04")
        fetcher = _FakeFetcher(
            {
                "US": _payload(
                    [{"stageId": "main_04-04", "itemId": "30012", "quantity": 1, "times": 10}],
                    # U+202E RIGHT-TO-LEFT OVERRIDE (a Cf bidi control) must be stripped.
                    [{"itemId": "30012", "name": "Ori‮rock"}],
                )
            }
        )
        import_penguin_drops(conn, fetcher, penguin_server="US", fetched_at=_FETCHED)
        name = conn.execute("SELECT display_name FROM items").fetchone()[0]
        assert "‮" not in name
        assert name == "Orirock"
    finally:
        conn.close()


# --- region never mixed (§V54/§V5) --------------------------------------------


def test_us_import_labels_every_row_en(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        _seed_stage(conn, region="en", game_id="main_04-04")
        _seed_stage(conn, region="cn", game_id="main_04-04")  # a cn stage must stay untouched
        fetcher = _FakeFetcher(
            {
                "US": _payload(
                    [{"stageId": "main_04-04", "itemId": "30012", "quantity": 1, "times": 10}],
                    [{"itemId": "30012", "name": "Orirock"}],
                )
            }
        )
        import_penguin_drops(conn, fetcher, penguin_server="US", fetched_at=_FETCHED)
        regions = {r for (r,) in conn.execute("SELECT DISTINCT region FROM stage_drops")}
        assert regions == {"en"}
        item_servers = {s for (s,) in conn.execute("SELECT DISTINCT server FROM items")}
        assert item_servers == {"en"}
    finally:
        conn.close()


# --- fail-closed skip of an unresolved stage/item -----------------------------


def test_unresolved_stage_or_item_skipped_not_fabricated(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        _seed_stage(conn, region="en", game_id="main_04-04")
        fetcher = _FakeFetcher(
            {
                "US": _payload(
                    [
                        # resolvable
                        {"stageId": "main_04-04", "itemId": "30012", "quantity": 3, "times": 9},
                        # stage absent
                        {"stageId": "main_99-99", "itemId": "30012", "quantity": 1, "times": 9},
                        # item absent (not in the items payload)
                        {"stageId": "main_04-04", "itemId": "99999", "quantity": 1, "times": 9},
                    ],
                    [{"itemId": "30012", "name": "Orirock"}],
                )
            }
        )
        result = import_penguin_drops(conn, fetcher, penguin_server="US", fetched_at=_FETCHED)
        assert result.drops_inserted == 1
        assert result.drops_skipped == 2
        assert conn.execute("SELECT COUNT(*) FROM stage_drops").fetchone()[0] == 1
    finally:
        conn.close()


# --- §V30 non-empty-or-fail ---------------------------------------------------


def test_nonempty_matrix_zero_resolved_fails_closed(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        # No stage seeded: every matrix row is unresolvable -> zero drops -> fail closed.
        fetcher = _FakeFetcher(
            {
                "US": _payload(
                    [{"stageId": "main_04-04", "itemId": "30012", "quantity": 1, "times": 10}],
                    [{"itemId": "30012", "name": "Orirock"}],
                )
            }
        )
        with pytest.raises(ImporterError, match="silent empty drop build"):
            import_penguin_drops(conn, fetcher, penguin_server="US", fetched_at=_FETCHED)
    finally:
        conn.close()


# --- §V33 duplicate (stage, item) ---------------------------------------------


def test_duplicate_stage_item_maps_to_importer_error(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        _seed_stage(conn, region="en", game_id="main_04-04")
        fetcher = _FakeFetcher(
            {
                "US": _payload(
                    [
                        {"stageId": "main_04-04", "itemId": "30012", "quantity": 1, "times": 10},
                        {"stageId": "main_04-04", "itemId": "30012", "quantity": 2, "times": 10},
                    ],
                    [{"itemId": "30012", "name": "Orirock"}],
                )
            }
        )
        with pytest.raises(ImporterError, match="duplicates"):
            import_penguin_drops(conn, fetcher, penguin_server="US", fetched_at=_FETCHED)
    finally:
        conn.close()


# --- parse-shape guards -------------------------------------------------------


def test_parse_matrix_requires_matrix_key() -> None:
    with pytest.raises(ImporterError, match="matrix"):
        parse_matrix({})


def test_parse_items_requires_array() -> None:
    with pytest.raises(ImporterError, match="array"):
        parse_items({"itemId": "x"}, region="en")
