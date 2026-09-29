"""Data-status service (PRD Section 13.9): the shared ``get_data_status`` domain
entry point both transports (and the ``status``/``doctor`` CLI) call.

Given a read-only connection to the active database, it reports the schema
version, active snapshots (source + region + commit/version + import time + age),
supported domains, the running analyzer version, the deployment mode, and
warnings with a suggested admin action -- never a query-time download.
Read-only + parameterized SQL only, through
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

#: The next step for an empty/unpromoted build is an admin CLI command the
#: MCP client cannot run itself (admin ops are CLI-only), so it is phrased as an
#: "ask the server admin" instruction rather than a bare command the client would try
#: to invoke. Client-facing text, so no internal cites -- the cites live in
#: this comment, never the emitted string.
_NO_SNAPSHOTS_ACTION = (
    "ask the server admin to run `arknights-mcp sync --server all` or `arknights-mcp import`"
)


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
        # The emitted key is ``import_status``, never ``status``. This row's
        # ``"imported"`` is a SNAPSHOT LIFECYCLE state, while the envelope's ``status``
        # is the tool's result status -- one name for two unrelated axes in one
        # response is the same namespace collision caught on ``schema_version``
        # a few keys away. The dataclass attribute keeps its short name; only the wire
        # (and the CLI ``--json`` that shares this projection) is disambiguated.
        return {
            "server": self.server,
            "source_id": self.source_id,
            "snapshot_id": self.snapshot_id,
            "commit_sha": self.commit_sha,
            "upstream_version": self.upstream_version,
            "imported_at": self.imported_at,
            "age_days": self.age_days,
            "import_status": self.status,
        }

    def to_provenance_extras(self, *, include_server: bool = False) -> dict[str, object]:
        """Snapshot fields for a ``data.snapshots`` row, keyed for the provenance join.

        ``imported_at`` travels ONLY with the envelope ``provenance`` entry
        (a row emitting it too would duplicate it per snapshot); ``snapshot_id`` is
        kept ON the row as the inline join key: one region can hold
        several active snapshots (game data + penguin + announcements all share
        ``server``), so ``server`` alone cannot pick a row's provenance entry and
        without ``snapshot_id`` the join would fall back to the "row N ↔ provenance N"
        order contract forbids. ``include_server`` additionally inlines the
        ``server`` region key for the multi-region ``get_data_status``
        tool; the region-scoped ``arknights://status/{server}`` resource carries the
        region once at the top level and leaves it off.
        ``commit_sha``/``upstream_version``/``age_days`` are omitted when unknown
        rather than emitted null (``age_days`` is None when ``imported_at``
        does not parse). ``to_dict`` keeps the full row for the CLI
        ``status``/``--json``, which has no envelope provenance.

        The lifecycle state is keyed ``import_status`` here as well, matching
        :meth:`to_dict` -- one vocabulary across the tool, the resource, and the CLI.
        """
        extras: dict[str, object] = {"source_id": self.source_id, "snapshot_id": self.snapshot_id}
        if include_server:
            extras = {"server": self.server, **extras}
        if self.commit_sha is not None:
            extras["commit_sha"] = self.commit_sha
        if self.upstream_version is not None:
            extras["upstream_version"] = self.upstream_version
        if self.age_days is not None:
            extras["age_days"] = self.age_days
        extras["import_status"] = self.status
        return extras


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
        """The full status body, for a caller with no envelope (the CLI ``--json``).

        The DB migration id is keyed ``db_schema_version``, never
        ``schema_version``. That name belongs to the response-contract version stamped on
        every MCP envelope, and this field is an unrelated axis -- the active build's
        migration (``"0018_enemy_range_declared_none"``). Both used to ship as
        ``schema_version`` in ONE ``get_data_status`` response, undocumented, so a client
        could not tell which of the two governed. The CLI has no envelope and so no
        collision, but it reads the same name for the same fact (one vocabulary).
        """
        return {
            "status": self.status,
            "db_schema_version": self.schema_version,
            "analyzer_version": self.analyzer_version,
            "mode": self.mode,
            "snapshots": [s.to_dict() for s in self.snapshots],
            "supported_domains": list(self.supported_domains),
            "disabled_analyzers": list(self.disabled_analyzers),
            "warnings": list(self.warnings),
            "suggested_action": self.suggested_action,
            "generated_at": self.generated_at,
        }

    def to_envelope_data(self) -> dict[str, object]:
        """The status body for an MCP ``data`` payload.

        :meth:`to_dict` minus the two fields the envelope already carries. A
        ``get_data_status`` response shipped ``status`` and ``analyzer_version`` at BOTH
        levels -- pure duplication, and the envelope is the sole
        carrier. Dropping them here rather than at each call site is what keeps the tool
        and the ``arknights://status/{server}`` resource from re-forking the projection:
        both build their payload from this one method and override only the
        region-scoped keys they genuinely differ on.
        """
        data = self.to_dict()
        del data["status"]
        del data["analyzer_version"]
        return data


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
    """Report the status of the active database (read-only).

    ``conn`` is a read-only connection to the promoted build. ``mode`` is the
    deployment mode (``"local"`` | ``"remote"``). ``now`` is injectable for
    deterministic age reporting; it defaults to the current UTC time.
    """
    clock = now if now is not None else datetime.now(tz=UTC)
    # Normalize an injected naive datetime to aware UTC so the age subtraction
    # never mixes naive/aware operands and raises TypeError (L11).
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)
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
        suggested_action = _NO_SNAPSHOTS_ACTION
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
