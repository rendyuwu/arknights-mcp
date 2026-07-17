"""Shared helpers for the ``arknights-mcp`` CLI command modules (§V38/§V37).

Cross-cutting glue used by more than one command group lives here in exactly one
home: config/registry loading, the candidate build->validate->promote pipeline
shared by ``sync`` and ``import``, active-database resolution, console output,
and the exception tuple that maps clean failures to exit 1. Command-group-local
helpers stay in their own module.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from arknights_mcp.config import AppConfig, ConfigError, load_config
from arknights_mcp.db.migrations import default_migrations_dir
from arknights_mcp.db.policy_events import read_events
from arknights_mcp.db.promotion import PromotionError, promote_candidate, resolve_active_database
from arknights_mcp.db.purge import PurgeError
from arknights_mcp.db.validate import format_report, validate_database
from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.sources.arknights_assets import Fetcher
from arknights_mcp.sources.base import SourceAdapterError
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


def _load(args: argparse.Namespace) -> tuple[AppConfig, SourceRegistry]:
    # Overlay the non-secret OIDC descriptors from the environment (§I env): they
    # are designed to come from env, so remote-safety / auth reporting in doctor
    # must see them, not TOML alone (L1).
    config = load_config(args.config, env=os.environ)
    registry = load_source_registry(config.source_registry.machine_registry)
    return config, registry


def _expected_schema_version() -> str | None:
    files = sorted(default_migrations_dir().glob("[0-9]*.sql"))
    return files[-1].stem if files else None


def _active_database(config: AppConfig) -> Path | None:
    return resolve_active_database(config.database.data_dir, config.database.current_manifest)


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
            # Per-stage combat import counts (§V30): make a silent empty build
            # visible even when it would still pass validation.
            _out(
                f"    levels {snap.levels_imported}, tiles {snap.tiles}, "
                f"spawns {snap.spawns}, stage_enemies {snap.stage_enemies}"
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
