"""Snapshot manifest, checksums, and provenance construction (SPEC §V17).

Builds a deterministic manifest of a snapshot's files (path -> content hash),
derives a ``snapshot_id`` and ``manifest_hash``, and constructs the provenance
records every imported row must carry: ``snapshot_id`` + ``source_path`` /
``source_record_key`` + ``transform_version`` + ``record_hash``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from arknights_mcp.importers.field_policy import FIELD_POLICY_VERSION
from arknights_mcp.sources.base import SourceAdapter
from arknights_mcp.util.hashing import record_hash, sha256_hex

#: Transform/normalization version stamped on provenance (§V17).
TRANSFORM_VERSION = "1"


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


@dataclass(frozen=True)
class SnapshotManifest:
    """Per-file content hashes plus an aggregate manifest hash."""

    files: dict[str, str]
    manifest_hash: str


def build_manifest(adapter: SourceAdapter, paths: Iterable[str] | None = None) -> SnapshotManifest:
    """Hash the given files (or every file the adapter lists) into a manifest.

    The ``manifest_hash`` is a stable SHA-256 over the sorted ``path:hash`` lines,
    so an unchanged snapshot yields an identical hash (enables no-op detection).
    """
    selected = sorted(paths) if paths is not None else sorted(adapter.iter_files())
    files: dict[str, str] = {}
    for rel in selected:
        files[rel] = sha256_hex(adapter.read_bytes(rel))
    digest_input = "\n".join(f"{rel}:{files[rel]}" for rel in sorted(files)).encode("utf-8")
    return SnapshotManifest(files=files, manifest_hash=sha256_hex(digest_input))


def make_snapshot_id(server: str, version_token: str) -> str:
    """Snapshot id ``<server>:<token>`` (§16.1, e.g. ``en:<commit-sha>``).

    For local snapshots without a commit, the manifest hash prefix is the token.
    """
    return f"{server}:{version_token[:12]}"


@dataclass(frozen=True)
class SourceSnapshotRecord:
    """A row for ``source_snapshots`` (§12.2)."""

    snapshot_id: str
    source_id: str
    server: str
    manifest_hash: str
    status: str = "imported"
    upstream_version: str | None = None
    commit_sha: str | None = None
    etag: str | None = None
    fetched_at: str | None = None
    imported_at: str = field(default_factory=_now_iso)
    license_status_at_import: str | None = None
    field_policy_version: str = FIELD_POLICY_VERSION


def make_snapshot_record(
    *,
    source_id: str,
    server: str,
    manifest: SnapshotManifest,
    commit_sha: str | None = None,
    license_status_at_import: str | None = None,
    imported_at: str | None = None,
) -> SourceSnapshotRecord:
    """Build a snapshot record; ``snapshot_id`` derives from commit or manifest."""
    token = commit_sha if commit_sha else manifest.manifest_hash
    return SourceSnapshotRecord(
        snapshot_id=make_snapshot_id(server, token),
        source_id=source_id,
        server=server,
        manifest_hash=manifest.manifest_hash,
        commit_sha=commit_sha,
        license_status_at_import=license_status_at_import,
        imported_at=imported_at if imported_at is not None else _now_iso(),
    )


@dataclass(frozen=True)
class RecordProvenance:
    """A row for ``record_provenance`` (§12.2). ``provenance_id`` is DB-assigned."""

    snapshot_id: str
    source_path: str
    source_record_key: str
    record_hash: str
    transform_version: str = TRANSFORM_VERSION
    field_policy_version: str = FIELD_POLICY_VERSION


def make_record_provenance(
    *,
    snapshot_id: str,
    source_path: str,
    source_record_key: str,
    record: Any,
    transform_version: str = TRANSFORM_VERSION,
    field_policy_version: str = FIELD_POLICY_VERSION,
) -> RecordProvenance:
    """Provenance for one imported record; hashes ``record`` for ``record_hash``."""
    return RecordProvenance(
        snapshot_id=snapshot_id,
        source_path=source_path,
        source_record_key=source_record_key,
        record_hash=record_hash(record),
        transform_version=transform_version,
        field_policy_version=field_policy_version,
    )
