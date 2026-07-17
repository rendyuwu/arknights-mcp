"""Immutable versioned builds + atomic ``current.json`` promotion (§T24; §V3, §V4).

A ``sync``/``import`` produces a validated SQLite *candidate*. Promotion copies it
into an immutable, versioned file ``data/builds/<ts>-<servers>.sqlite`` and swaps
``data/current.json`` **atomically** to point at it (PRD §11.5). The active
database is never mutated in place; superseded builds are only ever removed by
retention pruning, never edited (§V4).

Fail-closed (§V3): a candidate that has not passed validation, is not a readable
SQLite file, or carries no applied schema migrations is refused and
``current.json`` is left untouched, so the current database stays active.

"Unchanged → no-op" compares *logical content identity* (schema version,
analyzer version, and the imported snapshot manifest hashes), not the raw SQLite
bytes -- two candidates built from the same snapshots at different times are byte
-different yet logically identical, and must not churn a fresh promotion.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from arknights_mcp.analyzers.base import ANALYZER_VERSION
from arknights_mcp.db.connection import DatabaseUnavailable, read_only_connection
from arknights_mcp.util.atomic import atomic_write_bytes, atomic_write_text
from arknights_mcp.util.hashing import record_hash, sha256_file

#: Immutable build file extension and the build subdirectory of the data dir.
BUILD_SUFFIX = ".sqlite"
BUILDS_DIRNAME = "builds"
CURRENT_MANIFEST_NAME = "current.json"

#: Columns of ``source_snapshots`` captured in the manifest's ``snapshots`` list.
_SNAPSHOT_COLUMNS = ("snapshot_id", "source_id", "server", "manifest_hash", "imported_at", "status")


class PromotionError(RuntimeError):
    """A candidate could not be promoted; the current DB is left active (§V3/§V4)."""


@dataclass(frozen=True)
class CurrentManifest:
    """The ``current.json`` payload selecting the active immutable build (§11.5)."""

    database_filename: str
    database_hash: str
    content_hash: str
    schema_version: str
    analyzer_version: str
    snapshots: list[dict[str, str]]
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "database_filename": self.database_filename,
            "database_hash": self.database_hash,
            "content_hash": self.content_hash,
            "schema_version": self.schema_version,
            "analyzer_version": self.analyzer_version,
            "snapshots": self.snapshots,
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> CurrentManifest:
        data = json.loads(text)
        snapshots = [{str(k): str(v) for k, v in row.items()} for row in data.get("snapshots", [])]
        return cls(
            database_filename=str(data["database_filename"]),
            database_hash=str(data["database_hash"]),
            content_hash=str(data["content_hash"]),
            schema_version=str(data["schema_version"]),
            analyzer_version=str(data["analyzer_version"]),
            snapshots=snapshots,
            created_at=str(data["created_at"]),
        )


@dataclass(frozen=True)
class PromotionResult:
    """Outcome of :func:`promote_candidate`."""

    status: str  # "promoted" | "noop"
    manifest: CurrentManifest
    database_path: Path
    pruned: list[str]  # basenames removed by retention


def read_schema_version(conn: sqlite3.Connection) -> str:
    """Latest applied migration version (the DB schema version); raise if none (§V3)."""
    try:
        rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    except sqlite3.Error as exc:
        raise PromotionError("candidate has no schema_migrations table") from exc
    if not rows:
        raise PromotionError("candidate has no applied schema migrations")
    return str(rows[-1][0])


def _read_snapshots(conn: sqlite3.Connection) -> list[dict[str, str]]:
    try:
        rows = conn.execute(
            "SELECT snapshot_id, source_id, server, manifest_hash, imported_at, status "
            "FROM source_snapshots ORDER BY snapshot_id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise PromotionError("candidate has no source_snapshots table") from exc
    return [
        {
            col: ("" if val is None else str(val))
            for col, val in zip(_SNAPSHOT_COLUMNS, row, strict=True)
        }
        for row in rows
    ]


def _content_hash(
    schema_version: str, analyzer_version: str, snapshots: list[dict[str, str]]
) -> str:
    """Logical content identity for no-op detection (excludes raw SQLite bytes)."""
    snapshot_keys = sorted(
        f"{s['source_id']}|{s['server']}|{s['manifest_hash']}" for s in snapshots
    )
    return record_hash(
        {
            "schema_version": schema_version,
            "analyzer_version": analyzer_version,
            "snapshots": snapshot_keys,
        }
    )


def _manifest_path(data_dir: Path, current_manifest_path: str | Path | None) -> Path:
    if current_manifest_path is not None:
        return Path(current_manifest_path)
    return data_dir / CURRENT_MANIFEST_NAME


def read_current_manifest(path: str | Path) -> CurrentManifest | None:
    """Parse ``current.json``; ``None`` if absent or unreadable (treated as unpromoted)."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return CurrentManifest.from_json(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def resolve_active_database(
    data_dir: str | Path,
    current_manifest_path: str | Path | None = None,
) -> Path | None:
    """Resolve the promoted, immutable build path from ``current.json`` (for readers).

    Fulfils the resolution deferred by ``db/connection.py``. Returns ``None`` when
    nothing is promoted or the referenced build file is missing.
    """
    root = Path(data_dir)
    manifest = read_current_manifest(_manifest_path(root, current_manifest_path))
    if manifest is None:
        return None
    build = root / BUILDS_DIRNAME / manifest.database_filename
    return build if build.is_file() else None


def _versioned_filename(builds_dir: Path, timestamp: datetime, servers: Sequence[str]) -> str:
    """``<ts>-<servers>.sqlite`` (PRD §11.5), disambiguated on same-second collision."""
    ts_utc = timestamp.astimezone(UTC)
    token = ts_utc.strftime("%Y-%m-%dT%H%M%SZ")
    base = f"{token}-{'-'.join(servers)}"
    name = f"{base}{BUILD_SUFFIX}"
    counter = 2
    while (builds_dir / name).exists():
        name = f"{base}-{counter}{BUILD_SUFFIX}"
        counter += 1
    return name


def _prune_old_builds(builds_dir: Path, keep_name: str, retain_versions: int) -> list[str]:
    """Keep the newest ``retain_versions`` builds (always ``keep_name``); prune the rest."""
    retain = max(1, retain_versions)
    all_builds = sorted(builds_dir.glob(f"*{BUILD_SUFFIX}"), key=lambda p: p.name, reverse=True)
    keep: set[str] = {keep_name}
    for build in all_builds:
        if len(keep) >= retain:
            break
        keep.add(build.name)
    pruned: list[str] = []
    for build in all_builds:
        if build.name not in keep:
            build.unlink()
            pruned.append(build.name)
    return pruned


def promote_candidate(
    candidate_path: str | Path,
    *,
    data_dir: str | Path,
    validation_passed: bool,
    servers: Sequence[str] = ("en", "cn"),
    retain_versions: int = 3,
    current_manifest_path: str | Path | None = None,
    timestamp: datetime | None = None,
) -> PromotionResult:
    """Promote a validated candidate to the active build via atomic ``current.json``.

    Fails closed (§V3): raises :class:`PromotionError` -- leaving ``current.json``
    untouched -- when ``validation_passed`` is false, the candidate is missing, or
    it carries no schema/snapshots. Promotion is atomic (§V4): the build is copied
    immutably into ``data/builds/`` and ``current.json`` is swapped with
    ``os.replace``; the active DB is never mutated in place. An unchanged candidate
    (same logical content) is a no-op.
    """
    if not validation_passed:
        raise PromotionError("refusing to promote: candidate did not pass validation (§V4)")

    candidate = Path(candidate_path)
    if not candidate.is_file():
        raise PromotionError(f"candidate not found: {candidate.name}")

    try:
        with read_only_connection(candidate) as conn:
            schema_version = read_schema_version(conn)
            snapshots = _read_snapshots(conn)
    except DatabaseUnavailable as exc:
        raise PromotionError(f"candidate is not a readable database: {candidate.name}") from exc

    content_hash = _content_hash(schema_version, ANALYZER_VERSION, snapshots)
    database_hash = sha256_file(candidate)

    root = Path(data_dir)
    builds_dir = root / BUILDS_DIRNAME
    manifest_path = _manifest_path(root, current_manifest_path)

    current = read_current_manifest(manifest_path)
    if current is not None and current.content_hash == content_hash:
        active = builds_dir / current.database_filename
        if active.is_file():
            # Logically identical to the active build -> no-op (§11.2).
            return PromotionResult(status="noop", manifest=current, database_path=active, pruned=[])

    ts = timestamp if timestamp is not None else datetime.now(tz=UTC)
    builds_dir.mkdir(parents=True, exist_ok=True)
    build_name = _versioned_filename(builds_dir, ts, servers)
    build_path = builds_dir / build_name

    # Copy the candidate into its immutable, versioned home via a temp file +
    # os.replace so a reader never sees a partial build.
    atomic_write_bytes(build_path, candidate.read_bytes())

    manifest = CurrentManifest(
        database_filename=build_name,
        database_hash=database_hash,
        content_hash=content_hash,
        schema_version=schema_version,
        analyzer_version=ANALYZER_VERSION,
        snapshots=snapshots,
        created_at=ts.astimezone(UTC).isoformat(),
    )
    # Atomic promotion: the manifest swap is the single point that makes the new
    # build active (§V4).
    atomic_write_text(manifest_path, manifest.to_json())

    pruned = _prune_old_builds(builds_dir, keep_name=build_name, retain_versions=retain_versions)
    return PromotionResult(
        status="promoted", manifest=manifest, database_path=build_path, pruned=pruned
    )
