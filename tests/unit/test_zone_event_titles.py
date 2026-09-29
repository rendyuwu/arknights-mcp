"""The event TITLE is imported and searchable.

The original report read "search_stages('Lone Trail') -> not_found" as a key-name error
and moved the zone name read from ``zoneName`` to ``zoneNameSecond``. That could never
have fixed it:
``zoneNameSecond`` is the sub-zone SUBTITLE, and the Lone Trail zones ARE named -- "The
Coming of The Future" / "The Lingering of the Past" / "The Pursuing of the Present". The
string "Lone Trail" occurs nowhere in ``zone_table``. It lives in ``activity_table``
(``basicInfo[<actId>].name``), reached through that file's ``zoneToActivity`` map, and
that file was never fetched. The regression test passed because its fixture invented
a zone literally named "Lone Trail" and then asserted its own invention.

So every value below is TRANSCRIBED from the pinned upstream snapshot (413a81a3, en) --
never invented -- and the assertions run through the same service the MCP tool calls.
The real-corpus round trip over the WHOLE table is the guard proper and lives in
``tests/contract/test_zone_event_search.py``; this module pins the wiring.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.search_index import build_search_index
from arknights_mcp.importers.stages import (
    import_stages,
    parse_activity_titles,
    parse_zones,
)
from arknights_mcp.mcp.tools.stage import _stage_to_dict
from arknights_mcp.services.search import search_stages
from arknights_mcp.services.stages import get_stage
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

# --- transcribed from the pinned en snapshot ---------------------------------
# zone_table: the two zone shapes that matter. An event zone carries a SUBTITLE and
# nothing else name-like (zoneNameFirst is null here -- upstream fills it only for the
# 19 mainline "Episode N" rows); a CLIMB_TOWER zone carries no name at all.
ZONE_TABLE: dict[str, Any] = {
    "zones": {
        "act25sre_zone1": {
            "zoneID": "act25sre_zone1",
            "type": "ACTIVITY",
            "zoneNameFirst": None,
            "zoneNameSecond": "The Coming of The Future",
            "zoneNameTitleEx": None,
        },
        "act25sre_zone2": {
            "zoneID": "act25sre_zone2",
            "type": "ACTIVITY",
            "zoneNameFirst": None,
            "zoneNameSecond": "The Lingering of the Past",
            "zoneNameTitleEx": None,
        },
        "tower_n_01": {
            "zoneID": "tower_n_01",
            "type": "CLIMB_TOWER",
            "zoneNameFirst": None,
            "zoneNameSecond": None,
            "zoneNameTitleEx": None,
        },
    }
}

# activity_table: the title, plus the schedule/shop/medal fields the allowlist drops.
ACTIVITY_TABLE: dict[str, Any] = {
    "basicInfo": {
        "act25sre": {
            "id": "act25sre",
            "type": "TYPE_ACT25SIDE",
            "displayType": "NONE",
            "name": "Lone Trail - Rerun",
            "startTime": 1732208400,
            "endTime": 1733050799,
            "templateShopId": "shop_act25sre",
            "medalGroupId": "medalGroupActivity25side",
        }
    },
    "zoneToActivity": {
        "act25sre_zone1": "act25sre",
        "act25sre_zone2": "act25sre",
        "act25sre_zone3": "act25sre",
    },
}

# stage_table: two real Lone Trail stages and one CLIMB_TOWER stage whose zone belongs
# to no event. levelId is dropped from the transcription -- these tests are about names,
# and a referenced-but-absent level file only logs a warning here.
STAGE_TABLE: dict[str, Any] = {
    "stages": {
        "act25side_01": {
            "stageId": "act25side_01",
            "code": "CW-1",
            "name": "Dense Fog",
            "zoneId": "act25sre_zone1",
            "stageType": "ACTIVITY",
            "difficulty": "NORMAL",
            "apCost": 9,
        },
        "act25side_02": {
            "stageId": "act25side_02",
            "code": "CW-2",
            "name": "Without a Trace",
            "zoneId": "act25sre_zone1",
            "stageType": "ACTIVITY",
            "difficulty": "NORMAL",
            "apCost": 9,
        },
        "lt_01_01": {
            "stageId": "lt_01_01",
            "code": "LT-1",
            "name": "Mine #5",
            "zoneId": "tower_n_01",
            "stageType": "CLIMB_TOWER",
            "difficulty": "NORMAL",
            "apCost": 0,
        },
    }
}


def _write(root: Path, relative: str, payload: Any) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_snapshot(conn: sqlite3.Connection) -> str:
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "local_snapshot",
            "Local",
            "op",
            "local://x",
            "t",
            '["en"]',
            "1",
            "l",
            "p",
            "r",
            "a",
            1,
            "2026-07-28",
        ),
    )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES (?,?,?,?,?,?,?)",
        ("en:snap", "local_snapshot", "en", "2026-07-28T00:00:00+00:00", "mh", "imported", "11"),
    )
    conn.commit()
    return "en:snap"


def _build(tmp_path: Path, *, with_activity_table: bool = True) -> Path:
    """Import the transcribed slice and index it, exactly as the pipeline would."""
    root = tmp_path / "en"
    _write(root, "gamedata/excel/zone_table.json", ZONE_TABLE)
    _write(root, "gamedata/excel/stage_table.json", STAGE_TABLE)
    if with_activity_table:
        _write(root, "gamedata/excel/activity_table.json", ACTIVITY_TABLE)
    db_path = tmp_path / "cand.sqlite"
    conn = build_database(db_path)
    snapshot_id = _seed_snapshot(conn)
    import_stages(conn, LocalSnapshotAdapter(root, server="en"), snapshot_id)
    build_search_index(conn)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return open_read_only(_build(tmp_path))


# --- what the source actually says -------------------------------------------


def test_zone_name_is_the_subtitle_not_the_event_title() -> None:
    """The zone IS named -- just not the name a client types.

    This is the assertion the earlier fix needed and never made. Its test checked that *some*
    non-null name arrived, which is true of 373 of 429 real zones, so it passed while
    the searched-for string was absent from the entire corpus.
    """
    zones = {z.game_id: z.display_name for z in parse_zones(ZONE_TABLE)}
    assert zones["act25sre_zone1"] == "The Coming of The Future"
    assert zones["tower_n_01"] is None
    assert not any(name == "Lone Trail - Rerun" for name in zones.values())
    assert "Lone Trail" not in json.dumps(ZONE_TABLE)


def test_activity_table_supplies_the_event_title() -> None:
    titles = parse_activity_titles(ACTIVITY_TABLE)
    assert titles["act25sre_zone1"].display_name == "Lone Trail - Rerun"
    assert titles["act25sre_zone1"].game_id == "act25sre"
    # Every zone of the event resolves to the same activity record, not a copy each.
    assert titles["act25sre_zone2"] is titles["act25sre_zone1"]


def test_activity_allowlist_keeps_id_and_name_only() -> None:
    """The schedule / shop / medal fields ride the same record and stay out."""
    record = parse_activity_titles(ACTIVITY_TABLE)["act25sre_zone1"].provenance_record
    assert set(record) == {"id", "name"}


def test_zone_with_no_activity_has_no_title() -> None:
    """44 real en zones (annihilation / tower / IS / guide) have no event."""
    assert "tower_n_01" not in parse_activity_titles(ACTIVITY_TABLE)


def test_silent_empty_title_map_fails_closed() -> None:
    """An activity table that resolves NO zone is the failure state itself.

    Nothing downstream looks wrong when it happens -- every zone still has its
    subtitle -- so the importer refuses the snapshot instead of shipping a search
    index that silently cannot answer an event name. Routed through the shared
    guard.
    """
    broken = {
        "basicInfo": ACTIVITY_TABLE["basicInfo"],
        "zoneToActivity": {"act25sre_zone1": "act_that_is_not_in_basic_info"},
    }
    with pytest.raises(ImporterError, match="none resolved to a zone event title"):
        parse_activity_titles(broken)


def test_missing_top_level_keys_fail_closed() -> None:
    with pytest.raises(ImporterError, match="basicInfo"):
        parse_activity_titles({"basicInfo": {"act25sre": {"id": "act25sre"}}})


# --- the round trip, through the service the tool calls -----------------------


def test_event_title_finds_that_events_stages(conn: sqlite3.Connection) -> None:
    """The originally filed query: an event title returns the stages of that event."""
    result = search_stages(conn, query="Lone Trail", server="en")
    assert result.status == "ok"
    assert {hit.game_id for hit in result.hits} == {"act25side_01", "act25side_02"}


def test_alias_driven_hit_carries_both_names(conn: sqlite3.Connection) -> None:
    """The hit says WHY it came back, and the two names stay distinct."""
    hit = next(h for h in search_stages(conn, query="Lone Trail", server="en").hits)
    assert hit.event_name == "Lone Trail - Rerun"
    assert hit.zone_display_name == "The Coming of The Future"


def test_zone_subtitle_still_matches(conn: sqlite3.Connection) -> None:
    """The subtitle remains a stage alias."""
    hits = search_stages(conn, query="The Coming of The Future", server="en").hits
    assert {hit.game_id for hit in hits} == {"act25side_01", "act25side_02"}


def test_stage_outside_any_event_is_not_matched_by_the_title(conn: sqlite3.Connection) -> None:
    hits = search_stages(conn, query="Lone Trail", server="en").hits
    assert all(hit.game_id != "lt_01_01" for hit in hits)


def test_get_stage_emits_the_event_title(conn: sqlite3.Connection) -> None:
    result = get_stage(conn, server="en", game_id="act25side_01")
    assert result.stage is not None
    emitted = _stage_to_dict(result.stage)
    assert emitted["event_name"] == "Lone Trail - Rerun"
    assert emitted["zone_display_name"] == "The Coming of The Future"


def test_get_stage_omits_the_key_when_the_zone_has_no_event(conn: sqlite3.Connection) -> None:
    """Omitted, never a bare null -- a title-less zone is a real, common state."""
    result = get_stage(conn, server="en", game_id="lt_01_01")
    assert result.stage is not None
    emitted = _stage_to_dict(result.stage)
    assert "event_name" not in emitted
    assert emitted["zone_game_id"] == "tower_n_01"


def test_snapshot_without_activity_table_still_imports(tmp_path: Path) -> None:
    """The table is tolerated-absent -- zones simply carry no title."""
    conn = open_read_only(_build(tmp_path, with_activity_table=False))
    # The search still runs and still finds nothing, but a zero-hit set query
    # is an ``ok`` with an empty result -- the typed reason is what proves nothing matched
    # (rather than the region index being unavailable, gated separately).
    untitled = search_stages(conn, query="Lone Trail", server="en")
    assert untitled.status == "ok"
    assert untitled.hits == ()
    assert untitled.empty_reason == "no_match"
    assert search_stages(conn, query="Dense Fog", server="en").status == "ok"


def test_read_path_degrades_on_a_build_predating_migration_0015(tmp_path: Path) -> None:
    """An ACTIVE build made before the column must not crash the read path."""
    db_path = _build(tmp_path)
    writable = sqlite3.connect(db_path)
    writable.execute("ALTER TABLE zones DROP COLUMN event_name")
    writable.commit()
    writable.close()

    conn = open_read_only(db_path)
    hit = next(h for h in search_stages(conn, query="Dense Fog", server="en").hits)
    assert hit.event_name is None
    assert hit.zone_display_name == "The Coming of The Future"
    result = get_stage(conn, server="en", game_id="act25side_01")
    assert result.stage is not None and result.stage.event_name is None
