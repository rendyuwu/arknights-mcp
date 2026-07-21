"""Announcement metadata read repository (§V2; §T96).

Encapsulates the parameterized ``SELECT`` that backs :func:`get_announcements`:
every ``announcements`` row for a region, joined to its ``record_provenance`` ->
``source_snapshots`` chain so each announcement carries its OWN provenance (§V5/§V17),
distinct from the game-data / drop facts. An optional ``since``/``until`` date window
narrows the set at the SQL layer (parameterized, §V2).

The scope is METADATA-ONLY (§V56, extends §V16): the row shape is exactly the five
metadata columns the 0010 schema can hold (``announce_id``/``title``/``date``/``url``/
``category``) -- there is no body/html/prose column to select, so a prose leak is
impossible at this layer. ``region`` is carried explicitly so an announcement stands
alone (§V5); en and cn are never mixed (the WHERE gates on the requested region).

Rows are returned as flat, typed dataclasses mirroring the selected columns 1:1;
the page slicing + provenance dedup stay in the service. Every value is bound (§V2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arknights_mcp.db.repositories.base import Repository


@dataclass(frozen=True)
class AnnouncementRow:
    """One ``announcements`` row plus its provenance stamps (metadata-only, §V56).

    ``title``/``date``/``url``/``category`` are nullable metadata (a real feed row
    may omit any, §T95). ``snapshot_id`` + ``imported_at`` are the announcement's own
    provenance chain (§V5/§V17). A ``None`` field means the datum was absent upstream,
    never a fabricated value (§V26). There is deliberately no body/html/prose field --
    the schema itself cannot hold one (§V16/§V56).
    """

    announce_id: str
    title: str | None
    date: str | None
    url: str | None
    category: str | None
    region: str
    snapshot_id: str
    imported_at: str


# Every announcement for a region, joined to its own provenance chain (§V5/§V17). The
# since/until window is applied at the SQL layer via the "(? IS NULL OR a.date >= ?)"
# idiom: a NULL bound leaves that side open; a set bound narrows by the stored ISO date
# string (lexicographic compare is date-order-correct for ISO-8601). A row with a NULL
# date is excluded once EITHER bound is set (it cannot be placed in the window), but
# kept when the window is fully open. Ordered by date DESC then announce_id so the
# newest announcements page first and the payload is deterministic + reproducible
# (§V26); NULL dates sort last under DESC.
_ANNOUNCEMENTS_SQL = (
    "SELECT a.announce_id, a.title, a.date, a.url, a.category, a.region, "
    "p.snapshot_id, ss.imported_at "
    "FROM announcements a "
    "JOIN record_provenance p ON p.provenance_id = a.provenance_id "
    "JOIN source_snapshots ss ON ss.snapshot_id = p.snapshot_id "
    "WHERE a.region = ? "
    "AND (? IS NULL OR (a.date IS NOT NULL AND a.date >= ?)) "
    "AND (? IS NULL OR (a.date IS NOT NULL AND a.date <= ?)) "
    "ORDER BY a.date DESC, a.announce_id"
)


def _to_announcement_row(row: Any) -> AnnouncementRow:
    (
        announce_id,
        title,
        date,
        url,
        category,
        region,
        snapshot_id,
        imported_at,
    ) = row
    return AnnouncementRow(
        announce_id=announce_id,
        title=title,
        date=date,
        url=url,
        category=category,
        region=region,
        snapshot_id=snapshot_id,
        imported_at=imported_at,
    )


class AnnouncementRepository(Repository):
    """Read-only access to the announcement metadata cache (§V2)."""

    def announcements_for_region(
        self, region: str, *, since: str | None = None, until: str | None = None
    ) -> list[AnnouncementRow]:
        """Every announcement for ``region`` within the optional ``since``/``until``
        date window, newest first (§V26). Region-scoped so en/cn are never mixed
        (§V5); both bounds parameterized (§V2)."""
        return [
            _to_announcement_row(r)
            for r in self._all(_ANNOUNCEMENTS_SQL, (region, since, since, until, until))
        ]
