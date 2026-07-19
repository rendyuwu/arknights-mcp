"""Application core / service container shared by both transports (§V14; §T47).

Single home for wiring the read-only data path to the shared MCP tool registry.
Both transports (local ``stdio`` §T47, Streamable HTTP §T51) call
:func:`build_application`, so they dispatch the identical tool set over the
identical connection policy (§V14) -- there is no per-transport core to drift.

The active database is the promoted, immutable build selected by ``current.json``
(§T24). It is opened strictly read-only (§V2) and *lazily*: the connection is
created on first tool call and reused for the process lifetime, so a server that
starts before any build is promoted still runs -- its tools fail closed to a typed
``database_unavailable`` result (§V23) until a build exists, rather than refusing
to start.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from arknights_mcp.config import AppConfig
from arknights_mcp.db.connection import DatabaseUnavailable, open_read_only
from arknights_mcp.db.promotion import resolve_active_database
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.sources.registry import load_source_registry


class ActiveDatabaseProvider:
    """Lazily open + cache the process-wide read-only connection (§V2/§V14).

    A :data:`~arknights_mcp.mcp.tools._shared.ConnectionProvider`: every tool
    handler calls it to obtain the one shared connection. The promoted build is
    resolved from ``current.json`` on first use; when nothing is promoted (or the
    referenced build file is missing) it raises
    :class:`~arknights_mcp.db.connection.DatabaseUnavailable`, which the shared
    tool guard maps to a typed ``database_unavailable`` envelope (§V23) instead of
    a startup failure.

    The connection is created on first use and reused; the local ``stdio`` loop
    is single-threaded, so it is opened and used from the same thread.

    The active build is resolved from ``current.json`` exactly once (on first use)
    and held for the process lifetime -- a build promoted *after* the server has
    opened its connection is not picked up live (the open fd survives the retention
    unlink on Linux, so the process keeps serving the old build). Refreshing the
    served data therefore requires a server restart after ``import``/``sync``; the
    client setup docs' troubleshooting says so.
    """

    def __init__(self, data_dir: str, current_manifest: str | None) -> None:
        self._data_dir = data_dir
        self._current_manifest = current_manifest
        self._conn: sqlite3.Connection | None = None

    def __call__(self) -> sqlite3.Connection:
        if self._conn is None:
            db_path = resolve_active_database(self._data_dir, self._current_manifest)
            if db_path is None:
                raise DatabaseUnavailable("no active database has been promoted")
            self._conn = open_read_only(db_path)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


@dataclass(frozen=True)
class ApplicationCore:
    """The shared read-only core both transports serve from (§V14)."""

    config: AppConfig
    registry: ToolRegistry
    provider: ActiveDatabaseProvider


def build_application(config: AppConfig) -> ApplicationCore:
    """Assemble the shared core: read-only connection provider + tool registry.

    One home for the core wiring (§V14/§V37): both transports call this so they
    dispatch the same registry over the same connection policy. No network, no
    write handle (§V1/§V2). The machine source registry is loaded here *once* and
    held in the tool closure for the process lifetime, so the enabled/disabled
    posture ``get_data_sources`` reports is captured at startup: a ``source
    enable``/``disable`` run against a live server (§V20 kill switch) is picked up
    only on restart, mirroring the active-build refresh policy on
    :class:`ActiveDatabaseProvider`. The deployment mode is threaded to
    ``get_data_status`` (§T77).
    """
    provider = ActiveDatabaseProvider(
        config.database.data_dir,
        config.database.current_manifest,
    )
    source_registry = load_source_registry(config.source_registry.machine_registry)
    registry = build_tool_registry(
        provider,
        registry=source_registry,
        mode=config.deployment_mode,
    )
    return ApplicationCore(config=config, registry=registry, provider=provider)
