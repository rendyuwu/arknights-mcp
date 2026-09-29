"""Snapshot manifest, checksums, and provenance construction.

Builds a deterministic manifest of a snapshot's files (path -> content hash),
derives a ``snapshot_id`` and ``manifest_hash``, and constructs the provenance
records every imported row must carry: ``snapshot_id`` + ``source_path`` /
``source_record_key`` + ``transform_version`` + ``record_hash``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from arknights_mcp.importers.field_policy import FIELD_POLICY_VERSION
from arknights_mcp.sources.base import SourceAdapter
from arknights_mcp.util.hashing import record_hash, sha256_hex

#: Transform/normalization version stamped on provenance.
#:
#: ``2``: route positions are rebased from the upstream bottom-origin
#: ``row`` into the tiles' top-origin grid frame at import, so an identical snapshot
#: now imports different bytes. The bump is what makes a rebuild promote over an
#: unchanged snapshot instead of no-opping as "content unchanged".
#:
#: ``3``: the untrusted-string sanitize now replaces a removed control char
#: with a space instead of deleting it in place, so every imported effect TEMPLATE and
#: announcement title that carried an upstream ``\n`` imports different bytes. Same
#: reason for the bump: without it the repaired sanitize never reaches an active build.
#:
#: ``4``: the effect template is now tag-stripped before it is
#: length-capped, and capped at ``MAX_TEMPLATE_LENGTH`` rather than the name-class 512,
#: so the 349 EN skill/talent templates (and 1 module template) the old order cut
#: mid-sentence import whole. Same reason for the bump.
#:
#: ``5``: a skill's ``name``/``skillType``/``durationType``/``spType`` are
#: scoped PER LEVEL upstream and were stored as level 1's value for the whole skill; each
#: level now keeps its own and the skill scalar holds only what every level shares. Four
#: EN/CN skills import different bytes (``sktok_mjcsdw`` no longer stores the unnamed
#: ``sp_type`` code its level 2 names). No allowlist change -- all four keys were already
#: allowlisted -- so ``FIELD_POLICY_VERSION`` stands and this bump alone is what makes the
#: re-import promote over an unchanged snapshot.
#: ``6``: the enemy bridge now emits ``attackRange`` (with upstream's
#: ``-1.0`` no-radius sentinel kept OUT of the distance column), ``targeting``, and
#: the nine typed ``<x>Immune`` flags folded into one ``immunities`` list -- three keys the
#: allowlist admitted while nothing mapped them, so the columns were 100% NULL on every
#: build ever promoted. ``damageType`` is imported beside them (a policy change too, hence
#: both versions move). Every enemy level row imports different bytes.
#:
#: ``7``: the sentinel strip now records that upstream ANSWERED before
#: it removes the value, so a level whose ``rangeRadius`` is the ``-1.0`` no-radius mask
#: imports ``attack_range_declared_none = 1`` instead of an ``attack_range`` NULL
#: indistinguishable from "never defined". The strip itself is unchanged (no negative is
#: stored as a distance, then or now); what changed is that the answer survives it.
#:
#: ``8``: the module change bundles now carry the part's own
#: ``isToken`` down onto each candidate, so whose effect a change describes is stored as
#: the source's statement instead of being inferred downstream from the ``talentIndex``
#: ``-1`` sentinel -- two facts that disagree on 454 of 513 en rows. Every module level
#: row with a change bundle imports different bytes, so the transform moves even where
#: the field policy would not.
#:
#: ``9``: operator records now carry their sub-allowlisted ``mainPower``/``subPower``
#: blocks in provenance, plus the collab flag and faction rows (ADR 0021), so every
#: operator row imports different bytes.
TRANSFORM_VERSION = "9"


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
    """Snapshot id ``<server>:<token>`` (section 16.1, e.g. ``en:<commit-sha>``).

    For local snapshots without a commit, the manifest hash prefix is the token.
    """
    return f"{server}:{version_token[:12]}"


@dataclass(frozen=True)
class SourceSnapshotRecord:
    """A row for ``source_snapshots`` (section 12.2)."""

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
    """A row for ``record_provenance`` (section 12.2). ``provenance_id`` is DB-assigned."""

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


def insert_record_provenance(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    source_path: str,
    source_record_key: str,
    record: Any,
    transform_version: str = TRANSFORM_VERSION,
    field_policy_version: str = FIELD_POLICY_VERSION,
) -> int:
    """Insert one ``record_provenance`` row and return its ``provenance_id``.

    The single home for the provenance INSERT: both the enemy and stage
    importers route through it, so the column set lives in exactly one place.
    """
    prov = make_record_provenance(
        snapshot_id=snapshot_id,
        source_path=source_path,
        source_record_key=source_record_key,
        record=record,
        transform_version=transform_version,
        field_policy_version=field_policy_version,
    )
    cur = conn.execute(
        "INSERT INTO record_provenance "
        "(snapshot_id, source_path, source_record_key, record_hash, "
        "transform_version, field_policy_version) VALUES (?, ?, ?, ?, ?, ?)",
        (
            prov.snapshot_id,
            prov.source_path,
            prov.source_record_key,
            prov.record_hash,
            prov.transform_version,
            prov.field_policy_version,
        ),
    )
    return int(cur.lastrowid or 0)
