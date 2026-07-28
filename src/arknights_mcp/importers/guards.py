"""Shared importer guards: §V30 fail-closed + §V58 optional-domain fail-open (§V37).

Two patterns had been copy-pasted per domain (B125), and the copy-drift class they
belong to (B92/B106) is exactly the one §V37 exists to close -- each new optional
domain copied the block from the previous one:

* the **§V30 silent-empty guard** -- "the source carried N candidate records but
  none became a row, so refuse the build instead of promoting an empty domain" --
  stood as six near-identical inline copies (banners, skins, penguin drops,
  announcements twice, activity titles) plus the pipeline's combat guard;
* the **§V58 optional-domain fail-open** -- savepoint the domain, catch its
  :class:`ImporterError`, warn, and continue with an empty result -- stood as two
  verbatim copies in the pipeline (banners, then skins copy-pasted from it).

Both now live here exactly once. The variance between the former copies is passed
explicitly (the message parts, the typed empty result, the spec cite), never forked
into silent divergent copies (§V37) -- the same discipline
:func:`~arknights_mcp.util.sqlite.integrity_guard` applies to the §V33 pattern.

The two guards pull in OPPOSITE directions and that is deliberate:
:func:`guard_not_silently_empty` fails the build CLOSED (§V30/§V3 -- a shape or id
mismatch must never promote as an empty domain), while
:func:`import_optional_domain` fails OPEN (§V58 -- an optional archive must not take
down the mandatory combat core). Composed, an optional domain's own §V30 guard trips,
its savepoint rolls back that domain's partial rows, and the combat build continues.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import NoReturn

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.util.sqlite import savepoint

_LOG = logging.getLogger(__name__)


def refuse_silent_empty(
    reason: str,
    *,
    outcome: str,
    cite: str = "§V30",
    detail: str = "",
) -> NoReturn:
    """Raise the typed §V30 refusal (the single §V37 home for the message + raise).

    ``reason`` states what the source carried and what it failed to produce;
    ``outcome`` names the build that is being refused ("empty banner build",
    "degraded announcement build"). ``cite`` defaults to the §V30 invariant and takes
    a compound cite where a second invariant applies ("§V30/§V61"); ``detail`` appends
    a trailing clause. Always raises :class:`ImporterError`, so the candidate is
    discarded by the caller and the active database stays untouched (§V3/§V33).
    """
    raise ImporterError(f"{reason}; refusing a silent {outcome} ({cite}){detail}")


def guard_not_silently_empty(
    *,
    candidates: int,
    produced: int,
    source: str,
    unit: str,
    resolution: str,
    outcome: str,
    scope: str | None = None,
    cite: str = "§V30",
    detail: str = "",
) -> None:
    """Fail closed when a non-empty source produced nothing (§V30): the shared guard.

    The predicate every former copy shared: ``candidates`` counts what the source
    offered, ``produced`` counts what survived into rows. Zero candidates is a
    legitimately empty domain and passes; candidates with zero produced is a shape or
    id mismatch, which is invisible downstream (the domain simply looks empty) and so
    is refused rather than promoted (§V30/§V3).

    The message reads ``"{scope}: {source} had {candidates} {unit} but none
    {resolution}; refusing a silent {outcome} ({cite}){detail}"``. ``scope`` is the
    region/server when the caller knows it (§V5 per-region guards) and is omitted by
    the pure parse-time callers that do not.
    """
    if not candidates or produced:
        return
    prefix = f"{scope}: " if scope else ""
    refuse_silent_empty(
        f"{prefix}{source} had {candidates} {unit} but none {resolution}",
        outcome=outcome,
        cite=cite,
        detail=detail,
    )


def import_optional_domain[T](
    conn: sqlite3.Connection,
    do_import: Callable[[], T],
    *,
    domain: str,
    server: str,
    describe: str,
    empty: Callable[[], T],
    cites: str,
) -> T:
    """Import one OPTIONAL domain fail-open under its own savepoint (§V58/§V37).

    The shared home for the in-build optional-domain pattern (banners §T116/§V62,
    skins §T182/§V88, and whatever domain lands next -- B125 filed this precisely
    because each new one copied the last). ``do_import`` runs inside a
    :func:`~arknights_mcp.util.sqlite.savepoint` named ``domain``; on
    :class:`ImporterError` -- the domain's own §V30 non-empty-or-fail guard, or a §V33
    constraint anomaly -- that savepoint's writes (partial domain rows AND their
    provenance) roll back, the failure is warned once, and ``empty()`` supplies the
    typed zero result so the build continues.

    Fail-open is bounded to domains OUTSIDE ``CRITICAL_TABLES``, where an empty
    domain is a legitimate state; the mandatory combat core stays fail-closed (§V3).
    Only :class:`ImporterError` is caught -- a programming error or a
    :class:`sqlite3.DatabaseError` still tears the build down rather than silently
    shipping a domain-less build under a warning.

    The savepoint name is the ``domain`` label because regions run sequentially
    (mirrors ``cli.sync._ride_along``'s fixed name); ``describe`` + ``cites`` supply
    the per-domain half of the warning ("banner archive", "§V62/§V58").
    """
    try:
        with savepoint(conn, domain):
            return do_import()
    except ImporterError as exc:
        _LOG.warning(
            "%s: %s unavailable, skipped; continuing combat build (%s): %s",
            server,
            describe,
            cites,
            exc,
        )
        return empty()
