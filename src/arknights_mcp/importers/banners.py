"""Banner-archive importer: gacha_table.json -> banners + banner_featured_ops (§T113).

Parses the primary ``gacha_table.json`` ``gachaPoolClient`` (the SAME
``arknights_assets_gamedata`` snapshot as enemy/stage/operator, §V62 -- NOT a new
source) into the metadata-only banner archive:

* the field allowlist + recursive sanitize on every kept pool entry (§V18/§V31),
  routed through :mod:`arknights_mcp.importers.field_policy` -- only the structural
  schedule/identity fields (``gachaPoolId``/``gachaPoolName``/``openTime``/``endTime``/
  ``gachaRuleType``) survive, so gacha prose (``gachaPoolSummary``/``gachaPoolDetail``/
  ``dynMeta`` html/image) is never stored (§V16/§V62 metadata-only ceiling);
* the typed featured operator ids, extracted per rule type (§V62): a ``LIMITED``
  banner names one featured op under ``limitParam.limitedCharId``; a CLASSIC-family
  banner an array under ``dynMeta.attainRare6CharList``; ``NORMAL``/``SINGLE``/
  ``DOUBLE``/``LINKAGE`` carry no typed featured-op (rate-up lives only in prose, which
  is §V18-forbidden) -> none emitted;
* a SOFT-resolve of each featured char id to an ``operator_pk`` when that operator is
  present in the same snapshot, else the raw char id with ``resolved = 0`` -- an
  unresolvable featured-op never fails the build (the archive is a standalone FACT,
  §V3/§V62; operators are optional-zero per B36 so a combat-only snapshot yields raw
  char ids);
* per-record provenance so a banner carries its provenance chain (§V17); region on
  every row (§V5), en and cn never mixed.

Unix-epoch ``openTime``/``endTime`` are normalized to ISO here (CLEAN integer epochs,
unlike the year-less announcement feed §V61). Pure parsing (:func:`parse_banners`) is
separated from the DB write so it is unit-testable without a database. A pool entry
missing a ``gachaPoolId`` is skipped (fail-closed, no fabricated row). A non-empty
``gachaPoolClient`` that resolves to zero banners fails closed (§V30); an absent or
empty ``gacha_table`` is a legitimate empty build (``banners`` is not a CRITICAL_TABLE
-- the table is fetched tolerant-absent per §V41/B36).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.field_policy import (
    BANNER_ALLOWLIST,
    DYN_META_ALLOWLIST,
    LIMIT_PARAM_ALLOWLIST,
    apply_allowlist,
)
from arknights_mcp.importers.manifest import insert_record_provenance
from arknights_mcp.importers.operators import operator_pk_by_game_id
from arknights_mcp.sources.base import SourceAdapter
from arknights_mcp.util.coerce import as_int, as_str
from arknights_mcp.util.sqlite import integrity_guard

_LOG = logging.getLogger(__name__)

#: The single ``gachaRuleType`` naming a featured op under ``limitParam.limitedCharId``.
_LIMITED_RULE_TYPE = "LIMITED"

#: Rule types whose featured 6-star ops live in the ``dynMeta.attainRare6CharList``
#: array (§V62, verified vs live EN+CN 2026-07-21). Any other rule type
#: (``NORMAL``/``SINGLE``/``DOUBLE``/``LINKAGE``) carries no typed featured-op -- its
#: rate-up is prose only (§V18-forbidden), so none is emitted + a limitation is surfaced
#: by the read tool (§V26/§V62).
_CLASSIC_FAMILY_RULE_TYPES: frozenset[str] = frozenset(
    {"ATTAIN", "CLASSIC", "CLASSIC_ATTAIN", "CLASSIC_DOUBLE", "FESCLASSIC", "SPECIAL"}
)


@dataclass(frozen=True)
class ParsedBanner:
    game_id: str
    display_name: str | None
    open_time: str | None
    end_time: str | None
    rule_type: str | None
    featured_char_ids: list[str]
    provenance_record: dict[str, Any]


@dataclass(frozen=True)
class BannerImportResult:
    """Per-server outcome. ``featured_ops_resolved`` counts featured ops soft-resolved
    to a present operator; the rest carry the raw char id with ``resolved = 0``."""

    banners_inserted: int = 0
    featured_ops_inserted: int = 0
    featured_ops_resolved: int = 0


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a dict, else an empty dict (narrowing)."""
    return value if isinstance(value, dict) else {}


def _epoch_to_iso(value: Any) -> str | None:
    """Normalize a unix-epoch int to an ISO UTC timestamp, or ``None`` (§V62).

    ``openTime``/``endTime`` are clean integer epochs; a non-int or an out-of-range
    epoch yields ``None`` rather than a fabricated timestamp (§V26).
    """
    epoch = as_int(value)
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _featured_char_ids(
    rule_type: str | None, entry: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    """Extract the typed featured char ids for a pool entry, per rule type (§V62).

    Returns the char ids plus the sub-allowlisted parent block (for provenance): a
    ``LIMITED`` banner reads ``limitParam.limitedCharId`` (single), a CLASSIC-family
    banner ``dynMeta.attainRare6CharList`` (array). ``dynMeta``/``limitParam`` are NOT
    kept whole (``dynMeta`` also carries prose/html), only their typed featured-op leaf
    survives its own sub-allowlist (§V18/§V31/§V62). Any other rule type carries no typed
    featured-op -> empty.
    """
    if rule_type == _LIMITED_RULE_TYPE:
        limit_param = apply_allowlist(_as_dict(entry.get("limitParam")), LIMIT_PARAM_ALLOWLIST).kept
        char_id = as_str(limit_param.get("limitedCharId"))
        ids = [char_id] if char_id else []
        return ids, ({"limitParam": limit_param} if limit_param else {})
    if rule_type in _CLASSIC_FAMILY_RULE_TYPES:
        dyn_meta = apply_allowlist(_as_dict(entry.get("dynMeta")), DYN_META_ALLOWLIST).kept
        raw_list = dyn_meta.get("attainRare6CharList")
        ids = (
            [c for c in raw_list if isinstance(c, str) and c] if isinstance(raw_list, list) else []
        )
        return ids, ({"dynMeta": dyn_meta} if dyn_meta else {})
    return [], {}


def parse_banners(gacha_raw: Any) -> list[ParsedBanner]:
    """Transform a raw ``gacha_table`` into typed, allowlisted banners (§V18/§V62).

    Reads the ``gachaPoolClient`` list; only the §V62 metadata allowlist survives, so
    gacha prose/summary/detail/html/image is dropped (§V16). A pool entry with a missing
    OR blank ``gachaPoolId`` is skipped so no row is fabricated without its stable id
    (fail-closed). ``openTime``/``endTime`` unix epochs are normalized to ISO, and the
    typed featured ops are extracted per rule type.
    """
    if not isinstance(gacha_raw, dict):
        raise ImporterError("gacha_table is not a JSON object")
    pools = gacha_raw.get("gachaPoolClient")
    if not isinstance(pools, list):
        # A present-but-shapeless gacha_table has no pool list: nothing to parse. An
        # absent table is handled upstream in import_banners (optional per snapshot).
        return []
    out: list[ParsedBanner] = []
    for entry in pools:
        if not isinstance(entry, dict):
            continue
        kept = apply_allowlist(entry, BANNER_ALLOWLIST).kept
        game_id = as_str(kept.get("gachaPoolId"))
        if not game_id:
            # A missing OR blank gachaPoolId is skipped so no row is fabricated without a
            # stable id (an empty id would also collide on UNIQUE(server, game_id) or
            # stamp a keyless provenance record). Fail-closed, never a fabricated row.
            continue
        rule_type = as_str(kept.get("gachaRuleType"))
        featured_ids, featured_kept = _featured_char_ids(rule_type, entry)
        out.append(
            ParsedBanner(
                game_id=game_id,
                display_name=as_str(kept.get("gachaPoolName")),
                open_time=_epoch_to_iso(kept.get("openTime")),
                end_time=_epoch_to_iso(kept.get("endTime")),
                rule_type=rule_type,
                featured_char_ids=featured_ids,
                provenance_record={**kept, **featured_kept},
            )
        )
    return out


def insert_banners(
    conn: sqlite3.Connection,
    parsed: list[ParsedBanner],
    *,
    server: str,
    snapshot_id: str,
    source_path: str,
) -> BannerImportResult:
    """Insert banners + banner_featured_ops (§V17/§V33/§V62).

    Each featured char id SOFT-resolves to an ``operator_pk`` when that operator is
    present for ``server`` (via the shared :func:`operator_pk_by_game_id`, §V37), else
    the row keeps the raw char id with ``resolved = 0`` -- an unresolvable featured-op
    never fails the build (§V3/§V62). A duplicate ``gachaPoolId`` (UNIQUE(server,
    game_id)) or a repeated featured char id on one banner (UNIQUE(banner_pk, char_id))
    collides on a constraint; that anomaly maps to a typed :class:`ImporterError`
    rather than an uncaught ``IntegrityError`` tearing down the multi-region build (§V33).
    """
    operator_pk_map = operator_pk_by_game_id(conn, server)
    banners_inserted = 0
    featured_inserted = 0
    featured_resolved = 0
    for banner in parsed:
        provenance_id = insert_record_provenance(
            conn,
            snapshot_id=snapshot_id,
            source_path=source_path,
            source_record_key=banner.game_id,
            record=banner.provenance_record,
        )
        with integrity_guard(
            f"banner {banner.game_id!r} collides on a UNIQUE constraint "
            "(duplicate pool id, or a repeated featured char id on one banner)",
            ImporterError,
        ):
            cur = conn.execute(
                "INSERT INTO banners "
                "(server, game_id, display_name, open_time, end_time, rule_type, region, "
                "provenance_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    server,
                    banner.game_id,
                    banner.display_name,
                    banner.open_time,
                    banner.end_time,
                    banner.rule_type,
                    server,  # region == the fact region (§V5); server and region kept in step
                    provenance_id,
                ),
            )
            banner_pk = int(cur.lastrowid or 0)
            banners_inserted += 1
            for char_id in banner.featured_char_ids:
                operator_pk = operator_pk_map.get(char_id)
                resolved = 1 if operator_pk is not None else 0
                conn.execute(
                    "INSERT INTO banner_featured_ops "
                    "(banner_pk, operator_pk, char_id, resolved) VALUES (?, ?, ?, ?)",
                    (banner_pk, operator_pk, char_id, resolved),
                )
                featured_inserted += 1
                featured_resolved += resolved
    return BannerImportResult(
        banners_inserted=banners_inserted,
        featured_ops_inserted=featured_inserted,
        featured_ops_resolved=featured_resolved,
    )


def _pool_entry_count(gacha_raw: Any) -> int:
    """Count candidate (dict) entries in ``gachaPoolClient`` for the §V30 guard."""
    if not isinstance(gacha_raw, dict):
        return 0
    pools = gacha_raw.get("gachaPoolClient")
    if not isinstance(pools, list):
        return 0
    return sum(1 for entry in pools if isinstance(entry, dict))


def import_banners(
    conn: sqlite3.Connection,
    adapter: SourceAdapter,
    snapshot_id: str,
    *,
    gacha_table_path: str = "gamedata/excel/gacha_table.json",
) -> BannerImportResult:
    """Read ``gacha_table.json`` via the adapter and import the banner archive.

    A snapshot without ``gacha_table.json`` (e.g. a combat-only fixture) yields an
    empty result rather than failing, so the banner domain is optional per snapshot
    (B36/§V41). Must run AFTER operators so featured char ids soft-resolve to a real
    ``operator_pk`` (§V62). A non-empty ``gachaPoolClient`` that resolves to zero
    banners fails closed (§V30) so a shape/id mismatch is never promoted as a silent
    empty banner build; the candidate is discarded and the active DB stays untouched
    (§V3).
    """
    if not adapter.exists(gacha_table_path):
        return BannerImportResult()
    gacha_raw = adapter.read_json(gacha_table_path)
    parsed = parse_banners(gacha_raw)
    pool_count = _pool_entry_count(gacha_raw)
    if pool_count and not parsed:
        raise ImporterError(
            f"{adapter.server}: gacha_table had {pool_count} pool entr(y|ies) but none "
            "resolved to a banner; refusing a silent empty banner build (§V30)"
        )
    return insert_banners(
        conn,
        parsed,
        server=adapter.server,
        snapshot_id=snapshot_id,
        source_path=gacha_table_path,
    )
