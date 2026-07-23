"""``sync`` command: build a candidate from an allowlisted remote source (§T21).

Network access happens only here -- never at query time (§V1). Each region must
resolve to a distinct upstream tree so en/cn data is never silently mixed (§V5).
"""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
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
from arknights_mcp.importers.announcements import import_announcements
from arknights_mcp.importers.penguin_drops import (
    import_penguin_drops,
    penguin_server_for_region,
)
from arknights_mcp.importers.pipeline import ServerImport
from arknights_mcp.importers.search_index import rebuild_search_index
from arknights_mcp.sources.announcements import (
    AnnouncementsAdapter,
    source_id_for_region,
)
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
from arknights_mcp.util.sqlite import savepoint
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


def _announcement_feed_url(config: AppConfig, source_id: str) -> str | None:
    """Resolve an announcement feed URL from ``[sync.<source_id>].feed_url`` (§T106).

    Unlike penguin (which has a documented default endpoint), the official feed URL
    is operator-supplied and has no shipped default -- an unset/placeholder value
    returns ``None`` so the ride-along skips that region rather than fetching a
    guessed URL (§V56/§V1).
    """
    source_cfg = config.sync.sources.get(source_id)
    feed_url = source_cfg.feed_url if source_cfg is not None else ""
    return None if is_placeholder(feed_url) else feed_url


def _ride_along(
    candidate: Path,
    *,
    servers: Sequence[str],
    label: str,
    per_server: Callable[[sqlite3.Connection, str], str | None],
    after_all: Callable[[sqlite3.Connection], None] | None = None,
) -> None:
    """Import an optional per-region ride-along source into the candidate (§V58/§V37).

    The one shared home for the penguin-drop (§T102), announcement (§T106), and
    extra-locale alias (§T109) ride-alongs: each fetches a secondary source AFTER the
    game-data import, into the SAME candidate, before validate/promote, so the extra
    rows join one atomic build (§V4/§V58). The candidate connection runs in autocommit
    mode so each region's ``RELEASE`` commits its rows independently, and each region is
    imported under a per-server :func:`~arknights_mcp.util.sqlite.savepoint` (the single
    §V37 home for the savepoint dance, shared with the main-build banner isolation §T116)
    so a fetch/import failure rolls back only THAT region's partial inserts (all-or-nothing
    per server, §V58).

    Fail-open (§V58, must not break §V3): a failure of ANY kind (network, importer
    :class:`ImporterError` incl. its own §V30 non-empty-or-fail, a duplicate collision,
    or a malformed payload surfacing as ``ValueError``/``OverflowError``) re-raises out of
    the ``savepoint`` helper (which has already rolled back the region's partial inserts)
    and is caught + warned by this loop, so the build continues game-data-only -- the
    ride-along tables are outside CRITICAL_TABLES, so an empty domain is legitimate. The
    catch is deliberately broad rather than a fixed error tuple, because fail-open is the
    whole point.

    ``per_server`` runs on the writable candidate for one region and returns a success
    message to print, or ``None`` to skip the region without a success line (a region
    with no source for this ride-along, or an opt-out that the callback logged itself).

    ``after_all`` (optional) runs once on the candidate after every region has been
    processed -- used by the extra-locale ride-along to rebuild ``entity_fts`` from the
    surviving rows so the freshly-imported jp/kr aliases become searchable (§T109/§V37,
    the single FTS-rebuild home). It runs under its OWN savepoint with the same fail-open
    catch: if the post-step fails, its savepoint is rolled back (restoring the index the
    game-data build already populated) and the build continues -- a broken post-step must
    not defeat §V3 either.
    """
    conn = sqlite3.connect(str(candidate), isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for server in servers:
            try:
                with savepoint(conn, "ride_along"):
                    message = per_server(conn, server)
                if message is not None:
                    _out(f"  {message}")
            except Exception as exc:
                # The savepoint helper already rolled back this region's partial inserts;
                # swallow the failure and keep going (fail-open, §V58/§V3).
                _out(
                    f"  {label} {server}: unavailable, skipped; "
                    f"continuing game-data-only (§V58): {exc}"
                )
        if after_all is not None:
            try:
                with savepoint(conn, "ride_along_after"):
                    after_all(conn)
            except Exception as exc:
                _out(
                    f"  {label}: post-import step failed, skipped; "
                    f"continuing game-data-only (§V58): {exc}"
                )
    finally:
        conn.close()


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
    with no penguin server is skipped silently), then imports that server's drops under
    the shared per-server-savepoint, fail-open ride-along (:func:`_ride_along`, §V37):
    a penguin failure of any kind rolls back only THAT region and the build continues
    game-data-only. ``items``/``stage_drops`` are outside CRITICAL_TABLES (0009), so an
    empty drops domain is legitimate -- never a §V30 combat regression.
    """
    if _PENGUIN_SOURCE_ID not in config.sync.enabled_sources:
        return
    entry = registry.get(_PENGUIN_SOURCE_ID)
    if entry is None or not entry.enabled:
        return

    base_url = _penguin_base_url(config)

    def _import(conn: sqlite3.Connection, server: str) -> str | None:
        penguin_server = penguin_server_for_region(server)
        if penguin_server is None:
            # A region with no penguin server (never en/cn here, defensive) is
            # skipped silently rather than mislabelled (§V54/§V58).
            return None
        adapter = PenguinStatsAdapter(base_url, fetcher=fetcher, limits=limits, budget=budget)
        result = import_penguin_drops(conn, adapter, penguin_server=penguin_server)
        return f"penguin {server}: drops {result.drops_inserted}, skipped {result.drops_skipped}"

    _ride_along(candidate, servers=servers, label="penguin", per_server=_import)


def _announcement_region_eligible(config: AppConfig, registry: SourceRegistry, server: str) -> bool:
    """Whether one region actually has an announcement feed to fetch (§V56/§V58).

    Eligible == the region's source id resolves AND the source is both in
    ``[sync].enabled_sources`` and registry-enabled AND a ``feed_url`` is configured.
    The operator-supplied feed URL has no shipped default, so an enabled-but-unconfigured
    source has nothing to fetch -- treated as "off" here so the ride-along skips the
    write connection entirely (the same "off by default until a feed_url is set" state
    the efficiency pre-check guards against).
    """
    source_id = source_id_for_region(server)
    if source_id is None:
        return False
    if source_id not in config.sync.enabled_sources:
        return False
    entry = registry.get(source_id)
    if entry is None or not entry.enabled:
        return False
    return _announcement_feed_url(config, source_id) is not None


def _ride_along_announcements(
    candidate: Path,
    *,
    config: AppConfig,
    registry: SourceRegistry,
    servers: Sequence[str],
    fetcher: Fetcher | None,
    limits: DownloadLimits,
    budget: DownloadBudget,
) -> None:
    """Import announcement metadata into the candidate before validation (§V56/§T106).

    Mirrors the penguin ride-along (:func:`_ride_along`, §V37). Per region it resolves
    the announcement source id (en->Global, cn->CN; a region outside {en,cn} has none)
    and runs only when that source is both in ``[sync].enabled_sources`` and
    registry-enabled AND a ``[sync.<source_id>].feed_url`` is configured -- the official
    feed URL has no shipped default, so an unset feed skips that region rather than
    guessing a URL (§V56/§V1). The metadata-only importer stores only the §V56 allowlist;
    the article body is never fetched into storage (§V16).

    Fail-open (§V58, must not break §V3): a feed/import failure rolls back only THAT
    region and the build continues game-data-only -- ``announcements`` is outside
    CRITICAL_TABLES (0010), so an empty announcement domain is legitimate.

    Mirrors penguin's pre-check: if NO region is eligible (source enabled +
    registry-enabled + a configured ``feed_url``), return before ``_ride_along`` opens a
    writable connection and churns per-server savepoints for no work. The announcement
    source ships enabled by default (§V56) but the operator-supplied ``feed_url`` has no
    default, so a fresh install has nothing to fetch -- the hot promote path should not
    pay for a write connection on every such sync.
    """
    if not any(_announcement_region_eligible(config, registry, server) for server in servers):
        return

    def _import(conn: sqlite3.Connection, server: str) -> str | None:
        source_id = source_id_for_region(server)
        if source_id is None:
            # No announcement source for this region (never en/cn here, defensive).
            return None
        if source_id not in config.sync.enabled_sources:
            return None
        entry = registry.get(source_id)
        if entry is None or not entry.enabled:
            return None
        feed_url = _announcement_feed_url(config, source_id)
        if feed_url is None:
            # Enabled but no feed URL configured yet -- skip rather than fetch a
            # guessed endpoint (§V56/§V1). Logged so the operator sees why it is empty.
            _out(
                f"  announcements {server}: no feed_url configured "
                f"([sync.{source_id}].feed_url); skipped"
            )
            return None
        adapter = AnnouncementsAdapter(
            feed_url, server, fetcher=fetcher, limits=limits, budget=budget
        )
        result = import_announcements(conn, adapter, region=server)
        return (
            f"announcements {server}: {result.announcements_inserted} "
            f"(skipped {result.announcements_skipped})"
        )

    _ride_along(candidate, servers=servers, label="announcements", per_server=_import)


def _reindex_after_ride_alongs(candidate: Path) -> None:
    """Rebuild ``entity_fts`` after the post-build ride-alongs so items are searchable.

    The pipeline builds the FTS index (:func:`build_search_index`) at the end of the
    game-data import -- BEFORE the penguin ride-along imports the ``items`` table
    (§V58/§T102). Items are a searchable entity domain (§V73/§T142: an item locator's
    game_id feeds ``get_item_drops``), so the freshly-imported item rows would be
    invisible to ``search_entities(entity_type=item)`` unless the index is rebuilt from
    the now-complete row set. This is the single §V37 rebuild home
    (:func:`rebuild_search_index`), run once after every ride-along has settled (B83:
    a build that predates the item FTS rows dead-ends the drops name->id path).

    Fail-open under its own savepoint (§V58, must not defeat §V3): a rebuild failure is
    caught + warned and rolled back to the game-data index the pipeline already built --
    a broken post-step degrades item search, it never blocks the promote.
    """
    conn = sqlite3.connect(str(candidate), isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        with savepoint(conn, "reindex_after"):
            rebuild_search_index(conn)
    except Exception as exc:
        _out(f"  search index: rebuild failed, skipped; continuing (§V58): {exc}")
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
                _ride_along_announcements(
                    candidate,
                    config=config,
                    registry=registry,
                    servers=servers,
                    fetcher=ctx.fetcher,
                    limits=limits,
                    budget=budget,
                )
                # Rebuild the FTS index once the ride-alongs have settled so penguin
                # items land in ``search_entities`` (§V73/B83); the pipeline built the
                # index before the item rows existed.
                _reindex_after_ride_alongs(candidate)

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
