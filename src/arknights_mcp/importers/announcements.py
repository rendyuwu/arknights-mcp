"""Official-announcement importer: feed -> announcements (metadata-ONLY, §T95).

Consumes what the CLI-only :class:`~arknights_mcp.sources.announcements.AnnouncementsAdapter`
returns (never a query-time fetch, §V1) and writes the announcement-metadata cache:

* the field allowlist + recursive sanitize on every kept feed entry (§V18/§V56),
  routed through :mod:`arknights_mcp.importers.field_policy` -- only ``announceId``/
  ``title``/``date``/``url``/``category`` survive, so the article body / html / prose /
  image is never stored (§V16);
* an announcement ``source_snapshots`` row + per-record provenance so an announcement
  carries its OWN provenance chain, distinct from the game-data / drop facts (§V17);
* the region on every row (§V5), en and cn never mixed (§V56).

Pure parsing (:func:`parse_announcements`) is separated from the DB write so it is
unit-testable without a database. A feed entry missing an ``announceId`` is skipped
(fail-closed, no fabricated row). A non-empty feed that resolves to zero stored rows
fails closed (§V30); an empty feed is a legitimate empty build (``announcements`` is
not a CRITICAL_TABLE -- the adapter is disabled by default, D14/§V56).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.field_policy import (
    ANNOUNCEMENT_ALLOWLIST,
    FIELD_POLICY_VERSION,
    apply_allowlist,
)
from arknights_mcp.importers.manifest import insert_record_provenance, make_snapshot_id
from arknights_mcp.sources.announcements import source_id_for_region
from arknights_mcp.util.coerce import as_str
from arknights_mcp.util.hashing import canonical_json, sha256_hex
from arknights_mcp.util.sqlite import integrity_guard

_LOG = logging.getLogger(__name__)

#: Allowed fact regions for an announcement (§V56/§V5): en/cn only, never mixed.
_ALLOWED_REGIONS: frozenset[str] = frozenset({"en", "cn"})


class AnnouncementFetcher(Protocol):
    """The read surface the importer needs from the announcement adapter (§V37).

    Matches :meth:`AnnouncementsAdapter.fetch`; typed as a Protocol so the importer
    is unit-testable with an in-memory fake and never depends on the network class.
    """

    def fetch(self) -> Any: ...


@dataclass(frozen=True)
class ParsedAnnouncement:
    announce_id: str
    title: str | None
    date: str | None
    url: str | None
    category: str | None
    provenance_record: dict[str, Any]


@dataclass(frozen=True)
class AnnouncementImportResult:
    """Per-region outcome. ``announcements_skipped`` counts feed entries missing an
    ``announceId`` (skipped fail-closed, no fabricated row)."""

    region: str
    snapshot_id: str
    announcements_inserted: int
    announcements_skipped: int


def _feed_entries(feed_raw: Any) -> list[Any]:
    """Extract the list of entries from a feed payload (§V56 tolerant shape).

    Accepts either a top-level JSON array or an object wrapping the list under a
    common key (``announceList``/``announcements``/``list``/``data``). Anything else
    is a shape error -- fail closed rather than silently import nothing.
    """
    if isinstance(feed_raw, list):
        return feed_raw
    if isinstance(feed_raw, dict):
        for key in ("announceList", "announcements", "list", "data"):
            value = feed_raw.get(key)
            if isinstance(value, list):
                return value
    raise ImporterError(
        "announcement feed must be a JSON array or an object wrapping one "
        "(announceList/announcements/list/data)"
    )


def parse_announcements(feed_raw: Any) -> list[ParsedAnnouncement]:
    """Transform an announcement feed into allowlisted metadata rows (§V18/§V56).

    Only the §V56 metadata keys survive the allowlist; the article body / html /
    prose / image is dropped. An entry missing an ``announceId`` is skipped so no
    row is fabricated without its stable id (fail-closed).
    """
    out: list[ParsedAnnouncement] = []
    for entry in _feed_entries(feed_raw):
        if not isinstance(entry, dict):
            continue
        kept = apply_allowlist(entry, ANNOUNCEMENT_ALLOWLIST).kept
        announce_id = as_str(kept.get("announceId"))
        if announce_id is None:
            continue
        out.append(
            ParsedAnnouncement(
                announce_id=announce_id,
                title=as_str(kept.get("title")),
                date=as_str(kept.get("date")),
                url=as_str(kept.get("url")),
                category=as_str(kept.get("category")),
                provenance_record=kept,
            )
        )
    return out


def _insert_snapshot(
    conn: sqlite3.Connection,
    *,
    region: str,
    source_id: str,
    feed_raw: Any,
    fetched_at: str,
) -> str:
    """Insert the announcement ``source_snapshots`` row (its own provenance chain, §V17).

    The ``manifest_hash`` is derived from the fetched feed so an unchanged fetch
    yields a stable ``snapshot_id`` (like the game-data / drop snapshots, §V37).
    """
    manifest_hash = sha256_hex(canonical_json(feed_raw))
    snapshot_id = make_snapshot_id(region, manifest_hash)
    imported_at = datetime.now(tz=UTC).isoformat()
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, fetched_at, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot_id,
            source_id,
            region,
            fetched_at,
            imported_at,
            manifest_hash,
            "imported",
            FIELD_POLICY_VERSION,
        ),
    )
    return snapshot_id


def import_announcements(
    conn: sqlite3.Connection,
    adapter: AnnouncementFetcher,
    *,
    region: str,
    fetched_at: datetime | None = None,
    source_id: str | None = None,
) -> AnnouncementImportResult:
    """Fetch + import one region's announcement metadata into ``announcements``.

    ``region`` must be en or cn (§V56/§V5); every row is stamped with that region and
    a provenance row pointing at the announcement snapshot (§V17). Only the §V56
    metadata allowlist is stored (§V16). A non-empty feed that resolves to zero rows
    fails closed (§V30); an empty feed imports zero rows without error (the domain is
    legitimately empty -- the adapter is disabled by default, D14/§V56).
    """
    if region not in _ALLOWED_REGIONS:
        raise ImporterError(f"announcement region must be en|cn, got {region!r} (§V56)")
    resolved_source_id = source_id if source_id is not None else source_id_for_region(region)
    if resolved_source_id is None:  # pragma: no cover - guarded by _ALLOWED_REGIONS above
        raise ImporterError(f"no announcement source for region {region!r} (§V56)")

    feed_raw = adapter.fetch()
    parsed = parse_announcements(feed_raw)
    skipped = _skipped_count(feed_raw, parsed)

    # §V30: a feed that carried candidate entries but produced zero rows is a
    # silent-empty regression -- a shape mismatch (e.g. the id field is not
    # ``announceId``) leaves every entry skipped. Fail closed BEFORE writing a snapshot
    # so the candidate is discarded and the active DB stays untouched (§V3). A
    # genuinely empty feed (no dict entries) imports zero rows without error --
    # ``announcements`` is not a CRITICAL_TABLE (disabled by default, D14/§V56).
    if skipped and not parsed:
        raise ImporterError(
            f"{region}: announcement feed had {skipped} entr(y|ies) but none carried an "
            "announceId; refusing a silent empty announcement build (§V30)"
        )
    if skipped:
        _LOG.warning(
            "announcements %s: %d feed entr(y|ies) skipped (missing announceId)", region, skipped
        )

    fetched_iso = (fetched_at if fetched_at is not None else datetime.now(tz=UTC)).isoformat()
    snapshot_id = _insert_snapshot(
        conn,
        region=region,
        source_id=resolved_source_id,
        feed_raw=feed_raw,
        fetched_at=fetched_iso,
    )

    inserted = 0
    for ann in parsed:
        provenance_id = insert_record_provenance(
            conn,
            snapshot_id=snapshot_id,
            source_path=f"announcements/{region}",
            source_record_key=ann.announce_id,
            record=ann.provenance_record,
        )
        # A repeated announceId collides on UNIQUE(region, announce_id); map the
        # anomaly to a typed error (§V33), not an uncaught IntegrityError tearing
        # down the whole multi-region build (§V3).
        with integrity_guard(
            f"announcement {ann.announce_id!r} duplicates (region={region}, announce_id)",
            ImporterError,
        ):
            conn.execute(
                "INSERT INTO announcements (region, announce_id, title, date, url, category, "
                "provenance_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    region,
                    ann.announce_id,
                    ann.title,
                    ann.date,
                    ann.url,
                    ann.category,
                    provenance_id,
                ),
            )
        inserted += 1

    # §V30: a non-empty feed that stored zero rows is a silent-empty regression (every
    # entry missing an announceId, or a shape the allowlist stripped to nothing). Fail
    # closed so the candidate is discarded and the active DB stays untouched (§V3). An
    # empty feed (parsed == []) is a legitimate empty build -- announcements is not a
    # CRITICAL_TABLE (the adapter is disabled by default, D14/§V56).
    if parsed and inserted == 0:  # pragma: no cover - parsed rows always insert or raise
        raise ImporterError(
            f"{region}: announcement feed had {len(parsed)} entr(y|ies) but none stored; "
            "refusing a silent empty announcement build (§V30)"
        )

    skipped = _skipped_count(feed_raw, parsed)
    if skipped:
        _LOG.warning(
            "announcements %s: %d feed entr(y|ies) skipped (missing announceId)", region, skipped
        )
    return AnnouncementImportResult(
        region=region,
        snapshot_id=snapshot_id,
        announcements_inserted=inserted,
        announcements_skipped=skipped,
    )


def _skipped_count(feed_raw: Any, parsed: list[ParsedAnnouncement]) -> int:
    """Feed dict-entries that yielded no parsed row (missing announceId)."""
    total_dicts = sum(1 for entry in _feed_entries(feed_raw) if isinstance(entry, dict))
    return total_dicts - len(parsed)
