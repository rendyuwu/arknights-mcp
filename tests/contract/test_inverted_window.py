"""The real-build proof that rejecting an inverted window withholds nothing.

An impossible window can be answered either way -- a typed ``invalid_input`` or an
``ok`` plus an explicit limitation. This task ships the rejection, and a rejection needs a
stronger warrant than a limitation does: it must be impossible for it to swallow a row a
client would otherwise have received. The argument is that strings are totally ordered, so
``since > until`` leaves no value able to satisfy both ``column >= :since`` and
``column <= :until`` -- but that argument is about the SQL the repositories really run
against the dates really stored, which only the promoted build can settle. A fixture
cannot: it holds a handful of dates chosen by the same author as the guard.

So this drives the REPOSITORY layer (below the guard, so the window still executes) over
inverted pairs drawn from each region's OWN stored dates, and asserts every one of them
returns zero rows. Then the same pairs are shown to be rejected at the service, and a
non-inverted pair over the identical date domain is shown to still return rows -- otherwise
"zero rows either way" would prove nothing.

Skipped when no build is promoted; ``tests/unit/test_window_bounds.py`` carries the
behaviour itself, so a logic edit still regresses loudly offline.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.repositories.announcements import AnnouncementRepository
from arknights_mcp.db.repositories.banners import BannerRepository
from arknights_mcp.services.announcements import get_announcements
from arknights_mcp.services.banners import get_banners

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "data" / "current.json"

REGIONS = ("en", "cn")

#: The filed pair.
INVERTED = ("2026-07-01", "2026-06-01")

#: How many stored dates to draw the inverted pairs from. Evenly spaced over the region's
#: full ordered date domain, so the pairs span the whole corpus rather than one end of it;
#: N dates yield N*(N-1)/2 inverted pairs, each one a real query.
_SAMPLE = 12


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
    connection = sqlite3.connect(f"file:{BUILD}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _stored_dates(conn: sqlite3.Connection, sql: str, region: str) -> list[str]:
    """The region's distinct stored bound values, ascending -- the real date domain."""
    return [str(row[0]) for row in conn.execute(sql, (region,)) if row[0] is not None]


def _sample(values: list[str]) -> list[str]:
    """Evenly spaced picks across the ordered domain (deterministic, no RNG)."""
    if len(values) <= _SAMPLE:
        return values
    step = len(values) / _SAMPLE
    return [values[int(i * step)] for i in range(_SAMPLE)]


_ANNOUNCEMENT_DATES = "SELECT DISTINCT date FROM announcements WHERE region = ? ORDER BY date"
_BANNER_DATES = "SELECT DISTINCT open_time FROM banners WHERE region = ? ORDER BY open_time"


@pytest.mark.parametrize("region", REGIONS)
def test_every_inverted_pair_of_real_announcement_dates_matches_nothing(
    conn: sqlite3.Connection, region: str
) -> None:
    dates = _sample(_stored_dates(conn, _ANNOUNCEMENT_DATES, region))
    assert len(dates) >= 2, f"{region}: promoted build carries too few announcement dates"
    repo = AnnouncementRepository(conn)
    pairs = 0
    for i, until in enumerate(dates):
        for since in dates[i + 1 :]:
            # since is strictly LATER than until: the window is empty by construction, so
            # the guard's rejection cannot be withholding anything.
            assert repo.announcements_for_region(region, since=since, until=until) == []
            pairs += 1
    assert pairs >= 1


@pytest.mark.parametrize("region", REGIONS)
def test_every_inverted_pair_of_real_banner_dates_matches_nothing(
    conn: sqlite3.Connection, region: str
) -> None:
    dates = _sample(_stored_dates(conn, _BANNER_DATES, region))
    assert len(dates) >= 2, f"{region}: promoted build carries too few banner open times"
    repo = BannerRepository(conn)
    for i, until in enumerate(dates):
        for since in dates[i + 1 :]:
            assert repo.banners_for_region(region, since=since, until=until) == []


@pytest.mark.parametrize("region", REGIONS)
def test_the_same_domain_still_returns_rows_the_right_way_round(
    conn: sqlite3.Connection, region: str
) -> None:
    # Non-degenerate control: the zero above is the INVERSION's doing, not an
    # unreachable window or a broken query -- the widest pair over the identical stored
    # values returns rows in both domains.
    announcement_dates = _stored_dates(conn, _ANNOUNCEMENT_DATES, region)
    banner_dates = _stored_dates(conn, _BANNER_DATES, region)
    assert AnnouncementRepository(conn).announcements_for_region(
        region, since=announcement_dates[0], until=announcement_dates[-1]
    )
    assert BannerRepository(conn).banners_for_region(
        region, since=banner_dates[0], until=banner_dates[-1]
    )


@pytest.mark.parametrize("region", REGIONS)
def test_the_filed_pair_is_rejected_on_the_real_build(
    conn: sqlite3.Connection, region: str
) -> None:
    # The filed query, on the build that shipped it. Before this task both services
    # answered it ``ok`` with an empty collection -- get_banners with no limitation at all.
    since, until = INVERTED
    for service in (get_announcements, get_banners):
        with pytest.raises(ValueError, match="can match nothing"):
            service(conn, server=region, since=since, until=until)  # type: ignore[operator]


@pytest.mark.parametrize("region", REGIONS)
def test_a_real_window_still_answers_on_the_real_build(
    conn: sqlite3.Connection, region: str
) -> None:
    # The guard sits in front of the query, so it must not have cost the tools their
    # ordinary windowed answer on the corpus they serve.
    dates = _stored_dates(conn, _ANNOUNCEMENT_DATES, region)
    result = get_announcements(conn, server=region, since=dates[0], until=dates[-1])
    assert result.status == "ok" and result.announcements
    banner_dates = _stored_dates(conn, _BANNER_DATES, region)
    banners = get_banners(conn, server=region, since=banner_dates[0], until=banner_dates[-1])
    assert banners.status == "ok" and banners.banners
