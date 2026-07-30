"""Announcement metadata service (§T96): the shared domain entry point both
transports call (§V14) for the announcement metadata cache.

:func:`get_announcements` lists one region's announcement metadata (§V56) with an
optional ``since``/``until`` date window and bounded pagination (§V19/§V22). Every
row carries its region + provenance (§V5); en and cn are never mixed (the region is
part of the query). The scope is METADATA-ONLY (§V56, extends §V16): only the five
metadata fields are surfaced -- there is no body/html/prose to leak.

An empty answer states WHY (§V50/§V106 (b)): the feed is an OPTIONAL domain, so
"never imported for this region" and "imported, nothing in the requested window" are
different facts that used to ship identical bytes (``ok`` + ``[]``, B146). Availability
is checked BEFORE absence is asserted, against the region's own announcement snapshot.

Read-only + parameterized SQL only (§V2): the parameterized ``SELECT`` lives in
:class:`~arknights_mcp.db.repositories.announcements.AnnouncementRepository`. It does
not open the connection; callers pass one in, so both transports share this exact
function (§V14). The page bounds + provenance dedup reuse the shared §V37 helpers from
:mod:`arknights_mcp.services.stages`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from arknights_mcp.db.repositories.announcements import AnnouncementRepository, AnnouncementRow
from arknights_mcp.db.repositories.metadata import MetadataRepository
from arknights_mcp.models.common import PAGE_SIZE_DEFAULT, reject_inverted_window
from arknights_mcp.services.stages import (
    SectionPage,
    StageProvenance,
    _section_page,
    _validate_page,
)
from arknights_mcp.sources.announcements import source_id_for_region
from arknights_mcp.util.iso_bounds import canonical_window, coarsened_window_bounds

#: Typed outcome of an announcement lookup. Always ``ok`` (§V106 (b)): this is a SET
#: query, not an entity lookup, so an empty list is a legitimate answer to a
#: well-formed question -- never a ``not_found``. Why it is empty rides the
#: ``limitations`` instead (§V50), which keeps the status additive (⊥ a §V21 bump).
AnnouncementsStatus = Literal["ok"]


def feed_not_imported_limitation(source_id: str, server: str) -> str:
    """§V50: the region's announcement feed has NO imported snapshot on this build.

    Named source + an admin step (§V28: importing is CLI-only, never a query-time
    fetch, §V1). This is the availability half of §V50 -- absence of announcements
    cannot be asserted when the feed itself never ran -- and it is deliberately
    worded nothing like :func:`empty_window_limitation`, because the whole B146
    defect was that the two cases were indistinguishable on the wire.
    """
    return (
        f"the official announcement feed `{source_id}` has no imported snapshot for "
        f"{server} on this build, so this list cannot show whether announcements exist; "
        f"ask the server admin to run `arknights-mcp sync --server {server}`"
    )


def empty_window_limitation() -> str:
    """§V106 (b): the feed IS imported and no announcement fell in the window.

    A well-formed set query with zero hits is an ``ok`` answer, not a failure, but it
    still says why it is empty + what to change. The counterpart to
    :func:`feed_not_imported_limitation`: this one confirms the feed is present.
    """
    return (
        "the announcement feed is imported for this region, but no announcement falls in "
        "the requested window; widen or drop the since/until bounds"
    )


def feed_carries_no_announcement_limitation() -> str:
    """§V106 (b): the feed is imported for this region and carries no announcement.

    Distinct from :func:`empty_window_limitation` because no window was requested:
    nothing the client can change would widen this result, so it is told the emptiness
    is the imported snapshot's, not its query's.
    """
    return (
        "the announcement feed is imported for this region but its snapshot carries no "
        "announcement; this is the imported feed's own content, not a filtered-out result"
    )


def day_granular_bound_limitation(bounds: tuple[str, ...]) -> str:
    """§V116 (b): a bound finer than a day was read as the whole calendar day.

    ``announcements.date`` stores a calendar date with no time of day, so a bound
    carrying one cannot be applied as written. It is widened to its day (the inclusive
    reading -- excluding that day would assert an absence the column cannot support,
    §V26) and the widening is stated here rather than left for the client to discover
    from a row it did not expect to see. Fires only when a bound actually carried a time.
    """
    which = " and ".join(bounds)
    label = "bound" if len(bounds) == 1 else "bounds"
    return (
        "the announcement date is stored as a calendar date with no time of day, so the "
        f"{which} {label} you gave was read as the whole day; this feed cannot be "
        "windowed more finely than one day"
    )


def no_announcement_source_limitation(server: str) -> str:
    """§V56: the region has no official announcement feed at all.

    Unreachable through the MCP tool (the input model admits only en/cn) and kept for
    a caller reaching the service directly: an empty list from a region that has no
    feed must not read as "this region published nothing" (§V26/§V50).
    """
    return (
        f"there is no official announcement feed for region {server}; "
        "announcements are available for en and cn only"
    )


@dataclass(frozen=True)
class AnnouncementFacts:
    """One announcement's typed metadata for the wire (no prose; §V16/§V18/§V56).

    Exactly the five §V56 metadata fields; ``title``/``date``/``url``/``category`` are
    nullable (a real feed row may omit any). No body/html/prose field exists -- the
    schema cannot hold one (§V16).
    """

    announce_id: str
    title: str | None
    date: str | None
    url: str | None
    category: str | None
    region: str


@dataclass(frozen=True)
class AnnouncementsResult:
    """Domain result of :func:`get_announcements` (§T96; §V5/§V19/§V22).

    ``announcements`` holds the requested page (newest first); ``page`` is the bounded
    §V19 descriptor over the FULL filtered set (``total`` + ``has_more``). ``provenance``
    is the distinct announcement snapshots (``snapshot_id`` + ``imported_at``) backing
    the full filtered set, all sharing the requested region (§V5) -- derived over the
    full set (never the current page) so a later page never drops a snapshot.
    ``limitations`` carries the §V50/§V106 (b) reason an empty list is empty.
    """

    status: AnnouncementsStatus
    server: str
    announcements: tuple[AnnouncementFacts, ...]
    page: SectionPage
    provenance: tuple[StageProvenance, ...]
    limitations: tuple[str, ...]


def _announcement_facts(row: AnnouncementRow) -> AnnouncementFacts:
    """Shape a repository row into the typed, region-attributed metadata fact (§V5/§V56)."""
    return AnnouncementFacts(
        announce_id=row.announce_id,
        title=row.title,
        date=row.date,
        url=row.url,
        category=row.category,
        region=row.region,
    )


def _announcement_provenance(rows: tuple[AnnouncementRow, ...]) -> tuple[StageProvenance, ...]:
    """The distinct announcement snapshots backing the FULL filtered set (§V5/§V17).

    Derived over the whole set (never the current page) so paging never drops a
    snapshot from the provenance list. Region-scoped (§V5), so every row shares the
    requested region; the distinct ``(snapshot_id, imported_at)`` pairs are emitted in
    first-seen (already date-ordered) order so the list is deterministic (§V26).
    Typically one announcement snapshot per region.
    """
    seen: set[tuple[str, str]] = set()
    provenance: list[StageProvenance] = []
    for r in rows:
        key = (r.snapshot_id, r.imported_at)
        if key in seen:
            continue
        seen.add(key)
        provenance.append(StageProvenance(snapshot_id=r.snapshot_id, imported_at=r.imported_at))
    return tuple(provenance)


def _limitations(
    conn: sqlite3.Connection, server: str, total: int, windowed: bool
) -> tuple[str, ...]:
    """Why an empty announcement list is empty (§V50 availability, then §V106 (b)).

    Availability is decided BEFORE absence is asserted: a region whose feed has no
    imported snapshot gets :func:`feed_not_imported_limitation`, naming the source and
    the admin step, because "no announcement" is simply not inferable from a feed that
    never ran (§V50/§V26). Only once the feed IS present does an empty result mean what
    a client would read it to mean, and then it says so -- through
    :func:`empty_window_limitation` when a since/until window excluded everything, else
    through :func:`feed_carries_no_announcement_limitation`. Exactly one string fires,
    so the cases are always distinguishable on the wire (B146).

    A non-empty result carries none of them: the rows themselves prove the feed is
    present. A region outside {en,cn} has no announcement source at all (§V56) -- the
    model gate rejects one before it reaches here, so this only answers a caller
    arriving through the service directly, and it says so rather than implying the
    region merely has nothing to report.
    """
    if total > 0:
        return ()
    source_id = source_id_for_region(server)
    if source_id is None:
        return (no_announcement_source_limitation(server),)
    if not MetadataRepository(conn).has_source_snapshot(source_id, server):
        return (feed_not_imported_limitation(source_id, server),)
    if windowed:
        return (empty_window_limitation(),)
    return (feed_carries_no_announcement_limitation(),)


def get_announcements(
    conn: sqlite3.Connection,
    *,
    server: str,
    since: str | None = None,
    until: str | None = None,
    page: int = 1,
    page_size: int = PAGE_SIZE_DEFAULT,
) -> AnnouncementsResult:
    """List one region's announcement metadata + optional date window (§T96).

    Read-only; parameterized SQL only (§V2); metadata-only (§V56 -- no body/prose).
    Returns an :class:`AnnouncementsResult` with region + provenance on every row (§V5)
    and the requested bounded page (§V19/§V22). ``since``/``until`` narrow by the stored
    ISO date string (inclusive; a row with no date is excluded once either bound is set).

    The announcement list is unbounded in principle (a live feed accretes over time), so
    it is **paged** (§V22/§V19): ``page`` is validated against the §V19 window here too
    (mirroring the model gate -- one contract, both places, never a silent clamp). The
    provenance is computed over the FULL filtered set BEFORE slicing, so a later page
    never drops a snapshot. A region with no announcements is a legitimate empty ``ok``
    list, never a ``not_found`` (§V106 (b)) -- but never a BARE one either: the result
    states whether the region's feed was ever imported (§V50) before an empty list can
    be read as "nothing was announced" (B146). An IMPOSSIBLE window is not an empty
    answer at all: ``since`` after ``until`` is rejected here as well as at the model gate
    (§V105/B143 -- one contract, both places, like the §V19 page bounds), because no
    limitation can make "nothing matched" a true answer to a question nothing can match.
    Both transports call this same function (§V14).

    Each bound is rendered into the form of the column it is compared against BEFORE it
    reaches the query or the guard (§V116/B163): canonical ISO notation, then truncated to
    its calendar day, because ``announcements.date`` is day-granular. Without the render
    the window's collation was the caller's notation -- ``since="20260101"`` returned
    nothing on a corpus it should have matched entirely, under this service's own "widen
    or drop the bounds" advice, which no widening could have fixed. A bound that carried a
    time of day is widened to that day and says so (§V116 (b)). The render happens here as
    well as at the model gate so a caller reaching the service directly gets the same
    window (§V19's one-contract-both-places shape).
    """
    coarsened = coarsened_window_bounds(since, until, granularity="date")
    since, until = canonical_window(since, until, granularity="date")
    reject_inverted_window(since, until)
    p, size = _validate_page(page, page_size)

    all_rows = tuple(
        AnnouncementRepository(conn).announcements_for_region(server, since=since, until=until)
    )
    provenance = _announcement_provenance(all_rows)
    page_info = _section_page(p, size, len(all_rows))
    rows = all_rows[(p - 1) * size : p * size]

    return AnnouncementsResult(
        status="ok",
        server=server,
        announcements=tuple(_announcement_facts(r) for r in rows),
        page=page_info,
        provenance=provenance,
        limitations=(
            # Deterministic order (§V26): the granularity disclosure describes the QUERY
            # (it fires whether or not rows came back), then the §V50/§V106 (b) reason an
            # empty list is empty -- still exactly one of those four strings (B146).
            *((day_granular_bound_limitation(coarsened),) if coarsened else ()),
            *_limitations(
                conn, server, len(all_rows), windowed=since is not None or until is not None
            ),
        ),
    )
