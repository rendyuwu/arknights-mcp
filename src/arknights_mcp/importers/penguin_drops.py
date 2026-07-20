"""Penguin Statistics drop importer: items + result/matrix -> items + stage_drops.

Consumes what the CLI-only :class:`~arknights_mcp.sources.penguin_statistics.PenguinStatsAdapter`
returns (never a query-time fetch, §V52/§V1) and writes the drop-rate cache:

* the penguin server -> fact-region map (``US``/Global -> ``en``, ``CN`` -> ``cn``;
  ``JP``/``KR`` are outside {en,cn} in v0.2 and are dropped, §V54);
* the field allowlist + recursive sanitize on every kept item / matrix row (§V18,
  routed through :mod:`arknights_mcp.importers.field_policy`);
* a penguin ``source_snapshots`` row + per-record provenance so a drop fact carries
  its OWN provenance chain, distinct from the ``arknights_assets`` game-data fact
  (§V17/§V54);
* the §V53 stale/attribution stamps (``fetched_at`` + ``expires_at`` + penguin
  ``snapshot_id`` + ``region``) on every ``stage_drops`` row.

Pure parsing (:func:`parse_items` / :func:`parse_matrix`) is separated from the DB
write so it is unit-testable without a database. A penguin ``stageId`` / ``itemId``
is the arknights game id, joined to the internal ``stages`` / ``items`` rows; a drop
whose stage or item is absent is skipped (fail-closed, no fabricated row). A
non-empty matrix that resolves to zero drops fails closed (§V30).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.field_policy import (
    FIELD_POLICY_VERSION,
    ITEM_ALLOWLIST,
    PENGUIN_MATRIX_ALLOWLIST,
    apply_allowlist,
)
from arknights_mcp.importers.manifest import insert_record_provenance, make_snapshot_id
from arknights_mcp.sources.penguin_statistics import DEFAULT_SOURCE_ID
from arknights_mcp.util.coerce import as_int, as_str
from arknights_mcp.util.hashing import canonical_json, sha256_hex
from arknights_mcp.util.sqlite import integrity_guard

_LOG = logging.getLogger(__name__)

#: Penguin server code -> fact region (§V54). ``US`` is the Global/EN server, ``CN``
#: the Chinese server. ``JP``/``KR`` penguin servers are outside {en,cn} in v0.2 and
#: are dropped -- never mislabelled as en/cn.
PENGUIN_SERVER_TO_REGION: dict[str, str] = {"US": "en", "CN": "cn"}

#: Default lifetime of a cached drop fact before it is served as ``data_stale``
#: (§V53). The CLI/config may override; kept here as the single default home.
DEFAULT_DROP_TTL: timedelta = timedelta(days=7)


class DropFetcher(Protocol):
    """The read surface the importer needs from the penguin adapter (§V37).

    Matches :meth:`PenguinStatsAdapter.fetch`; typed as a Protocol so the importer
    is unit-testable with an in-memory fake and never depends on the network class.
    """

    def fetch(self, endpoint: str, *, server: str | None = None) -> Any: ...


@dataclass(frozen=True)
class ParsedItem:
    game_id: str
    display_name: str | None
    rarity: str | None
    item_type: str | None
    provenance_record: dict[str, Any]


@dataclass(frozen=True)
class ParsedDrop:
    stage_game_id: str
    item_game_id: str
    quantity: int | None
    times: int | None
    drop_rate: float | None
    provenance_record: dict[str, Any]


@dataclass(frozen=True)
class PenguinDropImportResult:
    """Per-server outcome. ``region``/``snapshot_id`` are ``None`` for a dropped
    (jp/kr) penguin server (§V54); ``drops_skipped`` counts matrix rows whose stage
    or item was absent from the DB (skipped fail-closed, no fabricated row)."""

    region: str | None
    snapshot_id: str | None
    items_inserted: int
    drops_inserted: int
    drops_skipped: int


def region_for_penguin_server(penguin_server: str) -> str | None:
    """Map a penguin server code to an en/cn fact region, or ``None`` if dropped."""
    return PENGUIN_SERVER_TO_REGION.get(penguin_server)


#: The inverse of :data:`PENGUIN_SERVER_TO_REGION`, derived from it so the two
#: directions cannot drift (§V37): the sync ride-along (§T102/§V58) maps a fact
#: region back to the penguin server it fetches from (en->US, cn->CN). Built once at
#: import; the source map has no duplicate values, so the inverse is unambiguous.
REGION_TO_PENGUIN_SERVER: dict[str, str] = {
    region: server for server, region in PENGUIN_SERVER_TO_REGION.items()
}


def penguin_server_for_region(region: str) -> str | None:
    """Map an en/cn fact region to its penguin server code, or ``None`` if none.

    The inverse of :func:`region_for_penguin_server` (§V54), used by the ``sync``
    ride-along (§V58): a region with no penguin server (e.g. a jp/kr region that is
    not in {en,cn} anyway) is skipped silently rather than mislabelled.
    """
    return REGION_TO_PENGUIN_SERVER.get(region)


def _as_text(value: Any) -> str | None:
    """Stringify a scalar kept field (penguin ``rarity`` is a numeric tier)."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return str(value)
    return None


def parse_items(items_raw: Any) -> list[ParsedItem]:
    """Transform the penguin ``items`` payload (a JSON array) into allowlisted items."""
    if not isinstance(items_raw, list):
        raise ImporterError("penguin items payload must be a top-level JSON array")
    out: list[ParsedItem] = []
    for entry in items_raw:
        if not isinstance(entry, dict):
            continue
        kept = apply_allowlist(entry, ITEM_ALLOWLIST).kept
        game_id = as_str(kept.get("itemId"))
        if game_id is None:
            continue
        out.append(
            ParsedItem(
                game_id=game_id,
                display_name=as_str(kept.get("name")),
                rarity=_as_text(kept.get("rarity")),
                item_type=as_str(kept.get("itemType")),
                provenance_record=kept,
            )
        )
    return out


def parse_matrix(matrix_raw: Any) -> list[ParsedDrop]:
    """Transform the penguin ``result/matrix`` payload into allowlisted drop rows.

    ``drop_rate`` is the expected quantity per run (``quantity / times``); ``None``
    when the sample is empty. A row missing ``stageId``/``itemId`` is dropped.
    """
    if not isinstance(matrix_raw, dict) or "matrix" not in matrix_raw:
        raise ImporterError("penguin matrix missing top-level 'matrix'")
    rows = matrix_raw["matrix"]
    if not isinstance(rows, list):
        raise ImporterError("penguin 'matrix' must be a JSON array")
    out: list[ParsedDrop] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kept = apply_allowlist(row, PENGUIN_MATRIX_ALLOWLIST).kept
        stage_game_id = as_str(kept.get("stageId"))
        item_game_id = as_str(kept.get("itemId"))
        if stage_game_id is None or item_game_id is None:
            continue
        quantity = as_int(kept.get("quantity"))
        times = as_int(kept.get("times"))
        drop_rate = (quantity / times) if (quantity is not None and times) else None
        out.append(
            ParsedDrop(
                stage_game_id=stage_game_id,
                item_game_id=item_game_id,
                quantity=quantity,
                times=times,
                drop_rate=drop_rate,
                provenance_record=kept,
            )
        )
    return out


def _insert_snapshot(
    conn: sqlite3.Connection,
    *,
    region: str,
    source_id: str,
    items_raw: Any,
    matrix_raw: Any,
    fetched_at: str,
) -> str:
    """Insert the penguin ``source_snapshots`` row (its own provenance chain, §V54).

    The ``manifest_hash`` is derived from the fetched payloads so an unchanged fetch
    yields a stable ``snapshot_id`` (like the game-data snapshot manifest, §V37).
    """
    files = {
        "items": sha256_hex(canonical_json(items_raw)),
        "result/matrix": sha256_hex(canonical_json(matrix_raw)),
    }
    digest_input = "\n".join(f"{name}:{files[name]}" for name in sorted(files)).encode("utf-8")
    manifest_hash = sha256_hex(digest_input)
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


def _stage_pk_by_game_id(conn: sqlite3.Connection, region: str) -> dict[str, int]:
    return {
        game_id: stage_pk
        for game_id, stage_pk in conn.execute(
            "SELECT game_id, stage_pk FROM stages WHERE server = ?", (region,)
        )
    }


def _insert_items(
    conn: sqlite3.Connection,
    parsed_items: list[ParsedItem],
    *,
    region: str,
    snapshot_id: str,
) -> dict[str, int]:
    """Insert items for ``region``, returning a game_id -> item_pk map (§V17)."""
    item_pk_by_game_id: dict[str, int] = {}
    for item in parsed_items:
        provenance_id = insert_record_provenance(
            conn,
            snapshot_id=snapshot_id,
            source_path="items",
            source_record_key=item.game_id,
            record=item.provenance_record,
        )
        # A repeated itemId collides on UNIQUE(server, game_id); fail closed with a
        # typed error rather than an uncaught IntegrityError tearing down the build
        # (§V33 / §V3).
        with integrity_guard(
            f"penguin item {item.game_id!r} duplicates (server={region}, game_id)",
            ImporterError,
        ):
            cur = conn.execute(
                "INSERT INTO items (server, game_id, display_name, rarity, item_type, "
                "provenance_id) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    region,
                    item.game_id,
                    item.display_name,
                    item.rarity,
                    item.item_type,
                    provenance_id,
                ),
            )
        item_pk_by_game_id[item.game_id] = int(cur.lastrowid or 0)
    return item_pk_by_game_id


def import_penguin_drops(
    conn: sqlite3.Connection,
    adapter: DropFetcher,
    *,
    penguin_server: str,
    fetched_at: datetime | None = None,
    ttl: timedelta = DEFAULT_DROP_TTL,
    source_id: str = DEFAULT_SOURCE_ID,
) -> PenguinDropImportResult:
    """Fetch + import one penguin server's drops into items + stage_drops.

    ``penguin_server`` maps to a fact region (§V54); a dropped (jp/kr) server returns
    an empty result without a snapshot. Every ``stage_drops`` row is stamped with the
    penguin ``snapshot_id`` + ``fetched_at`` + ``expires_at`` (= ``fetched_at`` +
    ``ttl``) + ``region`` (§V53). A non-empty matrix that resolves to zero drops
    fails closed (§V30).
    """
    region = region_for_penguin_server(penguin_server)
    if region is None:
        # JP/KR penguin servers are outside {en,cn} in v0.2 -- dropped, not mislabelled
        # (§V54). No snapshot, no rows.
        _LOG.warning("penguin server %r has no en/cn fact region; dropped (§V54)", penguin_server)
        return PenguinDropImportResult(
            region=None, snapshot_id=None, items_inserted=0, drops_inserted=0, drops_skipped=0
        )

    items_raw = adapter.fetch("items", server=penguin_server)
    matrix_raw = adapter.fetch("result/matrix", server=penguin_server)
    parsed_items = parse_items(items_raw)
    parsed_drops = parse_matrix(matrix_raw)

    fetched_dt = fetched_at if fetched_at is not None else datetime.now(tz=UTC)
    fetched_iso = fetched_dt.isoformat()
    expires_iso = (fetched_dt + ttl).isoformat()

    snapshot_id = _insert_snapshot(
        conn,
        region=region,
        source_id=source_id,
        items_raw=items_raw,
        matrix_raw=matrix_raw,
        fetched_at=fetched_iso,
    )
    item_pk_by_game_id = _insert_items(conn, parsed_items, region=region, snapshot_id=snapshot_id)
    stage_pk_by_game_id = _stage_pk_by_game_id(conn, region)

    drops_inserted = 0
    drops_skipped = 0
    for drop in parsed_drops:
        stage_pk = stage_pk_by_game_id.get(drop.stage_game_id)
        item_pk = item_pk_by_game_id.get(drop.item_game_id)
        if stage_pk is None or item_pk is None:
            # A drop whose stage or item is absent is skipped, never fabricated
            # (fail-closed per the 0009 migration contract).
            drops_skipped += 1
            _LOG.warning(
                "penguin drop %s/%s skipped for region %s: %s absent",
                drop.stage_game_id,
                drop.item_game_id,
                region,
                "stage" if stage_pk is None else "item",
            )
            continue
        provenance_id = insert_record_provenance(
            conn,
            snapshot_id=snapshot_id,
            source_path="result/matrix",
            source_record_key=f"{drop.stage_game_id}/{drop.item_game_id}",
            record=drop.provenance_record,
        )
        # A duplicate (stage, item) drop collides on UNIQUE(stage_pk, item_pk);
        # map the anomaly to a typed error (§V33), not an uncaught IntegrityError.
        with integrity_guard(
            f"penguin drop {drop.stage_game_id!r}/{drop.item_game_id!r} duplicates (stage, item)",
            ImporterError,
        ):
            conn.execute(
                "INSERT INTO stage_drops (stage_pk, item_pk, region, quantity, times, drop_rate, "
                "snapshot_id, fetched_at, expires_at, provenance_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stage_pk,
                    item_pk,
                    region,
                    drop.quantity,
                    drop.times,
                    drop.drop_rate,
                    snapshot_id,
                    fetched_iso,
                    expires_iso,
                    provenance_id,
                ),
            )
        drops_inserted += 1

    # §V30: a non-empty matrix yielding zero stored drops is a silent-empty regression
    # (a stageId/itemId join failure, or drops fetched for a region with no stages).
    # Fail closed so the candidate is discarded and the active DB stays untouched (§V3).
    if parsed_drops and drops_inserted == 0:
        raise ImporterError(
            f"{region}: penguin matrix had {len(parsed_drops)} drop row(s) but none resolved "
            "to a stage+item; refusing a silent empty drop build (§V30)"
        )

    return PenguinDropImportResult(
        region=region,
        snapshot_id=snapshot_id,
        items_inserted=len(item_pk_by_game_id),
        drops_inserted=drops_inserted,
        drops_skipped=drops_skipped,
    )
