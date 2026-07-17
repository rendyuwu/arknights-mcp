"""Read-only SQLite access + explicit migrations.

Public read path: :func:`~arknights_mcp.db.connection.open_read_only` (§V2) and
the parameterized repositories in :mod:`arknights_mcp.db.repositories`. The
writable migration runner (:mod:`arknights_mcp.db.migrations`) is CLI/build-only.
"""

from arknights_mcp.db.connection import (
    DatabaseUnavailable,
    open_read_only,
    read_only_connection,
)

__all__ = ["DatabaseUnavailable", "open_read_only", "read_only_connection"]
