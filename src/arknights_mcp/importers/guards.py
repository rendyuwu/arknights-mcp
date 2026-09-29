"""Shared importer guards: fail-closed silent-empty + optional-domain fail-open.

Two patterns had been copy-pasted per domain, and the copy-drift class they belong
to is exactly the one the single-home rule exists to close -- each new optional
domain copied the block from the previous one:

* the **silent-empty guard** -- "the source carried N candidate records but
  none became a row, so refuse the build instead of promoting an empty domain" --
  stood as six near-identical inline copies (banners, skins, penguin drops,
  announcements twice, activity titles) plus the pipeline's combat guard;
* the **optional-domain fail-open** -- savepoint the domain, catch its
  :class:`ImporterError`, warn, and continue with an empty result -- stood as two
  verbatim copies in the pipeline (banners, then skins copy-pasted from it).

Both now live here exactly once. The variance between the former copies is passed
explicitly (the message parts, the typed empty result), never forked into silent
divergent copies -- the same discipline
:func:`~arknights_mcp.util.sqlite.integrity_guard` applies to the constraint-anomaly
mapping.

The two guards pull in OPPOSITE directions and that is deliberate:
:func:`guard_not_silently_empty` fails the build CLOSED (a shape or id mismatch
must never promote as an empty domain), while
:func:`import_optional_domain` fails OPEN (an optional archive must not take down
the mandatory combat core). Composed, an optional domain's own silent-empty guard
trips, its savepoint rolls back that domain's partial rows, and the combat build
continues.
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
    detail: str = "",
) -> NoReturn:
    """Raise the typed refusal (the single home for the message + raise).

    ``reason`` states what the source carried and what it failed to produce;
    ``outcome`` names the build that is being refused ("empty banner build",
    "degraded announcement build"). ``detail`` appends a trailing clause. Always
    raises :class:`ImporterError`, so the candidate is discarded by the caller and
    the active database stays untouched.
    """
    raise ImporterError(f"{reason}; refusing a silent {outcome}{detail}")


def guard_not_silently_empty(
    *,
    candidates: int,
    produced: int,
    source: str,
    unit: str,
    resolution: str,
    outcome: str,
    scope: str | None = None,
    detail: str = "",
) -> None:
    """Fail closed when a non-empty source produced nothing: the shared guard.

    The predicate every former copy shared: ``candidates`` counts what the source
    offered, ``produced`` counts what survived into rows. Zero candidates is a
    legitimately empty domain and passes; candidates with zero produced is a shape or
    id mismatch, which is invisible downstream (the domain simply looks empty) and so
    is refused rather than promoted.

    The message reads ``"{scope}: {source} had {candidates} {unit} but none
    {resolution}; refusing a silent {outcome}{detail}"``. ``scope`` is the
    region/server when the caller knows it (per-region guards) and is omitted by
    the pure parse-time callers that do not.
    """
    if not candidates or produced:
        return
    prefix = f"{scope}: " if scope else ""
    refuse_silent_empty(
        f"{prefix}{source} had {candidates} {unit} but none {resolution}",
        outcome=outcome,
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
) -> T:
    """Import one OPTIONAL domain fail-open under its own savepoint.

    The shared home for the in-build optional-domain pattern (banners, skins, and
    whatever domain lands next -- this helper exists precisely because each new one
    copied the last). ``do_import`` runs inside a
    :func:`~arknights_mcp.util.sqlite.savepoint` named ``domain``; on
    :class:`ImporterError` -- the domain's own non-empty-or-fail guard, or a
    constraint anomaly -- that savepoint's writes (partial domain rows AND their
    provenance) roll back, the failure is warned once, and ``empty()`` supplies the
    typed zero result so the build continues.

    Fail-open is bounded to domains OUTSIDE ``CRITICAL_TABLES``, where an empty
    domain is a legitimate state; the mandatory combat core stays fail-closed.
    Only :class:`ImporterError` is caught -- a programming error or a
    :class:`sqlite3.DatabaseError` still tears the build down rather than silently
    shipping a domain-less build under a warning.

    The savepoint name is the ``domain`` label because regions run sequentially
    (mirrors ``cli.sync._ride_along``'s fixed name); ``describe`` supplies the
    per-domain half of the warning ("banner archive").
    """
    try:
        with savepoint(conn, domain):
            return do_import()
    except ImporterError as exc:
        _LOG.warning(
            "%s: %s unavailable, skipped; continuing combat build: %s",
            server,
            describe,
            exc,
        )
        return empty()
