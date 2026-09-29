"""Shared sqlite constraint-error guard (fail-closed).

A duplicate, absent, or otherwise anomalous source constraint (dup PK, repeated
variant, missing index) surfaces mid-build as a low-level ``sqlite3.IntegrityError``.
Left uncaught it tears down the whole multi-region candidate build with a raw
traceback. Every importer + purge write path guarded against this
with the *same* translate-and-reraise pattern; that pattern now lives here exactly
once.

The variance between the former copies -- which typed error to raise, the message,
and any cleanup (e.g. a transaction rollback) -- is passed explicitly, never forked
into silent divergent copies.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager

#: A bare SQL identifier -- savepoint names cannot be parameter-bound, so they are
#: interpolated; a non-identifier is rejected rather than concatenated (defence in
#: depth, even though every caller passes a literal constant).
_SAVEPOINT_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


@contextmanager
def savepoint(conn: sqlite3.Connection, name: str) -> Iterator[None]:
    """Run a block under a SQLite ``SAVEPOINT``, scoping only its writes.

    The single shared home for the ``SAVEPOINT``/``RELEASE``/``ROLLBACK TO`` dance used
    by the optional per-region ride-along sources (``cli.sync._ride_along``) and by
    the savepoint-isolated banner archive in the main build pipeline. On
    success the savepoint is ``RELEASE``d; on ANY exception its writes are rolled back --
    leaving every row written *before* the block intact -- and the exception re-raises so
    the caller decides whether to fail-open or fail-closed. This helper only scopes the
    writes; it never swallows the error. A cleanup failure (the failing statement already
    aborted the surrounding transaction, or ``SAVEPOINT`` creation itself failed) is
    swallowed so it cannot mask the original error.

    Works both inside an open transaction (the pipeline's candidate connection, where the
    savepoint nests) and in autocommit mode (the ride-along connection, where the outermost
    ``SAVEPOINT`` starts a transaction that ``RELEASE`` commits).
    """
    if not _SAVEPOINT_NAME.match(name):
        raise ValueError(f"invalid savepoint name: {name!r}")
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            conn.execute(f"RELEASE SAVEPOINT {name}")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {name}")


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True when ``name`` exists as a table (single home for the probe).

    The shared ``sqlite_master`` presence check used wherever a code path must
    tolerate a database built before the migration that adds an optional table
    (e.g. ``operator_skins``, migration 0014): the migration runner, purge, and
    any repository degrade path all ask the same question, so it lives once here
    rather than as scattered per-module copies.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True when ``table.column`` exists (single home, column-level sibling).

    Same job as :func:`table_exists` one level down: a read path must tolerate an
    ACTIVE build made before a migration that only ADDS a column (e.g.
    ``zones.event_name``, migration 0015) -- the server may be serving such a build
    while the new code is already deployed. ``PRAGMA table_info`` takes the
    table name as an identifier, so it is passed through a bound query on
    ``pragma_table_info`` rather than interpolated.
    """
    row = conn.execute(
        "SELECT 1 FROM pragma_table_info(?) WHERE name = ?",
        (table, column),
    ).fetchone()
    return row is not None


@contextmanager
def integrity_guard(
    message: str | Callable[[sqlite3.IntegrityError], str],
    error_type: type[Exception],
    *,
    on_error: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Translate a ``sqlite3.IntegrityError`` raised in the block into ``error_type``.

    ``message`` is either a fixed prefix -- the raised message is then
    ``f"{message}: {exc}"`` -- or a callable given the caught exception that returns
    the full message. ``on_error``, when set, runs before re-raising (used to roll
    back an open transaction). Non-``IntegrityError`` exceptions propagate untouched.
    """
    try:
        yield
    except sqlite3.IntegrityError as exc:
        if on_error is not None:
            on_error()
        text = message(exc) if callable(message) else f"{message}: {exc}"
        raise error_type(text) from exc
