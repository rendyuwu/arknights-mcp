"""Module importer (§T43; PRD §12.3).

Parses the real ``uniequip_table.json`` (``equipDict`` module metadata + the
``charEquip`` operator→module map) and ``battle_equip_table.json`` (per-level
stat/trait/talent changes) into the normalized module domain: ``modules`` +
``module_levels``.

Applies the explicit field allowlist and string sanitization (§V18/§V31) and
attaches per-record provenance (§V17) to each core ``modules`` row (level rows
link through their parent). The ``INITIAL`` "no-module" default slot is skipped;
a module whose ``charId`` names an operator absent from the roster is skipped
(the ``modules.operator_pk`` FK must resolve, mirroring the operator→skill link).
The per-candidate trait/talent-change effect description TEMPLATE (mechanic text
that references the change's blackboard keys) is imported and rides the
``trait_changes_json`` / ``talent_changes_json`` bundle alongside its blackboard
for grounding (§V65 path (a), ADR 0010). Lore/story prose -- the module's own
``uniEquipDesc`` -- is never allowlisted and is excluded (§V16 ceiling holds);
``module_levels.gameplay_description`` stays ``NULL`` (the module templates live
per-candidate in the change bundles, next to the blackboard they ground).

Per level only the numeric substrate is kept: ``attributeBlackboard`` →
``stat_bonus_json``, the trait/talent override bundles' ``blackboard`` (+ unlock
condition / talent index) → ``trait_changes_json`` / ``talent_changes_json``, and
``itemCost`` → ``cost_json`` -- each re-allowlisted from the raw source rather than
stored whole, so no unallowlisted dict/list leaf reaches a ``*_json`` column
(§V31). Pure parsing (:func:`parse_modules`) is separated from insertion so it is
unit-testable without a database.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.field_policy import (
    ITEM_COST_ALLOWLIST,
    UNIEQUIP_ALLOWLIST,
    allowlist_blackboard,
    apply_allowlist,
)
from arknights_mcp.importers.manifest import insert_record_provenance
from arknights_mcp.importers.operators import operator_pk_by_game_id
from arknights_mcp.sources.base import SourceAdapter
from arknights_mcp.util.coerce import as_int, as_str, json_or_none, suffix_int
from arknights_mcp.util.sqlite import integrity_guard

_LOG = logging.getLogger(__name__)

#: ``uniequip_table`` ``type`` values that are not real modules. ``INITIAL`` is the
#: default "no module" slot every operator carries; only ``ADVANCED`` (and any
#: future real module type) is imported.
_NON_MODULE_TYPES: frozenset[str] = frozenset({"INITIAL"})


# --- parsed shapes -----------------------------------------------------------


@dataclass(frozen=True)
class ParsedModuleLevel:
    level: int
    stat_bonus: list[dict[str, Any]] | None
    trait_changes: list[dict[str, Any]] | None
    talent_changes: list[dict[str, Any]] | None
    cost: list[dict[str, Any]] | None


@dataclass(frozen=True)
class ParsedModule:
    game_id: str
    operator_game_id: str
    module_type: str | None
    display_name: str | None
    unlock_phase: int | None
    unlock_level: int | None
    levels: list[ParsedModuleLevel]
    provenance_record: dict[str, Any]


@dataclass(frozen=True)
class ModuleImportResult:
    modules_inserted: int = 0
    module_levels_inserted: int = 0


# --- helpers -----------------------------------------------------------------


def _module_type(type_name1: Any, type_name2: Any) -> str | None:
    """Combine the branch code + variant into the recognizable module type.

    Real modules carry ``typeName1`` (branch, e.g. ``"SPL"``) and ``typeName2``
    (variant, e.g. ``"Y"``); players know the combined ``"SPL-Y"`` form. Both are
    read from an already-allowlisted+sanitized ``kept`` dict, so no further cleaning
    is needed. A missing variant yields the branch alone.
    """
    branch = as_str(type_name1)
    variant = as_str(type_name2)
    if branch and variant:
        return f"{branch}-{variant}"
    return branch or variant or None


def _int_key(key: Any) -> int | None:
    """Parse an ``itemCost`` level key (``"1"``/``"2"``/``"3"`` or a bare int)."""
    if isinstance(key, bool):
        return None
    if isinstance(key, int):
        return key
    if isinstance(key, str):
        try:
            return int(key.strip())
        except ValueError:
            return None
    return None


def _candidates(bundle: Any) -> list[dict[str, Any]]:
    """The ``candidates`` list of a trait/talent override bundle (never ``None``)."""
    if not isinstance(bundle, dict):
        return []
    cands = bundle.get("candidates")
    if not isinstance(cands, list):
        return []
    return [c for c in cands if isinstance(c, dict)]


def _unlock_condition(cond: Any) -> dict[str, Any] | None:
    """Extract the structural ``{phase, level}`` unlock condition (no prose)."""
    if not isinstance(cond, dict):
        return None
    return {"phase": as_str(cond.get("phase"), sanitize=True), "level": as_int(cond.get("level"))}


def _effect_template(source: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """First present, non-empty, sanitized effect-description TEMPLATE among ``keys``.

    The module trait/talent-change bundles carry the in-game effect description
    template under a source-shape-specific key (trait: ``additionalDescription`` then
    ``overrideDescripton``; talent: ``upgradeDescription`` then ``description``). The
    template is mechanic text that references the sibling ``blackboard`` keys, so it is
    imported + emitted alongside the blackboard for grounding (§V65 path (a), ADR 0010).
    Read from the raw candidate (not the allowlist), so it is sanitized + control-
    stripped + length-capped here as untrusted data (§V18). A blank-after-sanitize or
    absent value yields ``None`` -- never an empty template.
    """
    for key in keys:
        text = as_str(source.get(key), sanitize=True)
        if text:
            return text
    return None


def _trait_change(cand: dict[str, Any]) -> dict[str, Any]:
    """One ``overrideTraitDataBundle`` candidate → numeric params + effect template.

    Keeps the ``blackboard`` params, unlock condition, potential rank, and the in-game
    trait effect description TEMPLATE (``additionalDescription`` then the misspelled
    upstream ``overrideDescripton``) -- mechanic text referencing the blackboard keys,
    emitted alongside them for grounding (§V65 path (a), ADR 0010; sanitized + capped
    §V18). No lore/story prose is kept (§V16 ceiling).
    """
    return {
        "unlockCondition": _unlock_condition(cand.get("unlockCondition")),
        "requiredPotentialRank": as_int(cand.get("requiredPotentialRank")),
        "blackboard": allowlist_blackboard(cand.get("blackboard")),
        "description": _effect_template(cand, ("additionalDescription", "overrideDescripton")),
    }


def _talent_change(cand: dict[str, Any]) -> dict[str, Any]:
    """One ``addOrOverrideTalentDataBundle`` candidate → numeric params + effect template.

    Keeps the talent index, unlock condition, potential rank, ``blackboard`` params,
    and the in-game talent effect description TEMPLATE (``upgradeDescription`` then
    ``description``) -- mechanic text referencing the blackboard keys, emitted alongside
    them for grounding (§V65 path (a), ADR 0010; sanitized + capped §V18). The ``name``
    label and any lore/story prose are dropped (§V16 ceiling).
    """
    return {
        "talentIndex": as_int(cand.get("talentIndex")),
        "unlockCondition": _unlock_condition(cand.get("unlockCondition")),
        "requiredPotentialRank": as_int(cand.get("requiredPotentialRank")),
        "blackboard": allowlist_blackboard(cand.get("blackboard")),
        "description": _effect_template(cand, ("upgradeDescription", "description")),
    }


def _parse_parts(parts: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a phase's ``parts`` into trait changes + talent changes (numeric only)."""
    trait_changes: list[dict[str, Any]] = []
    talent_changes: list[dict[str, Any]] = []
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict):
            continue
        for cand in _candidates(part.get("overrideTraitDataBundle")):
            trait_changes.append(_trait_change(cand))
        for cand in _candidates(part.get("addOrOverrideTalentDataBundle")):
            talent_changes.append(_talent_change(cand))
    return trait_changes, talent_changes


def _parse_item_cost(item_cost_raw: Any) -> dict[int, list[dict[str, Any]]]:
    """Real per-level ``itemCost`` (``{"1": [...], ...}``) → ``{level: [items]}``.

    Each item is re-allowlisted to ``{id, count, type}`` (§V31); unknown fields are
    dropped. A ``null`` / absent ``itemCost`` (the INITIAL default) yields ``{}``.
    """
    out: dict[int, list[dict[str, Any]]] = {}
    if not isinstance(item_cost_raw, dict):
        return out
    for key, items in item_cost_raw.items():
        level = _int_key(key)
        if level is None or not isinstance(items, list):
            continue
        out[level] = [
            apply_allowlist(item, ITEM_COST_ALLOWLIST).kept
            for item in items
            if isinstance(item, dict)
        ]
    return out


def _parse_module_levels(
    battle_raw: Any, item_cost_raw: Any
) -> tuple[list[ParsedModuleLevel], list[dict[str, Any]]]:
    """Merge ``battle_equip`` phases + ``itemCost`` into per-level change rows."""
    stat_by_level: dict[int, dict[str, Any]] = {}
    phases = battle_raw.get("phases") if isinstance(battle_raw, dict) else None
    for i, phase in enumerate(phases if isinstance(phases, list) else []):
        if not isinstance(phase, dict):
            continue
        level = as_int(phase.get("equipLevel"))
        if level is None:
            level = i + 1
        trait_changes, talent_changes = _parse_parts(phase.get("parts"))
        stat_by_level[level] = {
            "stat_bonus": allowlist_blackboard(phase.get("attributeBlackboard")),
            "trait_changes": trait_changes or None,
            "talent_changes": talent_changes or None,
        }
    cost_by_level = _parse_item_cost(item_cost_raw)

    levels: list[ParsedModuleLevel] = []
    kept_levels: list[dict[str, Any]] = []
    for level in sorted(set(stat_by_level) | set(cost_by_level)):
        changes = stat_by_level.get(level, {})
        cost = cost_by_level.get(level) or None
        parsed = ParsedModuleLevel(
            level=level,
            stat_bonus=changes.get("stat_bonus"),
            trait_changes=changes.get("trait_changes"),
            talent_changes=changes.get("talent_changes"),
            cost=cost,
        )
        levels.append(parsed)
        kept_levels.append(
            {
                "level": level,
                "stat_bonus": parsed.stat_bonus,
                "trait_changes": parsed.trait_changes,
                "talent_changes": parsed.talent_changes,
                "cost": parsed.cost,
            }
        )
    return levels, kept_levels


# --- parsing -----------------------------------------------------------------


def parse_modules(uniequip_raw: Any, battle_equip_raw: Any) -> list[ParsedModule]:
    """Transform the raw ``uniequip_table`` + ``battle_equip_table`` into modules.

    Reads ``equipDict`` (the module metadata, a.k.a. ``uniequip_data``); the
    ``INITIAL`` default slot is skipped and only real modules are returned. Operator
    resolution happens at insertion (needs the DB ``operator_pk``); a module without
    a ``charId`` is dropped here since it can never link.
    """
    equip_dict = uniequip_raw.get("equipDict") if isinstance(uniequip_raw, dict) else None
    if not isinstance(equip_dict, dict):
        return []
    battle = battle_equip_raw if isinstance(battle_equip_raw, dict) else {}
    parsed: list[ParsedModule] = []
    for game_id in sorted(equip_dict):
        entry = equip_dict[game_id]
        if not isinstance(entry, dict) or not isinstance(game_id, str):
            continue
        if as_str(entry.get("type")) in _NON_MODULE_TYPES:
            continue
        kept = apply_allowlist(entry, UNIEQUIP_ALLOWLIST).kept
        operator_game_id = as_str(kept.get("charId"))
        if not operator_game_id:
            continue
        levels, kept_levels = _parse_module_levels(battle.get(game_id), entry.get("itemCost"))
        parsed.append(
            ParsedModule(
                game_id=game_id,
                operator_game_id=operator_game_id,
                module_type=_module_type(kept.get("typeName1"), kept.get("typeName2")),
                display_name=as_str(kept.get("uniEquipName")),
                unlock_phase=suffix_int(kept.get("unlockEvolvePhase"), "PHASE_"),
                unlock_level=as_int(kept.get("unlockLevel")),
                levels=levels,
                provenance_record={"uniequip": kept, "levels": kept_levels},
            )
        )
    return parsed


# --- insertion ---------------------------------------------------------------


def insert_modules(
    conn: sqlite3.Connection,
    parsed: list[ParsedModule],
    *,
    server: str,
    snapshot_id: str,
    uniequip_source_path: str,
) -> ModuleImportResult:
    """Insert modules + module_levels (§V17/§V33).

    A module whose ``charId`` names an operator not present for ``server`` is
    skipped (the ``operator_pk`` FK cannot resolve), not inserted with a dangling
    reference. A duplicate ``(server, game_id)`` collides on UNIQUE and raises a
    typed :class:`ImporterError` rather than tearing down the build (§V33).
    """
    operator_pk_map = operator_pk_by_game_id(conn, server)
    modules_inserted = 0
    levels_inserted = 0
    for module in parsed:
        operator_pk = operator_pk_map.get(module.operator_game_id)
        if operator_pk is None:
            _LOG.warning(
                "module %s references operator %r absent from operators; skipping",
                module.game_id,
                module.operator_game_id,
            )
            continue
        provenance_id = insert_record_provenance(
            conn,
            snapshot_id=snapshot_id,
            source_path=uniequip_source_path,
            source_record_key=module.game_id,
            record=module.provenance_record,
        )
        with integrity_guard(
            f"module {module.game_id!r} collides on UNIQUE(server, game_id) or a duplicate level",
            ImporterError,
        ):
            cur = conn.execute(
                "INSERT INTO modules "
                "(server, game_id, operator_pk, module_type, display_name, unlock_phase, "
                "unlock_level, provenance_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    server,
                    module.game_id,
                    operator_pk,
                    module.module_type,
                    module.display_name,
                    module.unlock_phase,
                    module.unlock_level,
                    provenance_id,
                ),
            )
            module_pk = int(cur.lastrowid or 0)
            modules_inserted += 1
            for level in module.levels:
                conn.execute(
                    "INSERT INTO module_levels "
                    "(module_pk, level, stat_bonus_json, trait_changes_json, "
                    "talent_changes_json, cost_json, gameplay_description) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        module_pk,
                        level.level,
                        json_or_none(level.stat_bonus),
                        json_or_none(level.trait_changes),
                        json_or_none(level.talent_changes),
                        json_or_none(level.cost),
                        None,  # prose excluded by default (§V16)
                    ),
                )
                levels_inserted += 1
    return ModuleImportResult(
        modules_inserted=modules_inserted, module_levels_inserted=levels_inserted
    )


def import_modules(
    conn: sqlite3.Connection,
    adapter: SourceAdapter,
    snapshot_id: str,
    *,
    uniequip_table_path: str = "gamedata/excel/uniequip_table.json",
    battle_equip_table_path: str = "gamedata/excel/battle_equip_table.json",
) -> ModuleImportResult:
    """Read the uniequip + battle_equip tables via the adapter and import them.

    A snapshot without ``uniequip_table.json`` (e.g. a combat-only fixture) yields
    an empty result rather than failing, so the module domain is optional per
    snapshot. Operators must already be imported so each module's ``charId``
    resolves to an ``operator_pk``.
    """
    if not adapter.exists(uniequip_table_path):
        return ModuleImportResult()
    uniequip_raw = adapter.read_json(uniequip_table_path)
    battle_raw = (
        adapter.read_json(battle_equip_table_path)
        if adapter.exists(battle_equip_table_path)
        else {}
    )
    parsed = parse_modules(uniequip_raw, battle_raw)
    return insert_modules(
        conn,
        parsed,
        server=adapter.server,
        snapshot_id=snapshot_id,
        uniequip_source_path=uniequip_table_path,
    )
