"""``status`` + ``doctor`` commands: active snapshot + environment health (§T25).

``doctor`` never prints secrets or secret values (§V12): auth is reported only as
a configured/not-configured boolean, never issuer/audience/JWKS contents.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import sqlite3
import sys

from arknights_mcp.cli._shared import (
    CliContext,
    _active_database,
    _expected_schema_version,
    _load,
    _out,
)
from arknights_mcp.config import AppConfig, ConfigError
from arknights_mcp.db.connection import read_only_connection
from arknights_mcp.db.validate import validate_database
from arknights_mcp.services.status import get_data_status
from arknights_mcp.sources.registry import RegistryError


def _mode(config: AppConfig) -> str:
    return "remote" if config.mcp.remote.enabled else "local"


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
