"""Banner-archive service: the shared domain entry point both transports
call for the banner archive.

:func:`get_banners` lists one region's banner metadata with an optional
``since``/``until`` open-time window and bounded pagination. Every banner
carries its region + provenance; en and cn are never mixed (the region is part of
the query). The scope is METADATA-ONLY: only the schedule/
identity fields + the TYPED featured operators are surfaced -- there is no gacha
summary/detail/html/image to leak.

Three caveats surface as limitations on the result:

* a standard banner (``NORMAL``/``SINGLE``/``DOUBLE``/``LINKAGE``) carries no typed
  featured-op in the game data (its rate-up lives only in prose, which is
  forbidden), so a listing that includes one notes "standard-banner rate-up not in typed
  gamedata" (missing-field -> limitation; the field is genuinely absent, never
  fabricated);
* a pool whose rule type declares a TYPED CARRIER, but whose featured-op array is
  absent from the source anyway, gets its own caveat naming those rule types.
  Four of the six declared carriers -- ``CLASSIC``/``CLASSIC_DOUBLE``/``FESCLASSIC``/
  ``SPECIAL`` -- carry the array on ZERO pools of the real corpus, so this arm is the
  common case, not an edge; an unclassified rule type (``BACKFLOW``, or a
  future token) joins it as the conservative side (unknown -> conservative);
* a featured op whose char id did not soft-resolve to an operator present in the same
  snapshot is surfaced as the raw char id; a listing with one notes that some
  featured operators are unresolved.

Read-only + parameterized SQL only: the parameterized ``SELECT`` lives in
:class:`~arknights_mcp.db.repositories.banners.BannerRepository`. It does not open the
connection; callers pass one in, so both transports share this exact function.
The page bounds + provenance dedup reuse the shared helpers from
:mod:`arknights_mcp.services.stages`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from arknights_mcp.db.repositories.banners import BannerRepository, BannerRow
from arknights_mcp.models.common import PAGE_SIZE_DEFAULT, reject_inverted_window
from arknights_mcp.services.stages import (
    SectionPage,
    StageProvenance,
    _section_page,
    _validate_page,
)
from arknights_mcp.util.iso_bounds import canonical_window

#: Typed outcome of a banner lookup. Always ``ok``: a region with no banners is a
#: legitimate empty list (gacha_table is fetched tolerant-absent), not a
#: ``not_found`` -- this is a list tool, not an entity lookup.
BannersStatus = Literal["ok"]

#: Rule types that carry a typed featured op in the game data: ``LIMITED`` names one
#: under ``limitParam.limitedCharId``, the CLASSIC family an array under
#: ``dynMeta.attainRare6CharList``. This is the set the importer READS (a mirror of
#: :mod:`arknights_mcp.importers.banners`), which is what makes a pool in it with zero
#: featured ops a SOURCE gap rather than a rule-type that never had one -- the distinction
#: the earlier sweep needed and the emit side could not previously draw.
EXPECTED_FEATURED_OP_RULE_TYPES: frozenset[str] = frozenset(
    {"LIMITED", "ATTAIN", "CLASSIC", "CLASSIC_ATTAIN", "CLASSIC_DOUBLE", "FESCLASSIC", "SPECIAL"}
)

#: Rule types that carry NO typed featured op anywhere in the game data: their rate-up
#: lives only in gacha prose (forbidden). COUNTED over the pinned upstream, both
#: regions: 282 of 389 EN pools and 312 of 437 CN pools, zero featured rows on any of them.
NO_TYPED_FEATURED_OP_RULE_TYPES: frozenset[str] = frozenset(
    {"NORMAL", "SINGLE", "DOUBLE", "LINKAGE"}
)

#: Limitation: a standard banner carries no typed featured-op in the game
#: data (its rate-up is prose only, forbidden), so none is emitted -- surfaced as
#: a caveat, never a fabricated rate-up. The wire OMITS the ``featured_ops``
#: key on such a pool rather than sending ``[]``, so this caveat is the sole signal.
STANDARD_BANNER_LIMITATION = (
    "standard-banner rate-up not in typed gamedata: one or more listed banners "
    "carry no typed featured operator, so they omit the featured_ops key; their rate-up "
    "lives only in gacha prose, which is excluded by the field policy and never fabricated"
)


def absent_featured_op_array_limitation(rule_types: tuple[str, ...]) -> str:
    """Caveat for expected-carrier pools with none on this page.

    A pool whose rule type DOES carry a typed featured op elsewhere in the data, yet has
    none here, names its rule types so the caveat resolves PER POOL -- a client maps each
    key-less row to this reason through that row's own ``rule_type``. The rule types are
    read off the page, never a static list, so the caveat cannot outlive the gap it
    describes (the partition is measured, not asserted) and stays bounded by the
    12-token domain rather than by the page size.

    This replaces the ``featured_ops: []`` that shipped before: an empty list is a
    CONFIRMED-none, which was FALSE for every FESCLASSIC/CLASSIC pool -- a real rate-up
    banner whose rate-up operator the wire denied existed.
    """
    return (
        "featured_ops is omitted for the listed pools whose rule_type is "
        f"{', '.join(rule_types)}: the typed featured-operator array is absent from the "
        "game data for them, so no featured operator is emitted. It is never emitted as "
        "an empty list, which would assert those pools have no rate-up operator at all"
    )


#: Limitation: a featured char id that did not soft-resolve to an operator present
#: in this snapshot is surfaced as the raw char id (operators are optional-zero).
UNRESOLVED_FEATURED_OP_LIMITATION = (
    "one or more featured operators could not be resolved to an operator present in this "
    "snapshot; the raw char id is surfaced and resolved is false"
)


@dataclass(frozen=True)
class FeaturedOpFacts:
    """One typed featured operator on a banner for the wire.

    ``char_id`` is the raw source id; ``resolved`` is true when it soft-resolved to an
    operator present in the same snapshot, in which case ``operator_name`` is that
    operator's display name (else ``None`` with the raw id surfaced -- never fabricated).
    """

    char_id: str
    resolved: bool
    operator_name: str | None


@dataclass(frozen=True)
class BannerFacts:
    """One banner's typed metadata for the wire (no prose).

    Exactly the schedule/identity fields plus the typed featured ops;
    ``display_name``/``open_time``/``end_time``/``rule_type`` are nullable (a raw pool
    entry may omit any). ``featured_ops`` is empty whenever the source carries no typed
    featured op for the pool -- which the wire encodes as an ABSENT key, never ``[]``,
    with the reason in a limitation. No gacha prose field exists -- the
    schema cannot hold one.
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
    """Domain result of :func:`get_banners`.

    ``banners`` holds the requested page (newest first); ``page`` is the bounded
    descriptor over the FULL filtered set (``total`` + ``has_more``). ``provenance`` is
    the distinct banner snapshots (``snapshot_id`` + ``imported_at``) backing the full
    filtered set, all sharing the requested region -- derived over the full set
    (never the current page) so a later page never drops a snapshot. ``limitations``
    carries the caveats for the returned page.
    """

    status: BannersStatus
    server: str
    banners: tuple[BannerFacts, ...]
    page: SectionPage
    provenance: tuple[StageProvenance, ...]
    limitations: tuple[str, ...]


def _group_banners(rows: tuple[BannerRow, ...]) -> tuple[BannerFacts, ...]:
    """Fold the flat featured-op leaves into one :class:`BannerFacts` per banner.

    Rows arrive ordered (open_time DESC, game_id, char_id) so each banner's leaves are
    contiguous; grouping by ``banner_pk`` in first-seen order preserves the newest-first
    display order. A standard banner's single NULL-``char_id`` leaf contributes no
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
    """The distinct banner snapshots backing the FULL filtered set.

    Derived over the whole set (never the current page) so paging never drops a snapshot
    from the provenance list. Region-scoped, so every row shares the requested
    region; the distinct ``(snapshot_id, imported_at)`` pairs are emitted in first-seen
    (already date-ordered) order so the list is deterministic. Typically one
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


def _absent_array_rule_types(banners: tuple[BannerFacts, ...]) -> tuple[str, ...]:
    """Rule types on this page that SHOULD carry a typed featured op but carry none.

    The conservative side takes two kinds of pool (unknown -> conservative side):
    a rule type declared a typed carrier, and a rule type classified NEITHER way
    (``BACKFLOW``, or a token upstream adds tomorrow) -- so a new pool type surfaces as a
    named source gap instead of being silently read as a prose-only standard banner. Only
    the four measured standard types are excluded, because only they are known to carry no
    typed featured op anywhere in the corpus. A pool with no ``rule_type`` at all cannot be
    named, so it falls to the standard caveat rather than an unnamed entry here.

    Sorted + deduped so the emitted caveat is deterministic.
    """
    return tuple(
        sorted(
            {
                b.rule_type
                for b in banners
                if not b.featured_ops
                and b.rule_type is not None
                and b.rule_type not in NO_TYPED_FEATURED_OP_RULE_TYPES
            }
        )
    )


def _limitations(banners: tuple[BannerFacts, ...]) -> tuple[str, ...]:
    """The caveats for the banners on the returned page.

    A banner with no typed featured-op splits by rule type: a standard type
    (or an unnamed one) adds the standard-banner caveat, while an expected-carrier type adds
    the absent-array caveat naming those types. Both can fire on one page -- a listing may
    mix a NORMAL pool with a FESCLASSIC one, and the two absences have different causes.
    A featured op that stayed unresolved adds the unresolved caveat. Each is added at most
    once, in a fixed order, so the list is deterministic.
    """
    absent_array_types = _absent_array_rule_types(banners)
    limitations: list[str] = []
    if any(
        not b.featured_ops
        and (b.rule_type is None or b.rule_type in NO_TYPED_FEATURED_OP_RULE_TYPES)
        for b in banners
    ):
        limitations.append(STANDARD_BANNER_LIMITATION)
    if absent_array_types:
        limitations.append(absent_featured_op_array_limitation(absent_array_types))
    if any(not op.resolved for b in banners for op in b.featured_ops):
        limitations.append(UNRESOLVED_FEATURED_OP_LIMITATION)
    return tuple(limitations)


def get_banners(
    conn: sqlite3.Connection,
    *,
    server: str,
    since: str | None = None,
    until: str | None = None,
    query: str | None = None,
    page: int = 1,
    page_size: int = PAGE_SIZE_DEFAULT,
) -> BannersResult:
    """List one region's banner archive + optional open-time window.

    Read-only; parameterized SQL only; metadata-only (no gacha prose).
    Returns a :class:`BannersResult` with region + provenance on every banner and
    the requested bounded page. ``since``/``until`` narrow by the stored ISO
    ``open_time`` string (inclusive; a banner with no open_time is excluded once either
    bound is set). ``query`` optionally narrows to banners whose ``display_name`` contains
    it (case-insensitive substring, additive; a banner with no display_name is
    excluded once it is set) -- still a paged list, so the no-dump bound holds.

    The banner archive is unbounded in principle (it accretes past + near-future
    banners), so it is **paged**: ``page`` is validated against the page
    window here too (mirroring the model gate -- one contract, both places, never a
    silent clamp). Grouping + provenance are computed over the FULL filtered set BEFORE
    slicing, so a later page never drops a banner or a snapshot. A region with no banners
    is a legitimate empty ``ok`` list (gacha_table is tolerant-absent), never a
    ``not_found``. An IMPOSSIBLE window is not an empty answer at all: ``since`` after
    ``until`` is rejected here as well as at the model gate (one contract,
    both places, like the page bounds), and this listing carried NO limitation at all
    on such a window before, so its empty list was indistinguishable from an empty
    archive. Both transports call this same function.

    Each bound is rendered into the form of the stored ``open_time`` before it reaches the
    query or the guard: canonical ISO notation, offset-aware values converted
    to UTC so they collate against the stored ``+00:00`` timestamps. Without the render the
    window's collation was the caller's notation, and this listing carried NO limitation to
    hint at it: ``since="20260101"`` returned an empty archive and ``until="20260101"``
    returned the whole one. Rendered here as well as at the model gate, so a caller reaching
    the service directly gets the same window (the one-contract-both-places shape).
    """
    since, until = canonical_window(since, until, granularity="datetime")
    reject_inverted_window(since, until)
    p, size = _validate_page(page, page_size)

    all_rows = tuple(
        BannerRepository(conn).banners_for_region(server, since=since, until=until, query=query)
    )
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
