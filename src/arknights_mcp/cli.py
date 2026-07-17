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
import importlib.metadata
import json
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from arknights_mcp.config import AppConfig, ConfigError, load_config
from arknights_mcp.db.connection import read_only_connection
from arknights_mcp.db.migrations import default_migrations_dir
from arknights_mcp.db.policy_events import append_event, read_events
from arknights_mcp.db.promotion import PromotionError, promote_candidate, resolve_active_database
from arknights_mcp.db.purge import PurgeError, purge_and_rebuild
from arknights_mcp.db.validate import format_report, validate_database
from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.services.status import get_data_status
from arknights_mcp.sources.arknights_assets import ArknightsAssetsAdapter, Fetcher
from arknights_mcp.sources.base import SourceAdapterError
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import (
    RegistryError,
    SourceRegistry,
    load_source_registry,
    set_source_enabled,
)

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
    PurgeError,
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


# --- validate (§T23) ----------------------------------------------------------


def _cmd_validate(args: argparse.Namespace, ctx: CliContext) -> int:
    report = validate_database(
        args.database,
        expected_schema_version=_expected_schema_version(),
        min_snapshots=0 if args.allow_empty else 1,
    )
    _out(format_report(report))
    return 0 if report.passed else 1


# --- status + doctor (§T25) ---------------------------------------------------


def _mode(config: AppConfig) -> str:
    return "remote" if config.mcp.remote.enabled else "local"


def _active_database(config: AppConfig) -> Path | None:
    return resolve_active_database(config.database.data_dir, config.database.current_manifest)


def _cmd_status(args: argparse.Namespace, ctx: CliContext) -> int:
    config, _ = _load(args)
    active = _active_database(config)
    if active is None:
        if args.json:
            _out(json.dumps({"status": "database_unavailable", "snapshots": []}))
        else:
            _out("status: no active database — run `arknights-mcp sync` or `import`")
        return 0

    with read_only_connection(active) as conn:
        status = get_data_status(conn, mode=_mode(config))

    if args.json:
        _out(json.dumps(status.to_dict(), indent=2, sort_keys=True))
        return 0

    _out(
        f"status: {status.status} schema={status.schema_version} "
        f"analyzer={status.analyzer_version} mode={status.mode}"
    )
    for snap in status.snapshots:
        age = "unknown" if snap.age_days is None else f"{snap.age_days}d"
        _out(
            f"  {snap.server:<3} {snap.source_id:<24} {snap.snapshot_id} "
            f"imported {snap.imported_at} (age {age})"
        )
    _out(f"  domains: {', '.join(status.supported_domains) or '(none)'}")
    for warning in status.warnings:
        _out(f"  warning: {warning}")
    if status.suggested_action:
        _out(f"  action: {status.suggested_action}")
    return 0


def _mcp_sdk_version() -> str:
    try:
        return importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - always installed here
        return "unknown"


def _cmd_doctor(args: argparse.Namespace, ctx: CliContext) -> int:
    """Health report: versions, DB, sources, transport, config warnings.

    Never prints secrets or secret values (§V12): auth is reported only as a
    configured/not-configured boolean, never issuer/audience/JWKS contents.
    """
    lines: list[tuple[str, str, str]] = []

    py = ".".join(str(v) for v in sys.version_info[:3])
    lines.append(("ok", "python", py))
    lines.append(("ok", "mcp SDK", _mcp_sdk_version()))
    lines.append(("ok", "sqlite", sqlite3.sqlite_version))

    try:
        config, registry = _load(args)
    except (ConfigError, RegistryError) as exc:
        lines.append(("fail", "config/registry", str(exc)))
        _print_doctor(lines)
        return 1

    lines.append(("ok", "config", "loaded"))
    enabled = registry.enabled()
    lines.append(
        ("ok", "source registry", f"{len(registry.entries)} sources, {len(enabled)} enabled")
    )

    active = _active_database(config)
    if active is None:
        lines.append(("warn", "active database", "none promoted — run sync/import"))
    else:
        report = validate_database(
            active, expected_schema_version=_expected_schema_version(), min_snapshots=0
        )
        level = "ok" if report.passed else "fail"
        lines.append(("ok", "active database", active.name))
        lines.append((level, "database validation", "PASS" if report.passed else "FAIL"))

    lines.append(("ok", "transport (local)", config.mcp.local.transport))
    if config.mcp.remote.enabled:
        try:
            config.assert_remote_startup_safe()
            lines.append(("ok", "remote startup", "safe (HTTPS + OAuth configured)"))
        except ConfigError as exc:
            lines.append(("fail", "remote startup", str(exc)))
    else:
        lines.append(("ok", "remote", "disabled (local-only)"))
    lines.append(("ok", "auth configured", str(config.auth.is_valid_oidc)))

    _print_doctor(lines)
    return 0 if all(level != "fail" for level, _, _ in lines) else 1


def _print_doctor(lines: list[tuple[str, str, str]]) -> None:
    _out("doctor:")
    for level, name, detail in lines:
        _out(f"  [{level:<4}] {name}: {detail}")


# --- source list/enable/disable/purge (§T26; §V20; §V28) ----------------------


def _cmd_source_list(args: argparse.Namespace, ctx: CliContext) -> int:
    _, registry = _load(args)
    if args.json:
        # Public-safe projection only (no policy notes / private hosting, §V27).
        _out(json.dumps(registry.public_registry(), indent=2, sort_keys=True))
        return 0
    _out("sources:")
    for source_id in sorted(registry.entries):
        entry = registry.entries[source_id]
        flag = "enabled " if entry.enabled else "disabled"
        regions = ",".join(entry.regions) or "-"
        _out(
            f"  [{flag}] {source_id:<28} {regions:<7} "
            f"{entry.license_status or '-'}/{entry.permission_status or '-'}"
        )
    return 0


def _toggle_source(args: argparse.Namespace, *, enabled: bool) -> int:
    """Flip a source's registry kill switch and journal the policy event (§V20).

    ``enable``/``disable`` only touch the registry (the mutable kill switch) and
    the operational journal -- they never rebuild or mutate the active database,
    so current data stays served until the next explicit build (§V4/§V20).
    """
    config, registry = _load(args)
    source_id = args.source_id
    if registry.get(source_id) is None:
        _err(f"source {source_id!r} not in registry")
        return 1
    event_type = "enable" if enabled else "disable"
    changed = set_source_enabled(config.source_registry.machine_registry, source_id, enabled)
    if not changed:
        _out(f"source {source_id!r} already {event_type}d")
        return 0
    append_event(
        config.database.data_dir,
        source_id=source_id,
        event_type=event_type,
        reason=args.reason,
    )
    if enabled:
        _out(f"enabled {source_id!r}: sync resumes on next `sync`")
    else:
        _out(f"disabled {source_id!r}: new sync stopped; current data stays active (§V20)")
    return 0


def _cmd_source_enable(args: argparse.Namespace, ctx: CliContext) -> int:
    return _toggle_source(args, enabled=True)


def _cmd_source_disable(args: argparse.Namespace, ctx: CliContext) -> int:
    return _toggle_source(args, enabled=False)


def _cmd_source_purge(args: argparse.Namespace, ctx: CliContext) -> int:
    """Rebuild the active DB with a source's rows removed, promote iff valid (§V20).

    Fail-closed: the current build stays active until the rebuilt candidate passes
    validation and is promoted atomically; a failing rebuild leaves it untouched.
    """
    config, registry = _load(args)
    if not args.rebuild:
        _err("purge requires --rebuild in v0.1 (§I.cmd/§V20)")
        return 1
    source_id = args.source_id
    if registry.get(source_id) is None:
        _err(f"source {source_id!r} not in registry")
        return 1
    active = _active_database(config)
    if active is None:
        _err("no active database to purge from — nothing to rebuild")
        return 1

    data_dir = config.database.data_dir
    # Journal the purge before rebuilding so it materializes into this build.
    append_event(data_dir, source_id=source_id, event_type="purge", reason=args.reason)
    _out(
        f"purge: rebuilding without {source_id!r}; "
        f"current database stays active until validated (§V20)"
    )
    result = purge_and_rebuild(
        active,
        source_id,
        data_dir=data_dir,
        servers=("en", "cn"),
        retain_versions=config.sync.retain_versions,
        current_manifest_path=config.database.current_manifest,
        policy_events=read_events(data_dir),
        expected_schema_version=_expected_schema_version(),
    )
    affected = result.affected
    _out(
        f"  removed {affected['snapshots']} snapshot(s), "
        f"{affected['enemies']} enemies, {affected['stages']} stages"
    )
    if not result.validation_passed:
        _err("rebuilt candidate failed validation; current database left active (§V20)")
        print(format_report(result.report), file=sys.stderr)
        return 1
    promotion = result.promotion
    assert promotion is not None  # validation passed -> promotion attempted
    if promotion.status == "noop":
        _out(f"unchanged: active build stays {promotion.manifest.database_filename} (no-op)")
    else:
        _out(f"promoted: {promotion.manifest.database_filename}")
        if promotion.pruned:
            _out(f"  pruned {len(promotion.pruned)} old build(s)")
    return 0


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

    p_validate = sub.add_parser("validate", help="run the validation gate against a database")
    p_validate.add_argument("--database", required=True, help="path to the database to validate")
    p_validate.add_argument(
        "--allow-empty",
        action="store_true",
        help="do not require at least one imported snapshot (schema-only check)",
    )
    p_validate.set_defaults(func=_cmd_validate)

    p_status = sub.add_parser("status", help="show the active snapshot + schema version")
    p_status.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p_status.set_defaults(func=_cmd_status)

    p_doctor = sub.add_parser("doctor", help="report environment/config/database health")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_source = sub.add_parser("source", help="manage data sources (list/enable/disable/purge)")
    source_sub = p_source.add_subparsers(dest="source_command", metavar="<action>")

    p_src_list = source_sub.add_parser("list", help="list registered sources + enabled status")
    p_src_list.add_argument("--json", action="store_true", help="emit public-safe JSON")
    p_src_list.set_defaults(func=_cmd_source_list)

    p_src_enable = source_sub.add_parser("enable", help="resume sync for a source (keeps data)")
    p_src_enable.add_argument("source_id")
    p_src_enable.add_argument("--reason", help="note recorded in the policy-event journal")
    p_src_enable.set_defaults(func=_cmd_source_enable)

    p_src_disable = source_sub.add_parser(
        "disable", help="stop new sync for a source; keep current data (§V20)"
    )
    p_src_disable.add_argument("source_id")
    p_src_disable.add_argument("--reason", help="note recorded in the policy-event journal")
    p_src_disable.set_defaults(func=_cmd_source_disable)

    p_src_purge = source_sub.add_parser(
        "purge", help="rebuild without a source's rows; current DB active until validated (§V20)"
    )
    p_src_purge.add_argument("source_id")
    p_src_purge.add_argument(
        "--rebuild", action="store_true", help="required: rebuild + validate before promoting"
    )
    p_src_purge.add_argument("--reason", help="note recorded in the policy-event journal")
    p_src_purge.set_defaults(func=_cmd_source_purge)

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
