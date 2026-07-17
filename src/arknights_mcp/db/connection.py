"""Read-only SQLite connection factory (§V2; §T20).

Every MCP process opens the promoted, immutable build **strictly read-only**: no
tool may write, run arbitrary SQL, or reach the filesystem/network through the
database. Enforcement is layered so a write cannot slip through silently:

* the connection is opened via the SQLite URI ``mode=ro`` handle, which refuses
  to create a missing file and rejects any write with
  ``attempt to write a readonly database``;
* ``PRAGMA query_only = ON`` pins the same intent at the connection level.

Query values are always bound through ``?`` placeholders; the ``db.repositories``
layer is the only sanctioned SQL surface (never string interpolation). This
module opens connections; it does not resolve the active build path from the
``current.json`` manifest -- that promotion logic is §T24.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class DatabaseUnavailable(RuntimeError):
    """The read-only database could not be opened (maps to §V23 ``database_unavailable``).

    Carries only the file *name*, never a full local path, so the message is safe
    to surface without leaking the filesystem layout (§V23).
    """


def open_read_only(db_path: str | Path) -> sqlite3.Connection:
    """Open ``db_path`` as a strictly read-only SQLite connection (§V2).

    Fails closed: a missing file raises :class:`DatabaseUnavailable` rather than
    letting SQLite create an empty database. Any subsequent write on the returned
    connection raises :class:`sqlite3.OperationalError`.
    """
    path = Path(db_path).resolve()
    if not path.is_file():
        raise DatabaseUnavailable(f"database not found: {path.name}")
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        # Belt-and-suspenders alongside mode=ro: reject writes at the connection
        # level too. Read-only pragma, not a database write, so it is permitted.
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:  # pragma: no cover - defensive, exercised via missing-file path
        raise DatabaseUnavailable(f"cannot open database: {path.name}") from exc
    return conn


@contextmanager
def read_only_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Context-managed :func:`open_read_only` connection, closed on exit."""
    conn = open_read_only(db_path)
    try:
        yield conn
    finally:
        conn.close()
