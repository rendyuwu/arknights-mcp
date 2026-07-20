"""``sync`` command: build a candidate from an allowlisted remote source (§T21).

Network access happens only here -- never at query time (§V1). Each region must
resolve to a distinct upstream tree so en/cn data is never silently mixed (§V5).
"""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from collections.abc import Sequence
from pathlib import Path

from arknights_mcp.cli._shared import (
    _PRIMARY_SOURCE_ID,
    CliContext,
    _build_validate_promote,
    _err,
    _load,
    _out,
)
from arknights_mcp.config import AppConfig
from arknights_mcp.importers.penguin_drops import (
    import_penguin_drops,
    penguin_server_for_region,
)
from arknights_mcp.importers.pipeline import ServerImport
from arknights_mcp.sources.arknights_assets import ArknightsAssetsAdapter
from arknights_mcp.sources.http_fetch import DownloadBudget, DownloadLimits, Fetcher
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.penguin_statistics import (
    DEFAULT_SOURCE_ID as _PENGUIN_SOURCE_ID,
)
from arknights_mcp.sources.penguin_statistics import (
    PENGUIN_BASE_URL,
    PenguinStatsAdapter,
)
from arknights_mcp.sources.registry import SourceRegistry
from arknights_mcp.util.text import is_placeholder


def _resolve_servers(server: str) -> list[str]:
    return ["en", "cn"] if server == "all" else [server]


def _download_limits(config: AppConfig) -> DownloadLimits:
    """Derive the sync download caps from config so the operator knob is live.

    ``[sync].max_total_download_mb`` bounds the *whole run* (all servers), in MiB,
    replacing the hardcoded default so the configured value actually takes effect
    (PRD §17.4).
    """
    return DownloadLimits(max_total_bytes=config.sync.max_total_download_mb * 1024 * 1024)


def _penguin_base_url(config: AppConfig) -> str:
    """Resolve the penguin API base URL from ``[sync.penguin_statistics].base_url``.

    Folded via the existing :class:`SyncSourceConfig`; an unset/placeholder value
    falls back to the documented default endpoint (§T102).
    """
    penguin_cfg = config.sync.sources.get(_PENGUIN_SOURCE_ID)
    base_url = penguin_cfg.base_url if penguin_cfg is not None else ""
    return PENGUIN_BASE_URL if is_placeholder(base_url) else base_url


def _ride_along_penguin(
    candidate: Path,
    *,
    config: AppConfig,
    registry: SourceRegistry,
    servers: Sequence[str],
    fetcher: Fetcher | None,
    limits: DownloadLimits,
    budget: DownloadBudget,
) -> None:
    """Import penguin drops into the freshly-built candidate before validation (§V58).

    Runs only when ``penguin_statistics`` is both in ``[sync].enabled_sources`` and
    registry-enabled -- otherwise it fetches nothing (§V58 opt-in). Per region it maps
    the fact region back to its penguin server (inverse §V54: en->US, cn->CN; a region
    with no penguin server is skipped silently) and imports that server's drops under a
    per-server ``SAVEPOINT`` so a fetch/import failure rolls back only THAT region's
    partial inserts (all-or-nothing per server, §V58).

    Fail-open (§V58, must not break §V3): a penguin network failure, an
    :class:`ImporterError` (including its own §V30 non-empty-or-fail), or a duplicate
    collision is caught + warned, the region's savepoint is rolled back, and the build
    continues game-data-only. ``items``/``stage_drops`` are outside the critical tables
    (0009), so an empty drops domain is legitimate -- never a §V30 combat regression.

    The candidate connection runs in autocommit mode so each region's ``RELEASE``
    commits its drops independently, keeping the per-region rollback isolated.
    """
    if _PENGUIN_SOURCE_ID not in config.sync.enabled_sources:
        return
    entry = registry.get(_PENGUIN_SOURCE_ID)
    if entry is None or not entry.enabled:
        return

    base_url = _penguin_base_url(config)
    conn = sqlite3.connect(str(candidate), isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for server in servers:
            penguin_server = penguin_server_for_region(server)
            if penguin_server is None:
                # A region with no penguin server (never en/cn here, defensive) is
                # skipped silently rather than mislabelled (§V54/§V58).
                continue
            try:
                conn.execute("SAVEPOINT penguin_drops")
                adapter = PenguinStatsAdapter(
                    base_url, fetcher=fetcher, limits=limits, budget=budget
                )
                result = import_penguin_drops(conn, adapter, penguin_server=penguin_server)
                conn.execute("RELEASE SAVEPOINT penguin_drops")
                _out(
                    f"  penguin {server}: drops {result.drops_inserted}, "
                    f"skipped {result.drops_skipped}"
                )
            except Exception as exc:
                # Roll back only this region's partial inserts and keep going: a
                # penguin failure of ANY kind (network, import, a malformed payload
                # surfacing as ValueError/OverflowError, ...) must not fail the
                # game-data build (§V58/§V3). Fail-open is the whole point of the
                # ride-along, so the catch is deliberately broad rather than a fixed
                # error tuple, and SAVEPOINT creation is inside the guard too.
                try:
                    # If the failure already aborted the transaction the savepoint is
                    # gone (its inserts already undone), and if SAVEPOINT itself failed
                    # there is nothing to release -- swallow the cleanup error rather
                    # than let it defeat fail-open.
                    conn.execute("ROLLBACK TO SAVEPOINT penguin_drops")
                    conn.execute("RELEASE SAVEPOINT penguin_drops")
                except sqlite3.Error:
                    pass
                _out(
                    f"  penguin {server}: unavailable, drops skipped; "
                    f"continuing game-data-only (§V58): {exc}"
                )
    finally:
        conn.close()


def _cmd_sync(args: argparse.Namespace, ctx: CliContext) -> int:
    config, registry = _load(args)
    if not config.sync.allow_remote_download:
        _err("remote download disabled in config ([sync].allow_remote_download = false)")
        return 1
    entry = registry.get(_PRIMARY_SOURCE_ID)
    if entry is None or not entry.enabled:
        _err(f"source {_PRIMARY_SOURCE_ID!r} is disabled; enable it before syncing (§V20)")
        return 1
    source_cfg = config.sync.sources.get(_PRIMARY_SOURCE_ID)
    servers = _resolve_servers(args.server)

    # Resolve a per-region base_url; each region must point at a distinct upstream
    # tree so en/cn data is never silently mixed (§V5).
    resolved: dict[str, str] = {}
    for server in servers:
        url = source_cfg.base_url_for(server) if source_cfg is not None else ""
        if is_placeholder(url):
            _err(
                f"no allowlisted base_url configured for {_PRIMARY_SOURCE_ID!r} "
                f"server {server!r} ([sync.{_PRIMARY_SOURCE_ID}])"
            )
            return 1
        resolved[server] = url
    if len(set(resolved.values())) != len(resolved):
        _err(
            "multiple servers resolve to the same base_url; refusing to import "
            "identical upstream data under different region labels (§V5). "
            "Configure a distinct [sync."
            f"{_PRIMARY_SOURCE_ID}].base_urls entry per region or use a {{server}} token."
        )
        return 1

    limits = _download_limits(config)
    budget = DownloadBudget(limits.max_total_bytes)
    _out(f"sync: {_PRIMARY_SOURCE_ID} servers={','.join(servers)}")
    try:
        with tempfile.TemporaryDirectory(prefix="arkmcp-staging-") as staging:
            imports: list[ServerImport] = []
            for server in servers:
                adapter = ArknightsAssetsAdapter(
                    resolved[server],
                    server,
                    fetcher=ctx.fetcher,
                    limits=limits,
                    budget=budget,
                    max_parallel=config.sync.max_parallel_downloads,
                )
                local: LocalSnapshotAdapter = adapter.stage(Path(staging) / server)
                imports.append(
                    ServerImport(server=server, adapter=local, source_id=_PRIMARY_SOURCE_ID)
                )

            def _post_build(candidate: Path) -> None:
                _ride_along_penguin(
                    candidate,
                    config=config,
                    registry=registry,
                    servers=servers,
                    fetcher=ctx.fetcher,
                    limits=limits,
                    budget=budget,
                )

            return _build_validate_promote(
                config, registry, imports, servers=servers, post_build=_post_build
            )
    finally:
        # Release any keep-alive sockets the fetcher opened across worker threads
        # (§T79): the pool threads have exited, so only the fetcher's own registry
        # can close them. Not every Fetcher keeps connections (e.g. a test double).
        close = getattr(ctx.fetcher, "close", None)
        if callable(close):
            close()
