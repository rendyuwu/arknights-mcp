"""T20: the read-only SQLite connection factory (§V2).

``open_read_only`` is the concrete enforcement point for §V2 -- every MCP
process opens the promoted build through it, and a write must be impossible:

* a ``SELECT`` succeeds;
* any write raises ``sqlite3.OperationalError`` (opened ``mode=ro``);
* ``PRAGMA query_only`` is pinned ON;
* a missing database fails closed with :class:`DatabaseUnavailable` (never a
  silently-created empty file);
* the context manager closes the connection on exit.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.connection import (
    DatabaseUnavailable,
    open_read_only,
    read_only_connection,
)
from arknights_mcp.db.migrations import build_database


def _built_db(tmp_path: Path) -> Path:
    """A migrated but empty candidate DB on disk, writer closed."""
    db_path = tmp_path / "cand.sqlite"
    conn = build_database(db_path)
    conn.close()
    return db_path


def test_open_read_only_allows_select(tmp_path: Path) -> None:
    conn = open_read_only(_built_db(tmp_path))
    try:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        assert rows  # migrations recorded
    finally:
        conn.close()


def test_open_read_only_rejects_writes(tmp_path: Path) -> None:
    # §V2: the connection cannot mutate the database.
    conn = open_read_only(_built_db(tmp_path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, checksum) "
                "VALUES ('x', 'y', 'z')"
            )
    finally:
        conn.close()


def test_query_only_pragma_is_on(tmp_path: Path) -> None:
    conn = open_read_only(_built_db(tmp_path))
    try:
        (value,) = conn.execute("PRAGMA query_only").fetchone()
        assert value == 1
    finally:
        conn.close()


def test_missing_database_fails_closed(tmp_path: Path) -> None:
    # §V2/§V23: no silently-created empty DB; a typed error naming only the file.
    missing = tmp_path / "absent.sqlite"
    with pytest.raises(DatabaseUnavailable) as excinfo:
        open_read_only(missing)
    assert "absent.sqlite" in str(excinfo.value)
    assert str(tmp_path) not in str(excinfo.value)  # no local path leak
    assert not missing.exists()  # not created as a side effect


def test_read_only_connection_context_manager_closes(tmp_path: Path) -> None:
    with read_only_connection(_built_db(tmp_path)) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")  # closed on exit
