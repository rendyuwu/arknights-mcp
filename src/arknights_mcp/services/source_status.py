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
    """Public-safe registry projection for one source (§V27; PRD §13.10).

    The public field set is defined solely by
    :meth:`SourceRegistryEntry.public_view` (§V34): this wrapper adds only the
    DB-derived ``active_snapshots`` enrichment and never re-enumerates the
    registry allowlist. Routing both this service and the CLI ``source list
    --json`` view through the single ``public_view`` projection is what keeps the
    two surfaces from diverging (B18).
    """

    entry: SourceRegistryEntry
    active_snapshots: tuple[ActiveSnapshotInfo, ...]

    @property
    def source_id(self) -> str:
        return self.entry.source_id

    @property
    def enabled(self) -> bool:
        return self.entry.enabled

    def to_dict(self) -> dict[str, object]:
        # public_view() is the sole allowlist; active_snapshots is DB-only (§V34).
        return {
            **self.entry.public_view(),
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
        SourceInfo(
            entry=registry.entries[source_id],
            active_snapshots=_latest_per_server(snapshots_by_source.get(source_id, [])),
        )
        for source_id in sorted(registry.entries)
    )
    return DataSourcesResult(sources=sources)
