"""Banner-archive service (§T114): the shared domain entry point both transports
call (§V14) for the banner archive.

:func:`get_banners` lists one region's banner metadata (§V62) with an optional
``since``/``until`` open-time window and bounded pagination (§V19/§V22). Every banner
carries its region + provenance (§V5); en and cn are never mixed (the region is part of
the query). The scope is METADATA-ONLY (§V62, extends §V16/§V56): only the schedule/
identity fields + the TYPED featured operators are surfaced -- there is no gacha
summary/detail/html/image to leak.

Two §V62/§V26 caveats surface as limitations on the result:

* a standard banner (``NORMAL``/``SINGLE``/``DOUBLE``/``LINKAGE``) carries no typed
  featured-op in the game data (its rate-up lives only in prose, which is §V18-
  forbidden), so a listing that includes one notes "standard-banner rate-up not in typed
  gamedata" (§V26 missing-field -> limitation; the field is genuinely absent, never
  fabricated);
* a featured op whose char id did not soft-resolve to an operator present in the same
  snapshot is surfaced as the raw char id (§V62); a listing with one notes that some
  featured operators are unresolved.

Read-only + parameterized SQL only (§V2): the parameterized ``SELECT`` lives in
:class:`~arknights_mcp.db.repositories.banners.BannerRepository`. It does not open the
connection; callers pass one in, so both transports share this exact function (§V14).
The page bounds + provenance dedup reuse the shared §V37 helpers from
:mod:`arknights_mcp.services.stages`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from arknights_mcp.db.repositories.banners import BannerRepository, BannerRow
from arknights_mcp.models.common import PAGE_SIZE_DEFAULT
from arknights_mcp.services.stages import (
    SectionPage,
    StageProvenance,
    _section_page,
    _validate_page,
)

#: Typed outcome of a banner lookup. Always ``ok``: a region with no banners is a
#: legitimate empty list (gacha_table is fetched tolerant-absent, §V41/B36), not a
#: ``not_found`` -- this is a list tool, not an entity lookup.
BannersStatus = Literal["ok"]

#: §V62/§V26 limitation: a standard banner carries no typed featured-op in the game
#: data (its rate-up is prose only, §V18-forbidden), so none is emitted -- surfaced as
#: a caveat, never a fabricated rate-up.
STANDARD_BANNER_LIMITATION = (
    "standard-banner rate-up not in typed gamedata: one or more listed banners "
    "carry no typed featured operator; their rate-up lives only in gacha prose, which "
    "is excluded by the field policy and never fabricated"
)

#: §V62 limitation: a featured char id that did not soft-resolve to an operator present
#: in this snapshot is surfaced as the raw char id (operators are optional-zero, B36).
UNRESOLVED_FEATURED_OP_LIMITATION = (
    "one or more featured operators could not be resolved to an operator present in this "
    "snapshot; the raw char id is surfaced and resolved is false"
)


@dataclass(frozen=True)
class FeaturedOpFacts:
    """One typed featured operator on a banner for the wire (§V62).

    ``char_id`` is the raw source id; ``resolved`` is true when it soft-resolved to an
    operator present in the same snapshot, in which case ``operator_name`` is that
    operator's display name (else ``None`` with the raw id surfaced -- never fabricated).
    """

    char_id: str
    resolved: bool
    operator_name: str | None


@dataclass(frozen=True)
class BannerFacts:
    """One banner's typed metadata for the wire (no prose; §V16/§V18/§V62).

    Exactly the §V62 schedule/identity fields plus the typed featured ops;
    ``display_name``/``open_time``/``end_time``/``rule_type`` are nullable (a raw pool
    entry may omit any). ``featured_ops`` is empty for a standard banner (surfaced as a
    listing-level limitation). No gacha prose field exists -- the schema cannot hold one
    (§V16).
    """

    game_id: str
    display_name: str | None
    open_time: str | None
    end_time: str | None
    rule_type: str | None
    region: str
    featured_ops: tuple[FeaturedOpFacts, ...]


@dataclass(frozen=True)
class BannersResult:
    """Domain result of :func:`get_banners` (§T114; §V5/§V19/§V22/§V62).

    ``banners`` holds the requested page (newest first); ``page`` is the bounded §V19
    descriptor over the FULL filtered set (``total`` + ``has_more``). ``provenance`` is
    the distinct banner snapshots (``snapshot_id`` + ``imported_at``) backing the full
    filtered set, all sharing the requested region (§V5) -- derived over the full set
    (never the current page) so a later page never drops a snapshot. ``limitations``
    carries the §V62/§V26 caveats for the returned page.
    """

    status: BannersStatus
    server: str
    banners: tuple[BannerFacts, ...]
    page: SectionPage
    provenance: tuple[StageProvenance, ...]
    limitations: tuple[str, ...]


def _group_banners(rows: tuple[BannerRow, ...]) -> tuple[BannerFacts, ...]:
    """Fold the flat featured-op leaves into one :class:`BannerFacts` per banner (§V62).

    Rows arrive ordered (open_time DESC, game_id, char_id) so each banner's leaves are
    contiguous; grouping by ``banner_pk`` in first-seen order preserves the newest-first
    display order (§V26). A standard banner's single NULL-``char_id`` leaf contributes no
    featured op, leaving ``featured_ops`` empty.
    """
    order: list[int] = []
    first: dict[int, BannerRow] = {}
    ops: dict[int, list[FeaturedOpFacts]] = {}
    for r in rows:
        if r.banner_pk not in first:
            order.append(r.banner_pk)
            first[r.banner_pk] = r
            ops[r.banner_pk] = []
        op = r.featured_op
        if op.char_id is not None:
            ops[r.banner_pk].append(
                FeaturedOpFacts(
                    char_id=op.char_id,
                    resolved=bool(op.resolved),
                    operator_name=op.operator_name,
                )
            )
    return tuple(
        BannerFacts(
            game_id=first[pk].game_id,
            display_name=first[pk].display_name,
            open_time=first[pk].open_time,
            end_time=first[pk].end_time,
            rule_type=first[pk].rule_type,
            region=first[pk].region,
            featured_ops=tuple(ops[pk]),
        )
        for pk in order
    )


def _banner_provenance(rows: tuple[BannerRow, ...]) -> tuple[StageProvenance, ...]:
    """The distinct banner snapshots backing the FULL filtered set (§V5/§V17).

    Derived over the whole set (never the current page) so paging never drops a snapshot
    from the provenance list. Region-scoped (§V5), so every row shares the requested
    region; the distinct ``(snapshot_id, imported_at)`` pairs are emitted in first-seen
    (already date-ordered) order so the list is deterministic (§V26). Typically one
    banner snapshot per region.
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


def _limitations(banners: tuple[BannerFacts, ...]) -> tuple[str, ...]:
    """The §V62/§V26 caveats for the banners on the returned page.

    A banner with no typed featured-op (a standard banner) adds the standard-banner
    caveat; a featured op that stayed unresolved adds the unresolved caveat. Each is
    added at most once, in a fixed order, so the list is deterministic (§V26).
    """
    limitations: list[str] = []
    if any(not b.featured_ops for b in banners):
        limitations.append(STANDARD_BANNER_LIMITATION)
    if any(not op.resolved for b in banners for op in b.featured_ops):
        limitations.append(UNRESOLVED_FEATURED_OP_LIMITATION)
    return tuple(limitations)


def get_banners(
    conn: sqlite3.Connection,
    *,
    server: str,
    since: str | None = None,
    until: str | None = None,
    page: int = 1,
    page_size: int = PAGE_SIZE_DEFAULT,
) -> BannersResult:
    """List one region's banner archive + optional open-time window (§T114).

    Read-only; parameterized SQL only (§V2); metadata-only (§V62 -- no gacha prose).
    Returns a :class:`BannersResult` with region + provenance on every banner (§V5) and
    the requested bounded page (§V19/§V22). ``since``/``until`` narrow by the stored ISO
    ``open_time`` string (inclusive; a banner with no open_time is excluded once either
    bound is set).

    The banner archive is unbounded in principle (it accretes past + near-future
    banners), so it is **paged** (§V22/§V19): ``page`` is validated against the §V19
    window here too (mirroring the model gate -- one contract, both places, never a
    silent clamp). Grouping + provenance are computed over the FULL filtered set BEFORE
    slicing, so a later page never drops a banner or a snapshot. A region with no banners
    is a legitimate empty ``ok`` list (gacha_table is tolerant-absent, §V41/B36), never a
    ``not_found``. Both transports call this same function (§V14).
    """
    p, size = _validate_page(page, page_size)

    all_rows = tuple(BannerRepository(conn).banners_for_region(server, since=since, until=until))
    all_banners = _group_banners(all_rows)
    provenance = _banner_provenance(all_rows)
    page_info = _section_page(p, size, len(all_banners))
    banners = all_banners[(p - 1) * size : p * size]

    return BannersResult(
        status="ok",
        server=server,
        banners=banners,
        page=page_info,
        provenance=provenance,
        limitations=_limitations(banners),
    )
