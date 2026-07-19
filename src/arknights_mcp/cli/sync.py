"""``sync`` command: build a candidate from an allowlisted remote source (§T21).

Network access happens only here -- never at query time (§V1). Each region must
resolve to a distinct upstream tree so en/cn data is never silently mixed (§V5).
"""

from __future__ import annotations

import argparse
import tempfile
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
from arknights_mcp.importers.pipeline import ServerImport
from arknights_mcp.sources.arknights_assets import (
    ArknightsAssetsAdapter,
    DownloadBudget,
    DownloadLimits,
)
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
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
            imports.append(ServerImport(server=server, adapter=local, source_id=_PRIMARY_SOURCE_ID))
        return _build_validate_promote(config, registry, imports, servers=servers)
