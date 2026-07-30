"""T212: the real-build proof that a window's answer no longer depends on its NOTATION.

§V116 (d) is why this file exists. The harm B163 filed is a COLLATION -- bound text sorted
against the dates really stored -- so a fixture cannot witness it: its handful of dates are
chosen by the same author as the guard, in whatever notation that author typed. Only the
promoted build carries the two real stored forms (a day-granular ``announcements.date`` and
a full ``banners.open_time`` timestamp with a ``+00:00`` offset) and enough rows for a
wrong collation to show up as a wrong COUNT.

Measured here before the fix, on ``2026-07-30T010030Z-en-cn``:

* ``since="20260101"`` -> 0 announcements + 0 banners in both regions, where
  ``since="2026-01-01"`` -> 14 en / 22 cn announcements + 44 en / 45 cn banners;
* ``until="20260101"`` -> the FULL corpus (14/22 + 389/437), i.e. the bound was ignored
  outright, including cn announcements out to ``2026-09-19``;
* the intra-day pair ``since="<day>T00:00:00", until="<day>"`` was rejected as impossible
  on all 359 en / 424 cn banner-open days, while the SQL would have answered every one.

So each notation is driven through the SERVICE (the layer that renders) and its row set is
compared against the extended-ISO form of the SAME instant. Equality is the assertion;
non-degenerate controls keep it from being satisfied by two empty answers.

Skipped when no build is promoted; ``tests/unit/test_window_bound_form.py`` carries the
behaviour itself, so a logic edit still regresses loudly offline.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.services.announcements import get_announcements
from arknights_mcp.services.banners import get_banners

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "data" / "current.json"

REGIONS = ("en", "cn")

#: Notations of ONE instant, each paired with the extended-ISO form it must agree with.
#: Both a date-only and a datetime instant are covered, because the two stored columns
#: have different granularities and a bound has to be placed correctly against each.
EQUIVALENT_NOTATIONS: tuple[tuple[str, str], ...] = (
    ("20260101", "2026-01-01"),
    ("2026-W01-4", "2026-01-01"),
    ("20260401", "2026-04-01"),
    ("20260701T000000", "2026-07-01T00:00:00"),
    ("2026-07-01 00:00:00", "2026-07-01T00:00:00"),
    ("2026-07-01T00:00:00Z", "2026-07-01T00:00:00"),
    ("2026-07-01T07:00:00+07:00", "2026-07-01T00:00:00"),
)


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


def _announcement_ids(conn: sqlite3.Connection, region: str, **bounds: str) -> list[str]:
    result = get_announcements(conn, server=region, page_size=100, **bounds)
    return [a.announce_id for a in result.announcements]


def _banner_ids(conn: sqlite3.Connection, region: str, **bounds: str) -> list[str]:
    result = get_banners(conn, server=region, page_size=100, **bounds)
    return [b.game_id for b in result.banners]


def _stored_days(conn: sqlite3.Connection, sql: str, region: str) -> list[str]:
    """The region's distinct stored bound values, ascending -- the real date domain."""
    return [str(row[0]) for row in conn.execute(sql, (region,)) if row[0] is not None]


_ANNOUNCEMENT_DATES = "SELECT DISTINCT date FROM announcements WHERE region = ? ORDER BY date"
_BANNER_DAYS = "SELECT DISTINCT substr(open_time, 1, 10) FROM banners WHERE server = ? ORDER BY 1"


# --- the two stored forms this build really carries ----------------------------


def test_the_two_windowed_columns_really_have_different_forms(conn: sqlite3.Connection) -> None:
    # The premise of §V116 (b), pinned rather than assumed: one shared validator serves a
    # day-granular column and a full-timestamp column, so "canonical" cannot be one string
    # shape for both. If a future import changes either form, the coarsening rule above it
    # must be revisited -- and this is the assertion that will say so.
    def distinct(sql: str) -> set[object]:
        return {row[0] for row in conn.execute(sql)}

    date_lengths = distinct("SELECT DISTINCT length(date) FROM announcements")
    open_lengths = distinct("SELECT DISTINCT length(open_time) FROM banners")
    offsets = distinct("SELECT DISTINCT substr(open_time, 20) FROM banners")
    assert date_lengths == {10}, f"announcements.date is not day-granular: {date_lengths}"
    assert open_lengths == {25}, f"banners.open_time is not a full timestamp: {open_lengths}"
    assert offsets == {"+00:00"}, f"banner open_time offsets are not all UTC: {offsets}"


# --- notation independence, both bounds, both tools, both regions --------------


@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize(("written", "extended"), EQUIVALENT_NOTATIONS)
def test_lower_bound_answer_is_notation_independent(
    conn: sqlite3.Connection, region: str, written: str, extended: str
) -> None:
    assert _announcement_ids(conn, region, since=written) == _announcement_ids(
        conn, region, since=extended
    )
    assert _banner_ids(conn, region, since=written) == _banner_ids(conn, region, since=extended)


@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize(("written", "extended"), EQUIVALENT_NOTATIONS)
def test_upper_bound_answer_is_notation_independent(
    conn: sqlite3.Connection, region: str, written: str, extended: str
) -> None:
    assert _announcement_ids(conn, region, until=written) == _announcement_ids(
        conn, region, until=extended
    )
    assert _banner_ids(conn, region, until=written) == _banner_ids(conn, region, until=extended)


# --- non-degenerate controls (§V96): the equality above is not two empties -----


@pytest.mark.parametrize("region", REGIONS)
def test_the_shared_bound_actually_selects_on_this_build(
    conn: sqlite3.Connection, region: str
) -> None:
    # B163 arm 1 was an EMPTY answer, so "both notations agree" must be shown to agree on a
    # NON-EMPTY one; B163 arm 2 was the whole corpus, so the upper bound must be shown to
    # still exclude something.
    wide_open_banners = _banner_ids(conn, region)
    assert _banner_ids(conn, region, since="20260101"), "the lower bound returns nothing at all"
    bounded = _banner_ids(conn, region, until="20260101")
    assert bounded != wide_open_banners, "the upper bound excludes nothing (it is being ignored)"
    assert _announcement_ids(conn, region, since="20260101"), "no announcement is in range"


@pytest.mark.parametrize("region", REGIONS)
def test_a_basic_format_upper_bound_excludes_the_dates_after_it(
    conn: sqlite3.Connection, region: str
) -> None:
    # The false-inclusion arm, stated as the property it violated: every row returned under
    # an upper bound must actually fall on or before that bound's own day.
    listed = get_announcements(conn, server=region, until="20260401", page_size=100)
    for announcement in listed.announcements:
        date = announcement.date
        assert date is not None and date[:10] <= "2026-04-01", (
            f"{announcement.announce_id}: {date} is after the bound"
        )
    for banner in get_banners(conn, server=region, until="20260401", page_size=100).banners:
        assert banner.open_time is not None
        assert banner.open_time[:10] <= "2026-04-01", f"{banner.game_id}: {banner.open_time}"


# --- the intra-day window the guard used to reject (B163 arm 3) ----------------


@pytest.mark.parametrize("region", REGIONS)
def test_every_real_banner_day_answers_as_an_intra_day_window(
    conn: sqlite3.Connection, region: str
) -> None:
    # T201's guard rejected this pair on every one of these days; the SQL answers each one,
    # so the rejection was withholding rows. Driven over the region's OWN stored days: a
    # chosen pair could miss the notation that breaks, a whole domain cannot.
    days = _stored_days(conn, _BANNER_DAYS, region)
    assert len(days) >= 2, f"{region}: promoted build carries too few banner days"
    for day in days:
        ids = _banner_ids(conn, region, since=f"{day}T00:00:00", until=day)
        assert ids, f"{region}: intra-day window on {day} returned nothing"


@pytest.mark.parametrize("region", REGIONS)
def test_a_genuinely_inverted_window_is_still_rejected(
    conn: sqlite3.Connection, region: str
) -> None:
    # The guard must still fire where it should: B143's own pair, and the same pair written
    # in mixed notations, on the build that shipped the defect.
    for since, until in (("2026-07-01", "2026-06-01"), ("20260701", "2026-06-01")):
        for service in (get_announcements, get_banners):
            with pytest.raises(ValueError, match="can match nothing"):
                service(conn, server=region, since=since, until=until)  # type: ignore[operator]


# --- the day-granular coarsening, on the real feed (§V116 (b)) -----------------


@pytest.mark.parametrize("region", REGIONS)
def test_a_sub_day_bound_keeps_the_real_announcements_of_that_day(
    conn: sqlite3.Connection, region: str
) -> None:
    # Before the coarsening, a bound naming a time of day dropped that whole day: on this
    # build every one of the region's stored announcement days answered EMPTY.
    days = _stored_days(conn, _ANNOUNCEMENT_DATES, region)
    assert days, f"{region}: promoted build carries no announcement dates"
    for day in days:
        result = get_announcements(conn, server=region, since=f"{day}T10:00:00", page_size=100)
        assert result.announcements, f"{region}: sub-day bound on {day} returned nothing"
        assert any("whole day" in limitation for limitation in result.limitations)
        assert all("widen or drop" not in limitation for limitation in result.limitations)
