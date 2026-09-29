"""Real-corpus guard -- an event title finds its stages.

The bug this replaces was invisible to every test that ran. An earlier sweep saw
``search_stages("Lone Trail") -> not_found``, read it as a key-name error, moved the
zone-name read from ``zoneName`` to ``zoneNameSecond``, and proved the fix with a
fixture that invented a zone literally named "Lone Trail". Both halves were wrong: the
real zones of that event are named "The Coming of The Future" / "The Lingering of the
Past" / "The Pursuing of the Present", and the string the client types occurs NOWHERE in
``zone_table``. It is in ``activity_table``, a file the adapter never fetched.

So the guard cannot be "a name arrived" (true of 373 of 429 real zones while the bug was
live) and cannot use a fixture at all. It is a round trip over the REAL tables: titles
read from upstream, imported through the production importer, indexed by the production
index builder, and queried through the same service the MCP tool calls.

CI-only: needs network, gated behind ``ARKMCP_LIVE_UPSTREAM`` like the other live-upstream
guards. Nothing fetched is persisted (code-only distribution).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from tests.support import (
    LIVE_UPSTREAM_SKIP_REASON,
    arknights_assets_base_url,
    fetch_upstream_bytes,
    live_upstream_disabled,
)

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.search_index import build_search_index
from arknights_mcp.importers.stages import import_stages, parse_activity_titles
from arknights_mcp.services.search import search_stages
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

pytestmark = pytest.mark.skipif(live_upstream_disabled(), reason=LIVE_UPSTREAM_SKIP_REASON)

BASE_URL = arknights_assets_base_url("en")

#: Event titles TRANSCRIBED from the real ``activity_table`` -- the literal strings a
#: client would type. Never invent one: an invented title is exactly how the earlier test
#: passed against a corpus that did not contain it. Each is asserted to still exist
#: upstream before it is used as a query, so a renamed event fails loudly rather than
#: silently weakening the guard.
SEARCHED_TITLES = (
    "Lone Trail",
    "Babel",
    "First of A Thousand Autumns",
    "Dossoles Holiday",
)

#: Floors on the real coverage, so the guard cannot pass vacuously on a corpus that
#: stopped exercising it. At the pinned commit: 266 of 429 EN zones carry a title, 120
#: distinct titles, 2313 of 3264 EN stages reachable by one.
MIN_TITLED_ZONES = 250
MIN_DISTINCT_TITLES = 100
MIN_TITLED_STAGES = 2000

#: Zone families that legitimately have no activity row of their own (163 EN zones at
#: the pinned commit): mainline + its retro/permanent re-hosts, weekly supply, guide,
#: annihilation, Stationary Security Service, Integrated Strategies. A zone appearing
#: outside these has lost its title to a source change, not to source design.
TITLE_LESS_ZONE_PREFIXES = (
    "main_",
    "permanent_",
    "weekly_",
    "guide_",
    "camp_",
    "tower_",
    "rogue_",
)


def _fetch_table(relative_path: str) -> Any:
    """Fetch + parse one pinned upstream table; never written to disk."""
    return json.loads(fetch_upstream_bytes(f"{BASE_URL}/{relative_path}").decode("utf-8"))


@pytest.fixture(scope="module")
def zone_table() -> Any:
    return _fetch_table("gamedata/excel/zone_table.json")


@pytest.fixture(scope="module")
def activity_table() -> Any:
    return _fetch_table("gamedata/excel/activity_table.json")


@pytest.fixture(scope="module")
def stage_table() -> Any:
    return _fetch_table("gamedata/excel/stage_table.json")


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


@pytest.fixture(scope="module")
def conn(
    tmp_path_factory: pytest.TempPathFactory,
    zone_table: Any,
    activity_table: Any,
    stage_table: Any,
) -> sqlite3.Connection:
    """The real EN zones + stages, imported and indexed by the production code.

    Level files are deliberately not staged: a stage that names an absent level is
    imported with no map (a logged warning), and this guard is about names. Everything
    else -- allowlist, sanitize, the activity join, the FTS document -- is the shipping
    path.
    """
    tmp_path = tmp_path_factory.mktemp("zone_event")
    root = tmp_path / "en" / "gamedata" / "excel"
    root.mkdir(parents=True)
    (root / "zone_table.json").write_text(json.dumps(zone_table), encoding="utf-8")
    (root / "stage_table.json").write_text(json.dumps(stage_table), encoding="utf-8")
    (root / "activity_table.json").write_text(json.dumps(activity_table), encoding="utf-8")

    db_path = tmp_path / "candidate.sqlite"
    writable = build_database(db_path)
    snapshot_id = _seed_snapshot(writable)
    import_stages(writable, LocalSnapshotAdapter(tmp_path / "en", server="en"), snapshot_id)
    build_search_index(writable)
    writable.commit()
    writable.close()
    return open_read_only(db_path)


def test_searched_titles_are_real(activity_table: Any) -> None:
    """Non-degeneracy: every query below is a title upstream actually publishes."""
    published = {
        entry["name"]
        for entry in activity_table["basicInfo"].values()
        if isinstance(entry, dict) and entry.get("name")
    }
    for title in SEARCHED_TITLES:
        assert any(title in name for name in published), f"{title!r} is no longer a real event"


def test_the_searched_title_is_absent_from_zone_table(zone_table: Any) -> None:
    """The assertion the guard needed: the title is not in the file it fixed.

    Had this been asserted then, "rename the zone-name key" would have been visibly
    insufficient -- no key of ``zone_table`` carries the string at all. If upstream ever
    moves titles into ``zone_table``, this fails and the activity join can be dropped.
    """
    zone_strings = [
        value
        for entry in zone_table["zones"].values()
        if isinstance(entry, dict)
        for value in entry.values()
        if isinstance(value, str)
    ]
    for title in SEARCHED_TITLES:
        matches = [text for text in zone_strings if title.lower() in text.lower()]
        assert not matches, f"{title!r} now appears in zone_table: {matches[:3]}"


def test_activity_table_covers_the_real_corpus(activity_table: Any) -> None:
    titles = parse_activity_titles(activity_table)
    assert len(titles) >= MIN_TITLED_ZONES
    assert len({parsed.display_name for parsed in titles.values()}) >= MIN_DISTINCT_TITLES


def test_title_less_zones_are_the_known_modes(zone_table: Any, activity_table: Any) -> None:
    """The zones with no title are a source-design set, not a coverage failure."""
    titles = parse_activity_titles(activity_table)
    orphans = [zone_id for zone_id in zone_table["zones"] if zone_id not in titles]
    unexpected = [
        zone_id for zone_id in orphans if not zone_id.startswith(TITLE_LESS_ZONE_PREFIXES)
    ]
    assert not unexpected, f"zones lost their event title: {unexpected[:10]}"


def test_real_titles_round_trip_through_the_search_service(conn: sqlite3.Connection) -> None:
    """The query returns that event's stages, through the real service."""
    for title in SEARCHED_TITLES:
        result = search_stages(conn, query=title, server="en", limit=50)
        assert result.status == "ok", f"{title!r} matched nothing"
        matched = [hit for hit in result.hits if hit.event_name and title in hit.event_name]
        assert matched, f"{title!r} returned stages but none of that event: {result.hits[:3]}"


def test_the_title_is_what_matched_not_the_zone_subtitle(conn: sqlite3.Connection) -> None:
    """The hit is attributable, and it is genuinely the new alias that produced it."""
    for title in SEARCHED_TITLES:
        hits = search_stages(conn, query=title, server="en", limit=50).hits
        hit = next(h for h in hits if h.event_name and title in h.event_name)
        assert title.lower() not in (hit.display_name or "").lower()
        assert title.lower() not in (hit.zone_display_name or "").lower()


def test_the_index_reaches_most_stages_by_event(conn: sqlite3.Connection) -> None:
    """Coverage floor on the shipped shape: the join is wired for the whole corpus."""
    titled_stages = conn.execute(
        "SELECT COUNT(*) FROM stages s JOIN zones z ON z.zone_pk = s.zone_pk "
        "AND z.server = s.server WHERE s.server = 'en' AND z.event_name IS NOT NULL"
    ).fetchone()[0]
    assert titled_stages >= MIN_TITLED_STAGES
