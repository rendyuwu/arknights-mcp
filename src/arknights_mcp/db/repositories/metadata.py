"""Metadata read repository (§V2; backs §T27 status services).

Parameterized, read-only SELECTs over the core metadata tables
(``schema_migrations``, ``source_snapshots``, ``data_sources``) plus per-domain
row-count probes. This is the sole sanctioned SQL surface for the data-status and
data-sources services; no value is interpolated into a query string (§V2).
"""

from __future__ import annotations

from dataclasses import dataclass

from arknights_mcp.db.repositories.base import Repository

#: Domain tables probed for "supported domains" reporting (name -> table).
_DOMAIN_TABLES: tuple[tuple[str, str], ...] = (
    ("enemies", "enemies"),
    ("stages", "stages"),
    ("operators", "operators"),
    ("modules", "modules"),
)


@dataclass(frozen=True)
class SnapshotRow:
    """One ``source_snapshots`` row (metadata only; no game content)."""

    snapshot_id: str
    source_id: str
    server: str
    upstream_version: str | None
    commit_sha: str | None
    imported_at: str
    manifest_hash: str
    status: str


class MetadataRepository(Repository):
    """Read-only access to schema, snapshot, and source metadata (§V2)."""

    def schema_version(self) -> str | None:
        rows = self._all("SELECT version FROM schema_migrations ORDER BY version")
        return str(rows[-1][0]) if rows else None

    def all_snapshots(self) -> list[SnapshotRow]:
        rows = self._all(
            "SELECT snapshot_id, source_id, server, upstream_version, commit_sha, "
            "imported_at, manifest_hash, status "
            "FROM source_snapshots ORDER BY server, imported_at, snapshot_id"
        )
        return [
            SnapshotRow(
                snapshot_id=str(r[0]),
                source_id=str(r[1]),
                server=str(r[2]),
                upstream_version=None if r[3] is None else str(r[3]),
                commit_sha=None if r[4] is None else str(r[4]),
                imported_at=str(r[5]),
                manifest_hash=str(r[6]),
                status=str(r[7]),
            )
            for r in rows
        ]

    def snapshots_for_source(self, source_id: str) -> list[SnapshotRow]:
        """Snapshots attributable to one source (parameterized, §V2)."""
        return [s for s in self.all_snapshots() if s.source_id == source_id]

    def domain_row_counts(self) -> dict[str, int]:
        """Row counts per domain table; a table absent from the schema counts 0."""
        counts: dict[str, int] = {}
        present = {
            row[0] for row in self._all("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for name, table in _DOMAIN_TABLES:
            if table not in present:
                counts[name] = 0
                continue
            # Table name comes from the fixed _DOMAIN_TABLES allowlist, never input.
            row = self._one(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 - fixed table names
            counts[name] = int(row[0]) if row else 0
        return counts

    def source_enabled_flags(self) -> dict[str, bool]:
        rows = self._all("SELECT source_id, enabled FROM data_sources")
        return {str(r[0]): bool(r[1]) for r in rows}
