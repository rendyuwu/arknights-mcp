"""Shared candidate-build pipeline (§T21/§T22; PRD §11.2).

One code path builds a SQLite *candidate* from source adapters, used by both
``sync`` (network-staged snapshot) and ``import`` (local snapshot) so the two
never diverge. The network concern is isolated upstream in
:mod:`arknights_mcp.sources.arknights_assets`: by the time the pipeline runs it
only ever sees a local, read-only :class:`SourceAdapter` rooted at a snapshot
directory (staged download or user-supplied), so this module performs no network
I/O (§V1).

Steps per build (PRD §11.2):

* open a fresh writable candidate + run migrations (never touch the active DB);
* seed ``data_sources`` from the source registry (the authoritative posture);
* materialize the source-policy-event journal into ``source_policy_events``;
* per server: hash the snapshot into a manifest + provenance snapshot row (§V17),
  then import enemies + stages/levels through the field allowlist (§V16/§V18).

The candidate is *not* promoted here: the caller validates it (§T23) and only then
promotes it atomically (§T24/§V4). A malformed snapshot raises, the candidate is
discarded, and the active database stays untouched (fail-closed, §V3).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from arknights_mcp.db.migrations import build_database
from arknights_mcp.db.policy_events import PolicyEvent, materialize_policy_events
from arknights_mcp.importers.enemies import import_enemies
from arknights_mcp.importers.manifest import build_manifest, make_snapshot_record
from arknights_mcp.importers.stages import import_stages
from arknights_mcp.sources.base import SourceAdapter
from arknights_mcp.sources.registry import SourceRegistry, SourceRegistryEntry


@dataclass(frozen=True)
class ServerImport:
    """One region's import: a local snapshot adapter tagged with its source."""

    server: str
    adapter: SourceAdapter
    source_id: str
    commit_sha: str | None = None


@dataclass(frozen=True)
class SnapshotSummary:
    """Per-server outcome recorded for CLI reporting (no game content)."""

    snapshot_id: str
    source_id: str
    server: str
    manifest_hash: str
    enemies: int
    enemy_levels: int
    zones: int
    stages: int


@dataclass(frozen=True)
class BuildResult:
    """Outcome of :func:`build_candidate` (the candidate is not yet promoted)."""

    candidate_path: Path
    snapshots: tuple[SnapshotSummary, ...]


def seed_data_sources(conn: sqlite3.Connection, registry: SourceRegistry) -> int:
    """Insert every registry entry into ``data_sources`` (returns rows written).

    Seeding the full registry -- not only the sources being imported -- keeps the
    public-safe posture complete for ``get_data_sources`` and lets
    ``source_policy_events`` reference any registered source by foreign key.
    """
    written = 0
    for source_id in sorted(registry.entries):
        _insert_data_source(conn, registry.entries[source_id])
        written += 1
    return written


def _insert_data_source(conn: sqlite3.Connection, entry: SourceRegistryEntry) -> None:
    conn.execute(
        "INSERT INTO data_sources ("
        "source_id, display_name, owner_name, canonical_url, source_type, regions_json, "
        "adapter_version, license_identifier, license_status, permission_status, "
        "private_hosting_status, redistribution_status, attribution_text, contact_url, "
        "policy_notes, enabled, last_reviewed_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entry.source_id,
            entry.display_name,
            entry.owner_name,
            entry.canonical_url,
            entry.source_type,
            json.dumps(entry.regions),
            entry.adapter_version,
            entry.license_identifier,
            entry.license_status,
            entry.permission_status,
            entry.private_hosting_status,
            entry.redistribution_status,
            entry.attribution_text,
            entry.contact_url,
            entry.policy_notes,
            int(entry.enabled),
            entry.last_reviewed_at,
        ),
    )


def _import_one(
    conn: sqlite3.Connection, job: ServerImport, *, imported_at: str | None
) -> SnapshotSummary:
    manifest = build_manifest(job.adapter)
    record = make_snapshot_record(
        source_id=job.source_id,
        server=job.server,
        manifest=manifest,
        commit_sha=job.commit_sha,
        imported_at=imported_at,
    )
    conn.execute(
        "INSERT INTO source_snapshots ("
        "snapshot_id, source_id, server, upstream_version, commit_sha, etag, fetched_at, "
        "imported_at, manifest_hash, status, license_status_at_import, field_policy_version"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record.snapshot_id,
            record.source_id,
            record.server,
            record.upstream_version,
            record.commit_sha,
            record.etag,
            record.fetched_at,
            record.imported_at,
            record.manifest_hash,
            record.status,
            record.license_status_at_import,
            record.field_policy_version,
        ),
    )
    enemies = import_enemies(conn, job.adapter, record.snapshot_id)
    stages = import_stages(conn, job.adapter, record.snapshot_id)
    return SnapshotSummary(
        snapshot_id=record.snapshot_id,
        source_id=job.source_id,
        server=job.server,
        manifest_hash=record.manifest_hash,
        enemies=enemies.enemies_inserted,
        enemy_levels=enemies.levels_inserted,
        zones=stages.zones_inserted,
        stages=stages.stages_inserted,
    )


def build_candidate(
    candidate_path: str | Path,
    imports: Sequence[ServerImport],
    *,
    registry: SourceRegistry,
    policy_events: Sequence[PolicyEvent] = (),
    migrations_dir: Path | None = None,
    imported_at: str | None = None,
) -> BuildResult:
    """Build (but do not promote) a candidate database from ``imports``.

    Opens a fresh writable candidate, seeds ``data_sources`` + policy events, and
    imports each server's snapshot. On any error the partially-built candidate is
    discarded by the caller and the active database is untouched (§V3). Foreign
    keys are enforced throughout (migrations turn them on).
    """
    if not imports:
        raise ValueError("build_candidate requires at least one server import")

    path = Path(candidate_path)
    conn = build_database(path, migrations_dir)
    try:
        seed_data_sources(conn, registry)
        materialize_policy_events(conn, policy_events)
        summaries = [_import_one(conn, job, imported_at=imported_at) for job in imports]
        conn.commit()
    finally:
        conn.close()
    return BuildResult(candidate_path=path, snapshots=tuple(summaries))
