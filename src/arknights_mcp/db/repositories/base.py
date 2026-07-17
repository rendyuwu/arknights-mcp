"""Base class for read-only repositories (§V2; §T20).

A repository is the *only* sanctioned SQL surface for the domain services: it
holds a SQLite connection and executes SQL kept as module-level constants, with
every runtime value bound through ``?`` placeholders. No method accepts
caller-supplied SQL and nothing is interpolated into a query string, so callers
cannot smuggle arbitrary SQL through this layer (§V2). Repositories only read;
combined with the read-only connection from :mod:`arknights_mcp.db.connection`,
a write is impossible from an MCP process.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any


class Repository:
    """Holds a connection and runs parameterized read-only queries (§V2)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _one(self, sql: str, params: Sequence[object] = ()) -> Any:
        """Return the first row of a parameterized ``SELECT`` (or ``None``)."""
        return self._conn.execute(sql, tuple(params)).fetchone()

    def _all(self, sql: str, params: Sequence[object] = ()) -> list[Any]:
        """Return every row of a parameterized ``SELECT``."""
        return self._conn.execute(sql, tuple(params)).fetchall()
