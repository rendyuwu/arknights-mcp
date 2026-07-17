"""Data-status service (§T27; PRD §13.9): the shared ``get_data_status`` domain
entry point both transports (and the ``status``/``doctor`` CLI) call.

Given a read-only connection to the active database, it reports the schema
version, active snapshots (source + region + commit/version + import time + age),
supported domains, the running analyzer version, the deployment mode, and
warnings with a suggested admin action -- never a query-time download (§V1/§V24).
Read-only + parameterized SQL only (§V2), through
:class:`~arknights_mcp.db.repositories.metadata.MetadataRepository`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from arknights_mcp.analyzers.base import ANALYZER_VERSION
from arknights_mcp.db.repositories.metadata import MetadataRepository, SnapshotRow

DataStatusCode = Literal["ok", "data_stale"]


@dataclass(frozen=True)
class SnapshotStatus:
    """One active snapshot's status line (metadata only; no game content)."""

    server: str
    source_id: str
    snapshot_id: str
    commit_sha: str | None
    upstream_version: str | None
    imported_at: str
    age_days: int | None
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "server": self.server,
            "source_id": self.source_id,
            "snapshot_id": self.snapshot_id,
            "commit_sha": self.commit_sha,
            "upstream_version": self.upstream_version,
            "imported_at": self.imported_at,
            "age_days": self.age_days,
            "status": self.status,
        }


@dataclass(frozen=True)
class DataStatus:
    """Domain result of :func:`get_data_status` (serializable, no prose)."""

    status: DataStatusCode
    schema_version: str | None
    analyzer_version: str
    mode: str
    snapshots: tuple[SnapshotStatus, ...]
    supported_domains: tuple[str, ...]
    disabled_analyzers: tuple[str, ...]
    warnings: tuple[str, ...]
    suggested_action: str | None
    generated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "schema_version": self.schema_version,
            "analyzer_version": self.analyzer_version,
            "mode": self.mode,
            "snapshots": [s.to_dict() for s in self.snapshots],
            "supported_domains": list(self.supported_domains),
            "disabled_analyzers": list(self.disabled_analyzers),
            "warnings": list(self.warnings),
            "suggested_action": self.suggested_action,
            "generated_at": self.generated_at,
        }


def _age_days(imported_at: str, now: datetime) -> int | None:
    try:
        parsed = datetime.fromisoformat(imported_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = now - parsed
    return max(0, delta.days)


def _to_status(row: SnapshotRow, now: datetime) -> SnapshotStatus:
    return SnapshotStatus(
        server=row.server,
        source_id=row.source_id,
        snapshot_id=row.snapshot_id,
        commit_sha=row.commit_sha,
        upstream_version=row.upstream_version,
        imported_at=row.imported_at,
        age_days=_age_days(row.imported_at, now),
        status=row.status,
    )


def get_data_status(
    conn: sqlite3.Connection,
    *,
    mode: str = "local",
    now: datetime | None = None,
) -> DataStatus:
    """Report the status of the active database (read-only; §V2/§V14).

    ``conn`` is a read-only connection to the promoted build. ``mode`` is the
    deployment mode (``"local"`` | ``"remote"``). ``now`` is injectable for
    deterministic age reporting; it defaults to the current UTC time.
    """
    clock = now if now is not None else datetime.now(tz=UTC)
    repo = MetadataRepository(conn)

    snapshots = tuple(_to_status(row, clock) for row in repo.all_snapshots())
    counts = repo.domain_row_counts()
    supported = tuple(name for name, count in counts.items() if count > 0)

    warnings: list[str] = []
    suggested_action: str | None = None
    status: DataStatusCode = "ok"
    if not snapshots:
        status = "data_stale"
        warnings.append("no imported snapshots in the active database")
        suggested_action = "run `arknights-mcp sync --server all` or `arknights-mcp import`"
    elif not supported:
        warnings.append("no domain rows present; the active build imported no entities")

    return DataStatus(
        status=status,
        schema_version=repo.schema_version(),
        analyzer_version=ANALYZER_VERSION,
        mode=mode,
        snapshots=snapshots,
        supported_domains=supported,
        disabled_analyzers=(),
        warnings=tuple(warnings),
        suggested_action=suggested_action,
        generated_at=clock.astimezone(UTC).isoformat(),
    )
