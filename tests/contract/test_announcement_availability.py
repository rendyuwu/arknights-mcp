"""The real-build guard: the availability probe matches the shipped build.

The verdict this guard adds is only as true as the source id it probes with. A
fixture cannot check that: the unit tests seed announcements through the SAME
``source_id_for_region`` constant the probe reads, so a typo in it stays self-consistent
and invisible -- every seeded test would still pass while every real response gained a
false "this feed was never imported" limitation, on a build where both feeds ARE
imported (verify against the real corpus, never against a fixture that
matches the code).

So this reads the PROMOTED build and asserts the probe agrees with what actually sits in
``source_snapshots``. At ``2026-07-28T030224Z``: one snapshot each for
``arknights_global_official_news``@en and ``arknights_cn_official_news``@cn, backing 13
en + 22 cn announcements -- so neither region may carry an availability limitation.

Skipped when no build is promoted (the offline ``pytest -q`` gate builds fixtures, not a
full en+cn corpus); the unit tests in ``tests/unit/test_get_announcements_tool.py`` carry
the behaviour itself, so a logic edit still regresses loudly offline.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.repositories.metadata import MetadataRepository
from arknights_mcp.services.announcements import get_announcements
from arknights_mcp.sources.announcements import source_id_for_region

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "data" / "current.json"

#: The regions whose official feed is expected on a full sync (region ∈ {en,cn}).
REGIONS = ("en", "cn")


def _active_build() -> Path | None:
    """The promoted build's path, or ``None`` when nothing is promoted."""
    if not MANIFEST.is_file():
        return None
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    filename = manifest.get("database_filename")
    if not filename:
        return None
    path = REPO_ROOT / "data" / "builds" / str(filename)
    return path if path.is_file() else None


BUILD = _active_build()

pytestmark = pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)


@pytest.fixture(scope="module")
def conn() -> sqlite3.Connection:
    assert BUILD is not None
    return sqlite3.connect(f"file:{BUILD}?mode=ro", uri=True)


@pytest.mark.parametrize("region", REGIONS)
def test_probed_source_id_is_the_one_in_the_build(conn: sqlite3.Connection, region: str) -> None:
    # The typo catcher: the id the probe asks about must be an id the sync really
    # wrote. Read the truth straight from the table, never through the probe.
    source_id = source_id_for_region(region)
    assert source_id is not None
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT source_id FROM source_snapshots WHERE server = ?", (region,)
        )
    }
    assert source_id in present, f"{region}: probe asks for {source_id!r}, build has {present}"
    assert MetadataRepository(conn).has_source_snapshot(source_id, region) is True


@pytest.mark.parametrize("region", REGIONS)
def test_imported_feed_carries_no_availability_limitation(
    conn: sqlite3.Connection, region: str
) -> None:
    # Both feeds are imported on the shipped build, so the verdict must stay
    # silent: a false "never imported" caveat on every live response is the exact
    # inverse of a false absence claim and just as unfalsifiable to a client.
    result = get_announcements(conn, server=region)
    assert result.status == "ok"
    assert result.announcements, f"{region}: promoted build carries no announcement"
    assert result.limitations == ()
    assert result.provenance


@pytest.mark.parametrize("region", REGIONS)
def test_empty_window_on_the_real_build_blames_the_window_not_the_import(
    conn: sqlite3.Connection, region: str
) -> None:
    # The probe only runs once the list comes back empty, so a wrong source id is
    # invisible while rows flow (the test above passes on the rows alone) and shows up
    # exactly here: on a build where the feed IS imported, an out-of-range window must
    # be told it is the window, never "the admin never synced this feed".
    # A DAY bound: this column is day-granular, so a bound carrying a time of day would
    # also carry the widening disclosure (its own fact, pinned in
    # ``tests/contract/test_window_bound_forms.py``); the one under test here is WHICH
    # empty-reason fires, and exactly one still does.
    result = get_announcements(conn, server=region, since="2099-01-01")
    assert result.status == "ok" and result.announcements == ()
    (text,) = result.limitations
    assert "is imported for this region" in text
    assert "arknights-mcp sync" not in text


def test_probe_reports_absence_for_a_source_that_never_ran(conn: sqlite3.Connection) -> None:
    # Negative control: the probe must be capable of saying no on this very build,
    # else the two assertions above pass for the wrong reason (a probe that always
    # answers True is indistinguishable from a correct one when both feeds exist).
    repo = MetadataRepository(conn)
    assert repo.has_source_snapshot("arknights_global_official_news", "cn") is False
    assert repo.has_source_snapshot("no_such_source", "en") is False
