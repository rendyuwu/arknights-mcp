"""Shared candidate-build pipeline (PRD Section 11.2).

One code path builds a SQLite *candidate* from source adapters, used by both
``sync`` (network-staged snapshot) and ``import`` (local snapshot) so the two
never diverge. The network concern is isolated upstream in
:mod:`arknights_mcp.sources.arknights_assets`: by the time the pipeline runs it
only ever sees a local, read-only :class:`SourceAdapter` rooted at a snapshot
directory (staged download or user-supplied), so this module performs no network
I/O.

Steps per build (PRD Section 11.2):

* open a fresh writable candidate + run migrations (never touch the active DB);
* seed ``data_sources`` from the source registry (the authoritative posture);
* materialize the source-policy-event journal into ``source_policy_events``;
* per server: hash the snapshot into a manifest + provenance snapshot row,
  then import enemies + stages/levels through the field allowlist.

The candidate is *not* promoted here: the caller validates it and only then
promotes it atomically. A malformed snapshot raises, the candidate is
discarded, and the active database stays untouched (fail-closed).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from arknights_mcp.db.migrations import build_database
from arknights_mcp.db.policy_events import PolicyEvent, materialize_policy_events
from arknights_mcp.importers.banners import BannerImportResult, import_banners
from arknights_mcp.importers.base_skills import BaseSkillImportResult, import_base_skills
from arknights_mcp.importers.enemies import import_enemies
from arknights_mcp.importers.guards import (
    guard_not_silently_empty,
    import_optional_domain,
    refuse_silent_empty,
)
from arknights_mcp.importers.manifest import build_manifest, make_snapshot_record
from arknights_mcp.importers.modules import import_modules
from arknights_mcp.importers.operators import import_operators
from arknights_mcp.importers.ranges import RangeImportResult, import_ranges
from arknights_mcp.importers.search_index import build_search_index
from arknights_mcp.importers.skins import SkinImportResult, import_skins
from arknights_mcp.importers.stages import StageImportResult, import_stages
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
    """Per-server outcome recorded for CLI reporting (no game content).

    Carries the per-stage import counts: tiles/spawns/stage_enemies plus how
    many referenced level files actually imported, so a silent empty combat build
    is both reported and refused.
    """

    snapshot_id: str
    source_id: str
    server: str
    manifest_hash: str
    enemies: int
    enemy_levels: int
    zones: int
    stages: int
    levels_imported: int = 0
    tiles: int = 0
    spawns: int = 0
    stage_enemies: int = 0
    operators: int = 0
    skills: int = 0
    modules: int = 0
    banners: int = 0
    skins: int = 0
    ranges: int = 0


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
    _guard_not_silently_empty(job.server, stages)
    # Operators are optional per snapshot: a combat-only snapshot without
    # character_table imports zero and is not a silent-empty failure (the guard is
    # combat-scoped).
    operators = import_operators(conn, job.adapter, record.snapshot_id)
    # Modules link to operators via operator_pk, so they import after operators;
    # a snapshot without uniequip_table imports zero (optional per snapshot; the
    # guard is combat-scoped).
    modules = import_modules(conn, job.adapter, record.snapshot_id)
    # Banners and skins are the OPTIONAL fail-open domains of the main build:
    # both soft-resolve char ids to an operator_pk so both import after operators,
    # INSIDE this build (not as a post-promotion ride-along), and a snapshot carrying
    # neither table imports zero of each (optional per snapshot). An optional
    # domain must not fail-close the MANDATORY combat core, so each runs
    # under import_optional_domain (the single home): its ImporterError (a
    # non-empty-source-zero-rows guard, or a dup gachaPoolId / skinId) rolls back
    # only THIS region's partial domain + provenance rows and the build continues
    # game-data-only -- neither table is CRITICAL, so an empty archive/gallery is
    # legitimate and combat fail-closed is unchanged.
    banners = import_optional_domain(
        conn,
        lambda: import_banners(conn, job.adapter, record.snapshot_id),
        domain="banners",
        server=job.server,
        describe="banner archive",
        empty=BannerImportResult,
    )
    skins = import_optional_domain(
        conn,
        lambda: import_skins(conn, job.adapter, record.snapshot_id),
        domain="skins",
        server=job.server,
        describe="skin gallery",
        empty=SkinImportResult,
    )
    # Base (RIIC) skills (ADR 0021): same optional fail-open class; they link to an
    # operator_pk, so they import after operators. The counts feed no summary field.
    import_optional_domain(
        conn,
        lambda: import_base_skills(conn, job.adapter, record.snapshot_id),
        domain="base_skills",
        server=job.server,
        describe="base skills",
        empty=BaseSkillImportResult,
    )
    # The attack-range grids that resolve the `range_id` phases and
    # skill levels emit. Same optional fail-open class as banners/skins -- `range_table`
    # is fetched tolerant-absent and `ranges` is not CRITICAL, so a snapshot
    # without it imports zero and the wire surfaces its limitation. It references no
    # operator row, so its position here is free; it runs last with the other optional
    # domains so one home (import_optional_domain) governs them all.
    ranges = import_optional_domain(
        conn,
        lambda: import_ranges(conn, job.adapter, record.snapshot_id),
        domain="ranges",
        server=job.server,
        describe="attack-range grids",
        empty=RangeImportResult,
    )
    lv = stages.levels
    return SnapshotSummary(
        snapshot_id=record.snapshot_id,
        source_id=job.source_id,
        server=job.server,
        manifest_hash=record.manifest_hash,
        enemies=enemies.enemies_inserted,
        enemy_levels=enemies.levels_inserted,
        zones=stages.zones_inserted,
        stages=stages.stages_inserted,
        levels_imported=stages.levels_imported,
        tiles=lv.tiles,
        spawns=lv.spawns,
        stage_enemies=lv.stage_enemies,
        operators=operators.operators_inserted,
        skills=operators.skills_inserted,
        modules=modules.modules_inserted,
        banners=banners.banners_inserted,
        skins=skins.skins_inserted,
        ranges=ranges.ranges_inserted,
    )


def _guard_not_silently_empty(server: str, stages: StageImportResult) -> None:
    """Fail closed if a non-empty combat source yielded no combat rows.

    Two silent-empty regressions: every stage names a level file but
    none resolves (a schema/path mismatch), or level files import yet produce zero
    tiles/spawns/stage_enemies (a shape mismatch). Either raises so the candidate is
    discarded and the active database stays untouched — never a promoted
    build with empty combat data.
    """
    lv = stages.levels
    guard_not_silently_empty(
        candidates=stages.levels_referenced,
        produced=stages.levels_imported,
        scope=server,
        source="stage_table",
        unit="stage(s) referencing a level file",
        resolution="resolved to an imported level",
        outcome="empty combat build",
    )
    # The second case does not fit the shared candidates-vs-produced predicate (three
    # downstream counts, any one of them zero), so it states its own reason and raises
    # through the same refusal home rather than re-forking the message.
    if stages.levels_imported and (lv.tiles == 0 or lv.spawns == 0 or lv.stage_enemies == 0):
        refuse_silent_empty(
            f"{server}: imported {stages.levels_imported} level file(s) but produced "
            f"tiles={lv.tiles} spawns={lv.spawns} stage_enemies={lv.stage_enemies}",
            outcome="empty combat build",
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
    discarded by the caller and the active database is untouched. Foreign
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
        # Populate the unified FTS search index once every server is imported, so
        # it covers all regions in one pass; read-only from here on.
        build_search_index(conn)
        conn.commit()
    finally:
        conn.close()
    return BuildResult(candidate_path=path, snapshots=tuple(summaries))
