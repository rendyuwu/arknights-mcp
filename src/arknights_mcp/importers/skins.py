"""Skin-gallery importer: skin_table.json -> operator_skins (§T182, ADR 0015).

Parses the primary ``skin_table.json`` ``charSkins`` dict (the SAME
``arknights_assets_gamedata`` snapshot as enemy/stage/operator, §V88 -- NOT a new
source) into the metadata-only named skin gallery:

* the field allowlist + recursive sanitize on every kept entry (§V18/§V31), routed
  through :mod:`arknights_mcp.importers.field_policy` -- only the structural
  identity/label fields (``skinId``/``charId``/``tmplId``/``portraitId``/``isBuySkin``
  + the ``displaySkin`` name/group leaves) survive, so outfit flavor prose
  (``content``/``dialog``/``usage``/``description``/``drawerList``) is never stored
  (§V16/§V18 metadata-only ceiling);
* a TOKEN filter: a ``charSkins`` entry whose ``charId`` is not a ``char_``-prefixed
  operator id is a summon/token skin, not part of the operator gallery -> dropped;
* the alt-form link via ``tmplId`` (ADR 0015): an alt-form skin (Amiya family)
  carries the BASE operator's ``charId`` plus a distinct ``tmplId`` naming the
  alternate playable form, so ``tmpl_id != char_id`` is the discriminator --
  ``char_patch_table.json`` is NOT imported;
* a SOFT-resolve of ``charId`` to an ``operator_pk`` when that operator is present in
  the same snapshot, else the raw char id with a NULL ``operator_pk`` -- an
  unresolvable skin never fails the build (operators are optional-zero per B36, so a
  combat-only snapshot yields raw char ids);
* per-record provenance so a skin carries its provenance chain (§V17); region on
  every row (§V5), en and cn never mixed.

The mirror image URL stays QUERY-TIME DERIVED from ``portrait_id``
(``skin/<portraitId>b.png``, §V63 derive-not-store): no URL and no bytes are
persisted. Pure parsing (:func:`parse_skins`) is separated from the DB write so it
is unit-testable without a database. An entry missing a ``skinId`` or a
``portraitId`` is skipped (fail-closed, no fabricated row -- a skin without its art
stem cannot be surfaced). A non-empty operator ``charSkins`` set that resolves to
zero skins fails closed (§V30); an absent or empty ``skin_table`` is a legitimate
empty build (``operator_skins`` is not a CRITICAL_TABLE -- the table is fetched
tolerant-absent per §V41/B36).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.field_policy import (
    DISPLAY_SKIN_ALLOWLIST,
    SKIN_ALLOWLIST,
    apply_allowlist,
)
from arknights_mcp.importers.guards import guard_not_silently_empty
from arknights_mcp.importers.manifest import insert_record_provenance
from arknights_mcp.importers.operators import operator_pk_by_game_id
from arknights_mcp.sources.base import SourceAdapter
from arknights_mcp.util.coerce import as_dict, as_str
from arknights_mcp.util.sqlite import integrity_guard

_LOG = logging.getLogger(__name__)

_OPERATOR_CHAR_PREFIX = "char_"


@dataclass(frozen=True)
class ParsedSkin:
    skin_id: str
    char_id: str
    tmpl_id: str | None
    portrait_id: str
    display_name: str | None
    skin_group_id: str | None
    skin_group_name: str | None
    is_buy_skin: bool | None
    provenance_record: dict[str, Any]


@dataclass(frozen=True)
class SkinImportResult:
    """Per-server outcome. ``skins_resolved`` counts skins soft-resolved to a present
    operator; the rest carry the raw char id with a NULL ``operator_pk``."""

    skins_inserted: int = 0
    skins_resolved: int = 0


def _is_operator_entry(entry: Any) -> bool:
    """True when a ``charSkins`` entry belongs to an operator (``char_`` charId).

    Token/summon skins carry a ``token_``/``trap_`` charId and are not part of the
    operator gallery (ADR 0015) -- they are dropped before the §V30 candidate count
    so an all-token table still reads as an empty (legitimate) operator gallery.
    """
    return isinstance(entry, dict) and str(entry.get("charId") or "").startswith(
        _OPERATOR_CHAR_PREFIX
    )


def parse_skins(skin_raw: Any) -> list[ParsedSkin]:
    """Transform a raw ``skin_table`` into typed, allowlisted skins (§V18/§V88).

    Reads the ``charSkins`` id-keyed dict; only the ADR 0015 metadata allowlist
    survives (``displaySkin`` sub-extracted, never kept whole), so outfit prose is
    dropped (§V16). Token skins are filtered out. An entry with a missing OR blank
    ``skinId``/``charId``/``portraitId`` is skipped so no row is fabricated without
    its stable id or its art stem (fail-closed). Iteration is key-sorted so the
    parse order (and thus provenance ids) is deterministic across builds.
    """
    if not isinstance(skin_raw, dict):
        raise ImporterError("skin_table is not a JSON object")
    char_skins = skin_raw.get("charSkins")
    if not isinstance(char_skins, dict):
        # A present-but-shapeless skin_table has no charSkins dict: nothing to parse.
        # An absent table is handled upstream in import_skins (optional per snapshot).
        return []
    out: list[ParsedSkin] = []
    for key in sorted(char_skins, key=str):
        entry = char_skins[key]
        if not _is_operator_entry(entry):
            continue
        kept = apply_allowlist(entry, SKIN_ALLOWLIST).kept
        display_skin = apply_allowlist(
            as_dict(entry.get("displaySkin")), DISPLAY_SKIN_ALLOWLIST
        ).kept
        skin_id = as_str(kept.get("skinId"))
        char_id = as_str(kept.get("charId"))
        portrait_id = as_str(kept.get("portraitId"))
        if not skin_id or not char_id or not portrait_id:
            # No stable id or no art stem -> the row can neither be keyed (§V17,
            # UNIQUE(server, skin_id)) nor surfaced (§V63 URL derives from
            # portrait_id). Skip fail-closed, never a fabricated row.
            continue
        is_buy_raw = kept.get("isBuySkin")
        out.append(
            ParsedSkin(
                skin_id=skin_id,
                char_id=char_id,
                # A label sanitized down to an empty string is no label (B52): None.
                tmpl_id=as_str(kept.get("tmplId")) or None,
                portrait_id=portrait_id,
                display_name=as_str(display_skin.get("skinName")) or None,
                skin_group_id=as_str(display_skin.get("skinGroupId")) or None,
                skin_group_name=as_str(display_skin.get("skinGroupName")) or None,
                is_buy_skin=is_buy_raw if isinstance(is_buy_raw, bool) else None,
                provenance_record={**kept, "displaySkin": display_skin},
            )
        )
    return out


def insert_skins(
    conn: sqlite3.Connection,
    parsed: list[ParsedSkin],
    *,
    server: str,
    snapshot_id: str,
    source_path: str,
) -> SkinImportResult:
    """Insert operator_skins rows (§V17/§V33/§V88).

    Each skin's ``char_id`` SOFT-resolves to an ``operator_pk`` when that operator is
    present for ``server`` (via the shared :func:`operator_pk_by_game_id`, §V37),
    else the row keeps the raw char id with a NULL ``operator_pk`` -- an unresolvable
    skin never fails the build. The resolution state is exactly ``operator_pk IS
    NULL``; there is no second ``resolved`` column re-encoding it (§V94/B123). An
    alt-form skin resolves through its BASE ``charId``
    (ADR 0015), so the Amiya-family gallery attaches to base Amiya, labeled via
    ``tmpl_id``. A duplicate ``skinId`` (UNIQUE(server, skin_id)) collides on a
    constraint; that anomaly maps to a typed :class:`ImporterError` rather than an
    uncaught ``IntegrityError`` tearing down the multi-region build (§V33).
    """
    operator_pk_map = operator_pk_by_game_id(conn, server)
    inserted = 0
    resolved_count = 0
    for skin in parsed:
        provenance_id = insert_record_provenance(
            conn,
            snapshot_id=snapshot_id,
            source_path=source_path,
            source_record_key=skin.skin_id,
            record=skin.provenance_record,
        )
        operator_pk = operator_pk_map.get(skin.char_id)
        with integrity_guard(
            f"skin {skin.skin_id!r} collides on a UNIQUE constraint (duplicate skin id)",
            ImporterError,
        ):
            conn.execute(
                "INSERT INTO operator_skins "
                "(server, skin_id, char_id, tmpl_id, operator_pk, display_name, "
                "skin_group_id, skin_group_name, portrait_id, is_buy_skin, region, "
                "provenance_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    server,
                    skin.skin_id,
                    skin.char_id,
                    skin.tmpl_id,
                    operator_pk,
                    skin.display_name,
                    skin.skin_group_id,
                    skin.skin_group_name,
                    skin.portrait_id,
                    None if skin.is_buy_skin is None else int(skin.is_buy_skin),
                    server,  # region == the fact region (§V5); server and region kept in step
                    provenance_id,
                ),
            )
        inserted += 1
        resolved_count += 1 if operator_pk is not None else 0
    return SkinImportResult(skins_inserted=inserted, skins_resolved=resolved_count)


def _operator_entry_count(skin_raw: Any) -> int:
    """Count candidate operator (non-token) ``charSkins`` entries for the §V30 guard."""
    if not isinstance(skin_raw, dict):
        return 0
    char_skins = skin_raw.get("charSkins")
    if not isinstance(char_skins, dict):
        return 0
    return sum(1 for entry in char_skins.values() if _is_operator_entry(entry))


def import_skins(
    conn: sqlite3.Connection,
    adapter: SourceAdapter,
    snapshot_id: str,
    *,
    skin_table_path: str = "gamedata/excel/skin_table.json",
) -> SkinImportResult:
    """Read ``skin_table.json`` via the adapter and import the named skin gallery.

    A snapshot without ``skin_table.json`` (e.g. a combat-only fixture) yields an
    empty result rather than failing, so the skin domain is optional per snapshot
    (B36/§V41). Must run AFTER operators so char ids soft-resolve to a real
    ``operator_pk`` (§V88). A non-empty operator ``charSkins`` set that resolves to
    zero skins fails closed (§V30) so a shape/id mismatch is never promoted as a
    silent empty gallery; the candidate is discarded and the active DB stays
    untouched (§V3). A PARTIAL skip (some entries missing an id/stem while others
    parse) is warned so a truncated gallery is at least visible in the sync log --
    the §V30 guard alone only catches the all-zero case.
    """
    if not adapter.exists(skin_table_path):
        return SkinImportResult()
    skin_raw = adapter.read_json(skin_table_path)
    parsed = parse_skins(skin_raw)
    candidate_count = _operator_entry_count(skin_raw)
    guard_not_silently_empty(
        candidates=candidate_count,
        produced=len(parsed),
        scope=adapter.server,
        source="skin_table",
        unit="operator skin entr(y|ies)",
        resolution="resolved to a skin row",
        outcome="empty skin gallery",
    )
    skipped = candidate_count - len(parsed)
    if skipped > 0:
        _LOG.warning(
            "%s: %d operator skin entr(y|ies) skipped (missing/blank skinId, charId, or "
            "portraitId); the imported gallery may be partially truncated",
            adapter.server,
            skipped,
        )
    return insert_skins(
        conn,
        parsed,
        server=adapter.server,
        snapshot_id=snapshot_id,
        source_path=skin_table_path,
    )
