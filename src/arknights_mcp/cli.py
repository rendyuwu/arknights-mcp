"""``arknights-mcp`` command-line entry point (admin-only).

Admin operations (``sync``, ``import``, ``validate``, ``status``, ``doctor``,
``source ...``) are CLI-only and are never exposed as MCP tools (§V28). Network
access happens only here, in ``sync`` -- never at query time (§V1). Every build
produces a *candidate* that is promoted only after it passes validation, and the
active database is never mutated in place (§V3, §V4).

Subcommands land per SPEC.md §T task: ``sync`` (§T21), ``import`` (§T22),
``validate`` (§T23), ``status``/``doctor`` (§T25), ``source ...`` (§T26).
``serve`` is wired later (§T29/§T51).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from arknights_mcp.config import AppConfig, ConfigError, load_config
from arknights_mcp.db.migrations import default_migrations_dir
from arknights_mcp.db.policy_events import read_events
from arknights_mcp.db.promotion import PromotionError, promote_candidate
from arknights_mcp.db.validate import format_report, validate_database
from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.sources.arknights_assets import ArknightsAssetsAdapter, Fetcher
from arknights_mcp.sources.base import SourceAdapterError
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import RegistryError, SourceRegistry, load_source_registry

DEFAULT_CONFIG_PATH = "config.toml"
_PRIMARY_SOURCE_ID = "arknights_assets_gamedata"
_LOCAL_SOURCE_ID = "local_snapshot"

# Errors that map to a clean CLI failure (exit 1) rather than a traceback; the
# text never includes secrets or full local paths (§V12/§V23).
_HANDLED_ERRORS = (
    ConfigError,
    RegistryError,
    SourceAdapterError,
    PromotionError,
    ImporterError,
    ValueError,
    FileNotFoundError,
)


@dataclass
class CliContext:
    """Cross-cutting dependencies injectable for testing (e.g. a fake fetcher)."""

    fetcher: Fetcher | None = None


def _err(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _out(message: str) -> None:
    print(message)


def _resolve_servers(server: str) -> list[str]:
    return ["en", "cn"] if server == "all" else [server]


def _load(args: argparse.Namespace) -> tuple[AppConfig, SourceRegistry]:
    config = load_config(args.config)
    registry = load_source_registry(config.source_registry.machine_registry)
    return config, registry


def _expected_schema_version() -> str | None:
    files = sorted(default_migrations_dir().glob("[0-9]*.sql"))
    return files[-1].stem if files else None


def _is_placeholder(value: str) -> bool:
    stripped = value.strip()
    return not stripped or (stripped.startswith("<") and stripped.endswith(">"))


def _build_validate_promote(
    config: AppConfig,
    registry: SourceRegistry,
    imports: Sequence[ServerImport],
    *,
    servers: Sequence[str],
    min_snapshots: int = 1,
) -> int:
    """Build a candidate from ``imports``, validate it, and promote iff it passes.

    Fail-closed (§V3/§V4): on a validation failure the candidate is discarded and
    ``current.json`` is left untouched, so the active database stays active.
    """
    data_dir = config.database.data_dir
    policy_events = read_events(data_dir)
    with tempfile.TemporaryDirectory(prefix="arkmcp-candidate-") as tmp:
        candidate = Path(tmp) / "candidate.sqlite"
        result = build_candidate(
            candidate,
            imports,
            registry=registry,
            policy_events=policy_events,
        )
        for snap in result.snapshots:
            _out(
                f"  imported {snap.server}: {snap.enemies} enemies, "
                f"{snap.stages} stages, {snap.zones} zones ({snap.snapshot_id})"
            )
        report = validate_database(
            candidate,
            expected_schema_version=_expected_schema_version(),
            min_snapshots=min_snapshots,
        )
        if not report.passed:
            _err("candidate failed validation; active database left unchanged (§V3/§V4)")
            print(format_report(report), file=sys.stderr)
            return 1
        promotion = promote_candidate(
            candidate,
            data_dir=data_dir,
            validation_passed=report.passed,
            servers=tuple(servers),
            retain_versions=config.sync.retain_versions,
            current_manifest_path=config.database.current_manifest,
        )
    if promotion.status == "noop":
        _out(f"unchanged: active build stays {promotion.manifest.database_filename} (no-op)")
    else:
        _out(f"promoted: {promotion.manifest.database_filename}")
        if promotion.pruned:
            _out(f"  pruned {len(promotion.pruned)} old build(s)")
    return 0


# --- sync (§T21) --------------------------------------------------------------


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
    base_url = source_cfg.base_url if source_cfg is not None else ""
    if _is_placeholder(base_url):
        _err(
            f"no allowlisted base_url configured for {_PRIMARY_SOURCE_ID!r} "
            f"([sync.{_PRIMARY_SOURCE_ID}])"
        )
        return 1

    servers = _resolve_servers(args.server)
    _out(f"sync: {_PRIMARY_SOURCE_ID} servers={','.join(servers)}")
    with tempfile.TemporaryDirectory(prefix="arkmcp-staging-") as staging:
        imports: list[ServerImport] = []
        for server in servers:
            adapter = ArknightsAssetsAdapter(base_url, server, fetcher=ctx.fetcher)
            local: LocalSnapshotAdapter = adapter.stage(Path(staging) / server)
            imports.append(ServerImport(server=server, adapter=local, source_id=_PRIMARY_SOURCE_ID))
        return _build_validate_promote(config, registry, imports, servers=servers)


# --- import (§T22) ------------------------------------------------------------


def _cmd_import(args: argparse.Namespace, ctx: CliContext) -> int:
    config, registry = _load(args)
    entry = registry.get(_LOCAL_SOURCE_ID)
    if entry is None or not entry.enabled:
        _err(f"source {_LOCAL_SOURCE_ID!r} is disabled; enable it before importing (§V20)")
        return 1

    server = args.server
    # Raises SourceAdapterError (caught -> exit 1) if the path is not a directory.
    adapter = LocalSnapshotAdapter(args.source_path, server, source_id=_LOCAL_SOURCE_ID)
    _out(f"import: {_LOCAL_SOURCE_ID} server={server} from local snapshot")
    job = ServerImport(server=server, adapter=adapter, source_id=_LOCAL_SOURCE_ID)
    return _build_validate_promote(config, registry, [job], servers=[server])


# --- parser + dispatch --------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arknights-mcp",
        description="Admin CLI for the read-only Arknights Intelligence MCP (CLI-only ops, §V28).",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="path to config.toml (default: ./config.toml)",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_sync = sub.add_parser("sync", help="build a candidate from an allowlisted remote source")
    p_sync.add_argument("--server", required=True, choices=["en", "cn", "all"])
    p_sync.set_defaults(func=_cmd_sync)

    p_import = sub.add_parser("import", help="build a candidate from a local snapshot directory")
    p_import.add_argument("--server", required=True, choices=["en", "cn"])
    p_import.add_argument("--source-path", required=True, help="path to the snapshot directory")
    p_import.set_defaults(func=_cmd_import)

    return parser


def main(argv: list[str] | None = None, *, fetcher: Fetcher | None = None) -> int:
    """Console-script entry point. ``fetcher`` is a test seam for ``sync`` (§T21)."""
    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return 2
    ctx = CliContext(fetcher=fetcher)
    try:
        return int(func(args, ctx))
    except _HANDLED_ERRORS as exc:
        _err(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "CliContext"]
