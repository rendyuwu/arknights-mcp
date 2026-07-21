"""Announcement metadata service (§T96): the shared domain entry point both
transports call (§V14) for the announcement metadata cache.

:func:`get_announcements` lists one region's announcement metadata (§V56) with an
optional ``since``/``until`` date window and bounded pagination (§V19/§V22). Every
row carries its region + provenance (§V5); en and cn are never mixed (the region is
part of the query). The scope is METADATA-ONLY (§V56, extends §V16): only the five
metadata fields are surfaced -- there is no body/html/prose to leak.

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
from arknights_mcp.models.common import PAGE_SIZE_DEFAULT
from arknights_mcp.services.stages import (
    SectionPage,
    StageProvenance,
    _section_page,
    _validate_page,
)

#: Typed outcome of an announcement lookup. Always ``ok``: a region with no
#: announcements is a legitimate empty list (the adapter is disabled by default,
#: D14/§V56), not a ``not_found`` -- this is a list tool, not an entity lookup.
AnnouncementsStatus = Literal["ok"]


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
    """

    status: AnnouncementsStatus
    server: str
    announcements: tuple[AnnouncementFacts, ...]
    page: SectionPage
    provenance: tuple[StageProvenance, ...]


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
    list (the adapter is disabled by default, D14/§V56), never a ``not_found``. Both
    transports call this same function (§V14).
    """
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
    )
