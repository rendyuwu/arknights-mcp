"""Data-sources service (§T27; §V27; PRD §13.10): the shared ``get_data_sources``
domain entry point that returns the public-safe source registry.

For each registered source it returns the PRD §13.10 fields (id, display name,
owner, canonical URL, purpose + consumed fields, region coverage, license /
permission status, private-hosting + redistribution posture, attribution text,
enabled state + last-reviewed date) plus the active snapshot version/commit when
a promoted build carries one. It **never** returns secrets, local filesystem
paths, OAuth configuration, internal policy notes, or takedown correspondence
(§V27): the projection is built from an explicit allowlist of registry fields,
not by dumping the row.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from arknights_mcp.db.repositories.metadata import MetadataRepository, SnapshotRow
from arknights_mcp.sources.registry import SourceRegistry, SourceRegistryEntry


@dataclass(frozen=True)
class ActiveSnapshotInfo:
    """The latest snapshot for one region of a source (metadata only)."""

    server: str
    snapshot_id: str
    commit_sha: str | None
    upstream_version: str | None
    imported_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "server": self.server,
            "snapshot_id": self.snapshot_id,
            "commit_sha": self.commit_sha,
            "upstream_version": self.upstream_version,
            "imported_at": self.imported_at,
        }


@dataclass(frozen=True)
class SourceInfo:
    """Public-safe registry projection for one source (§V27; PRD §13.10)."""

    source_id: str
    display_name: str
    owner_name: str
    canonical_url: str
    source_type: str
    regions: tuple[str, ...]
    purpose: str
    fields_consumed: tuple[str, ...]
    license_identifier: str
    license_status: str
    permission_status: str
    private_hosting_status: str
    redistribution_status: str
    attribution_text: str
    contact_url: str
    enabled: bool
    last_reviewed_at: str
    active_snapshots: tuple[ActiveSnapshotInfo, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "display_name": self.display_name,
            "owner_name": self.owner_name,
            "canonical_url": self.canonical_url,
            "source_type": self.source_type,
            "regions": list(self.regions),
            "purpose": self.purpose,
            "fields_consumed": list(self.fields_consumed),
            "license_identifier": self.license_identifier,
            "license_status": self.license_status,
            "permission_status": self.permission_status,
            "private_hosting_status": self.private_hosting_status,
            "redistribution_status": self.redistribution_status,
            "attribution_text": self.attribution_text,
            "contact_url": self.contact_url,
            "enabled": self.enabled,
            "last_reviewed_at": self.last_reviewed_at,
            "active_snapshots": [s.to_dict() for s in self.active_snapshots],
        }


@dataclass(frozen=True)
class DataSourcesResult:
    """Domain result of :func:`get_data_sources` (serializable, public-safe)."""

    sources: tuple[SourceInfo, ...]

    def to_dict(self) -> dict[str, object]:
        return {"sources": [s.to_dict() for s in self.sources]}


def _latest_per_server(snapshots: list[SnapshotRow]) -> tuple[ActiveSnapshotInfo, ...]:
    """The newest snapshot per region for one source (by import time)."""
    by_server: dict[str, SnapshotRow] = {}
    for row in snapshots:
        current = by_server.get(row.server)
        if current is None or row.imported_at >= current.imported_at:
            by_server[row.server] = row
    return tuple(
        ActiveSnapshotInfo(
            server=row.server,
            snapshot_id=row.snapshot_id,
            commit_sha=row.commit_sha,
            upstream_version=row.upstream_version,
            imported_at=row.imported_at,
        )
        for row in (by_server[s] for s in sorted(by_server))
    )


def _source_info(entry: SourceRegistryEntry, active: tuple[ActiveSnapshotInfo, ...]) -> SourceInfo:
    return SourceInfo(
        source_id=entry.source_id,
        display_name=entry.display_name,
        owner_name=entry.owner_name,
        canonical_url=entry.canonical_url,
        source_type=entry.source_type,
        regions=tuple(entry.regions),
        purpose=entry.purpose,
        fields_consumed=tuple(entry.fields_consumed),
        license_identifier=entry.license_identifier,
        license_status=entry.license_status,
        permission_status=entry.permission_status,
        private_hosting_status=entry.private_hosting_status,
        redistribution_status=entry.redistribution_status,
        attribution_text=entry.attribution_text,
        contact_url=entry.contact_url,
        enabled=entry.enabled,
        last_reviewed_at=entry.last_reviewed_at,
        active_snapshots=active,
    )


def get_data_sources(
    registry: SourceRegistry,
    conn: sqlite3.Connection | None = None,
) -> DataSourcesResult:
    """Return the public-safe source registry (§V27; PRD §13.10).

    ``registry`` is the authoritative live posture (enabled/disabled reflects the
    machine registry). When ``conn`` is a read-only connection to the active build,
    each source is annotated with its latest snapshot per region. Excludes secrets,
    local paths, OAuth config, policy notes, and takedown correspondence (§V27).
    """
    snapshots_by_source: dict[str, list[SnapshotRow]] = {}
    if conn is not None:
        for row in MetadataRepository(conn).all_snapshots():
            snapshots_by_source.setdefault(row.source_id, []).append(row)

    sources = tuple(
        _source_info(
            registry.entries[source_id],
            _latest_per_server(snapshots_by_source.get(source_id, [])),
        )
        for source_id in sorted(registry.entries)
    )
    return DataSourcesResult(sources=sources)
