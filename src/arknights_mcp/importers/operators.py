"""Operator / skill / talent importer (§T42; PRD §12.3).

Parses the real ``character_table.json`` + ``skill_table.json`` shapes (both
top-level id-keyed dicts, no wrapper) into the normalized operator domain:
``operators`` + ``operator_aliases`` + ``operator_phases`` + ``skills`` +
``operator_skills`` + ``skill_levels`` + ``talents`` + ``talent_levels``.

Applies the explicit field allowlist and string sanitization (§V18/§V31) and
attaches per-record provenance (§V17) to each core row (operators + skills);
sub-tables link through their parent. Prose fields (operator/skill/talent
``description``, ``itemUsage``, …) are never allowlisted, and the
``gameplay_description`` columns are left ``NULL`` by default (§V16). Pure parsing
(:func:`parse_operators` / :func:`parse_skills`) is separated from insertion so it
is unit-testable without a database.

Nested numeric blocks (``spData``, phase attribute ``data``) and ``blackboard``
parameter lists are each re-allowlisted from the raw source rather than stored
whole, so no unallowlisted dict/list leaf reaches a ``*_json`` column (§V31).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.field_policy import (
    BLACKBOARD_ALLOWLIST,
    CHARACTER_ALLOWLIST,
    PHASE_ALLOWLIST,
    PHASE_ATTR_ALLOWLIST,
    SKILL_LEVEL_ALLOWLIST,
    SKILL_LINK_ALLOWLIST,
    SP_DATA_ALLOWLIST,
    TALENT_CANDIDATE_ALLOWLIST,
    apply_allowlist,
)
from arknights_mcp.importers.manifest import insert_record_provenance
from arknights_mcp.sources.base import SourceAdapter
from arknights_mcp.util.coerce import as_float, as_int, as_str, json_or_none
from arknights_mcp.util.sqlite import integrity_guard

_LOG = logging.getLogger(__name__)

#: character_table professions that are not player operators (summon tokens,
#: map traps); excluded so search + operator intel cover operators only.
_NON_OPERATOR_PROFESSIONS: frozenset[str] = frozenset({"TOKEN", "TRAP"})


# --- parsed shapes -----------------------------------------------------------


@dataclass(frozen=True)
class ParsedSkillLevel:
    level: int
    sp_cost: int | None
    initial_sp: int | None
    duration: float | None
    range_id: str | None
    blackboard: Any


@dataclass(frozen=True)
class ParsedSkill:
    game_id: str
    display_name: str | None
    skill_type: str | None
    sp_type: str | None
    duration_type: str | None
    levels: list[ParsedSkillLevel]
    provenance_record: dict[str, Any]


@dataclass(frozen=True)
class ParsedPhase:
    phase: int
    max_level: int | None
    max_hp: int | None
    atk: int | None
    def_: int | None
    res: int | None
    redeploy_time: int | None
    cost: int | None
    block_count: int | None
    attack_interval: float | None
    range_id: str | None


@dataclass(frozen=True)
class ParsedSkillLink:
    skill_game_id: str
    slot_index: int
    unlock_phase: int | None
    unlock_level: int | None


@dataclass(frozen=True)
class ParsedTalentVariant:
    variant_index: int
    unlock_phase: int | None
    unlock_level: int | None
    potential_rank: int | None
    blackboard: Any


@dataclass(frozen=True)
class ParsedTalent:
    talent_index: int
    display_name: str | None
    variants: list[ParsedTalentVariant]


@dataclass(frozen=True)
class ParsedAlias:
    alias: str
    alias_type: str
    normalized_alias: str


@dataclass(frozen=True)
class ParsedOperator:
    game_id: str
    display_name: str | None
    rarity: int | None
    profession: str | None
    subclass_id: str | None
    position: str | None
    tags: list[str]
    obtainable: bool
    aliases: list[ParsedAlias]
    phases: list[ParsedPhase]
    skill_links: list[ParsedSkillLink]
    talents: list[ParsedTalent]
    provenance_record: dict[str, Any]


@dataclass(frozen=True)
class OperatorImportResult:
    operators_inserted: int = 0
    skills_inserted: int = 0
    phases_inserted: int = 0
    talents_inserted: int = 0
    skill_links_inserted: int = 0
    aliases_inserted: int = 0


# --- coercion helpers --------------------------------------------------------


def _suffix_int(value: Any, prefix: str) -> int | None:
    """``"TIER_6"`` / ``"PHASE_0"`` → ``6`` / ``0``; a plain int passes through.

    Real ``rarity`` (``TIER_<n>``, 1-indexed: ``TIER_6`` = 6★) and unlock
    ``phase`` (``PHASE_<n>``) are enum strings; older dumps use a bare int, kept
    as-is (a documented limitation for the 0-indexed legacy form). ``bool`` — an
    ``int`` subclass — is rejected so a stray ``True`` never counts as ``1``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        upper = value.strip().upper()
        head = prefix.upper()
        tail = upper[len(head) :]
        if upper.startswith(head) and tail.isdigit():
            return int(tail)
    return None


def _enum_text(value: Any) -> str | None:
    """Enum-ish field (``skillType``/``spType``/``durationType``) → text or ``None``.

    The value is read from an already-allowlisted+sanitized ``kept`` dict, so a
    ``str`` needs no further cleaning; an ``int`` code is stringified so both the
    modern string form and the legacy numeric form round-trip into a ``TEXT``
    column. ``bool`` is rejected (an ``int`` subclass, never a real code).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, int):
        return str(value)
    return None


def _allowlist_blackboard(raw: Any) -> list[dict[str, Any]] | None:
    """Strictly allowlist a ``blackboard`` list to ``{key, value, valueStr}`` items.

    Read from the raw source (not a broadly-kept parent), so no unallowlisted
    parameter key or prose leaf is stored (§V31). ``None`` for an absent/empty list.
    """
    if not isinstance(raw, list):
        return None
    out = [
        apply_allowlist(item, BLACKBOARD_ALLOWLIST).kept for item in raw if isinstance(item, dict)
    ]
    return out or None


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a dict, else an empty dict (single narrowing home)."""
    return value if isinstance(value, dict) else {}


# --- skills ------------------------------------------------------------------


def parse_skills(skill_raw: Any) -> list[ParsedSkill]:
    """Transform raw ``skill_table`` (id-keyed dict) into typed, allowlisted skills."""
    if not isinstance(skill_raw, dict):
        return []
    parsed: list[ParsedSkill] = []
    for game_id in sorted(skill_raw):
        entry = skill_raw[game_id]
        if not isinstance(entry, dict) or not isinstance(game_id, str):
            continue
        raw_levels = entry.get("levels")
        raw_levels = raw_levels if isinstance(raw_levels, list) else []
        levels: list[ParsedSkillLevel] = []
        kept_levels: list[dict[str, Any]] = []
        for i, raw_level in enumerate(raw_levels):
            if not isinstance(raw_level, dict):
                continue
            kept = apply_allowlist(raw_level, SKILL_LEVEL_ALLOWLIST).kept
            sp = apply_allowlist(_as_dict(raw_level.get("spData")), SP_DATA_ALLOWLIST).kept
            blackboard = _allowlist_blackboard(raw_level.get("blackboard"))
            kept_levels.append({**kept, "spData": sp, "blackboard": blackboard})
            levels.append(
                ParsedSkillLevel(
                    level=i + 1,
                    sp_cost=as_int(sp.get("spCost")),
                    initial_sp=as_int(sp.get("initSp")),
                    duration=as_float(kept.get("duration")),
                    range_id=as_str(kept.get("rangeId")),
                    blackboard=blackboard,
                )
            )
        first = kept_levels[0] if kept_levels else {}
        first_sp = _as_dict(first.get("spData"))
        parsed.append(
            ParsedSkill(
                game_id=game_id,
                display_name=as_str(first.get("name")),
                skill_type=_enum_text(first.get("skillType")),
                sp_type=_enum_text(first_sp.get("spType")),
                duration_type=_enum_text(first.get("durationType")),
                levels=levels,
                provenance_record={"skill_id": game_id, "levels": kept_levels},
            )
        )
    return parsed


def insert_skills(
    conn: sqlite3.Connection,
    parsed: list[ParsedSkill],
    *,
    server: str,
    snapshot_id: str,
    skill_source_path: str,
) -> dict[str, int]:
    """Insert skills + skill_levels; return ``{skill game_id: skill_pk}`` for linking."""
    skill_pk_by_game_id: dict[str, int] = {}
    for skill in parsed:
        provenance_id = insert_record_provenance(
            conn,
            snapshot_id=snapshot_id,
            source_path=skill_source_path,
            source_record_key=skill.game_id,
            record=skill.provenance_record,
        )
        with integrity_guard(
            f"skill {skill.game_id!r} collides on UNIQUE(server, game_id) or a duplicate level",
            ImporterError,
        ):
            cur = conn.execute(
                "INSERT INTO skills "
                "(server, game_id, display_name, skill_type, sp_type, duration_type, "
                "provenance_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    server,
                    skill.game_id,
                    skill.display_name,
                    skill.skill_type,
                    skill.sp_type,
                    skill.duration_type,
                    provenance_id,
                ),
            )
            skill_pk = int(cur.lastrowid or 0)
            for level in skill.levels:
                conn.execute(
                    "INSERT INTO skill_levels "
                    "(skill_pk, level, sp_cost, initial_sp, duration, range_id, "
                    "blackboard_json, gameplay_description) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        skill_pk,
                        level.level,
                        level.sp_cost,
                        level.initial_sp,
                        level.duration,
                        level.range_id,
                        json_or_none(level.blackboard),
                        None,  # prose excluded by default (§V16)
                    ),
                )
        skill_pk_by_game_id[skill.game_id] = skill_pk
    return skill_pk_by_game_id


# --- operators ---------------------------------------------------------------


def _parse_phases(raw_phases: Any) -> tuple[list[ParsedPhase], list[dict[str, Any]]]:
    phases: list[ParsedPhase] = []
    kept_phases: list[dict[str, Any]] = []
    for i, raw in enumerate(raw_phases if isinstance(raw_phases, list) else []):
        if not isinstance(raw, dict):
            continue
        kept = apply_allowlist(raw, PHASE_ALLOWLIST).kept
        frames = raw.get("attributesKeyFrames")
        last = frames[-1] if isinstance(frames, list) and frames else {}
        data_raw = last.get("data") if isinstance(last, dict) else {}
        data = apply_allowlist(
            data_raw if isinstance(data_raw, dict) else {}, PHASE_ATTR_ALLOWLIST
        ).kept
        kept_phases.append({**kept, "data": data})
        phases.append(
            ParsedPhase(
                phase=i,
                max_level=as_int(kept.get("maxLevel")),
                max_hp=as_int(data.get("maxHp")),
                atk=as_int(data.get("atk")),
                def_=as_int(data.get("def")),
                res=as_int(data.get("magicResistance")),
                redeploy_time=as_int(data.get("respawnTime")),
                cost=as_int(data.get("cost")),
                block_count=as_int(data.get("blockCnt")),
                attack_interval=as_float(data.get("baseAttackTime")),
                range_id=as_str(kept.get("rangeId")),
            )
        )
    return phases, kept_phases


def _parse_skill_links(raw_skills: Any) -> tuple[list[ParsedSkillLink], list[dict[str, Any]]]:
    links: list[ParsedSkillLink] = []
    kept_links: list[dict[str, Any]] = []
    for i, raw in enumerate(raw_skills if isinstance(raw_skills, list) else []):
        if not isinstance(raw, dict):
            continue
        kept = apply_allowlist(raw, SKILL_LINK_ALLOWLIST).kept
        skill_id = as_str(kept.get("skillId"))
        if not skill_id:
            continue
        kept_links.append(kept)
        unlock = _as_dict(kept.get("unlockCond"))
        links.append(
            ParsedSkillLink(
                skill_game_id=skill_id,
                slot_index=i + 1,  # 1-based skill slot
                unlock_phase=_suffix_int(unlock.get("phase"), "PHASE_"),
                unlock_level=as_int(unlock.get("level")),
            )
        )
    return links, kept_links


def _parse_talents(raw_talents: Any) -> tuple[list[ParsedTalent], list[dict[str, Any]]]:
    talents: list[ParsedTalent] = []
    kept_talents: list[dict[str, Any]] = []
    for ti, raw in enumerate(raw_talents if isinstance(raw_talents, list) else []):
        if not isinstance(raw, dict):
            continue
        candidates = raw.get("candidates")
        display_name: str | None = None
        variants: list[ParsedTalentVariant] = []
        kept_cands: list[dict[str, Any]] = []
        for vi, cand in enumerate(candidates if isinstance(candidates, list) else []):
            if not isinstance(cand, dict):
                continue
            kept = apply_allowlist(cand, TALENT_CANDIDATE_ALLOWLIST).kept
            blackboard = _allowlist_blackboard(cand.get("blackboard"))
            kept_cands.append({**kept, "blackboard": blackboard})
            if display_name is None:
                display_name = as_str(kept.get("name"))
            cond = _as_dict(kept.get("unlockCondition"))
            variants.append(
                ParsedTalentVariant(
                    variant_index=vi,
                    unlock_phase=_suffix_int(cond.get("phase"), "PHASE_"),
                    unlock_level=as_int(cond.get("level")),
                    potential_rank=as_int(kept.get("requiredPotentialRank")),
                    blackboard=blackboard,
                )
            )
        talents.append(ParsedTalent(talent_index=ti, display_name=display_name, variants=variants))
        kept_talents.append({"talent_index": ti, "candidates": kept_cands})
    return talents, kept_talents


def _operator_aliases(name: str | None, appellation: str | None) -> list[ParsedAlias]:
    aliases: list[ParsedAlias] = []
    if name:
        aliases.append(ParsedAlias(name, "name", name.casefold()))
    if appellation and appellation != name:
        aliases.append(ParsedAlias(appellation, "appellation", appellation.casefold()))
    return aliases


def parse_operators(character_raw: Any) -> list[ParsedOperator]:
    """Transform raw ``character_table`` (id-keyed dict) into typed operators.

    Summon tokens + map traps (``profession`` in ``TOKEN``/``TRAP``) are skipped.
    """
    if not isinstance(character_raw, dict):
        raise ImporterError("character table is not a JSON object")
    parsed: list[ParsedOperator] = []
    for game_id in sorted(character_raw):
        entry = character_raw[game_id]
        if not isinstance(entry, dict) or not isinstance(game_id, str):
            continue
        kept = apply_allowlist(entry, CHARACTER_ALLOWLIST).kept
        profession = as_str(kept.get("profession"))
        if profession in _NON_OPERATOR_PROFESSIONS:
            continue
        name = as_str(kept.get("name"))
        appellation = as_str(kept.get("appellation"))
        tags = [t for t in kept.get("tagList", []) if isinstance(t, str)]
        phases, kept_phases = _parse_phases(entry.get("phases"))
        skill_links, kept_links = _parse_skill_links(entry.get("skills"))
        talents, kept_talents = _parse_talents(entry.get("talents"))
        parsed.append(
            ParsedOperator(
                game_id=game_id,
                display_name=name,
                rarity=_suffix_int(kept.get("rarity"), "TIER_"),
                profession=profession,
                subclass_id=as_str(kept.get("subProfessionId")),
                position=as_str(kept.get("position")),
                tags=tags,
                obtainable=not bool(entry.get("isNotObtainable")),
                aliases=_operator_aliases(name, appellation),
                phases=phases,
                skill_links=skill_links,
                talents=talents,
                provenance_record={
                    "character": kept,
                    "phases": kept_phases,
                    "skills": kept_links,
                    "talents": kept_talents,
                },
            )
        )
    return parsed


def insert_operators(
    conn: sqlite3.Connection,
    parsed: list[ParsedOperator],
    *,
    skill_pk_by_game_id: dict[str, int],
    server: str,
    snapshot_id: str,
    character_source_path: str,
    skills_inserted: int = 0,
) -> OperatorImportResult:
    """Insert operators + aliases + phases + skill links + talents (§V17/§V33)."""
    counts = {"operators": 0, "phases": 0, "talents": 0, "links": 0, "aliases": 0}
    for op in parsed:
        provenance_id = insert_record_provenance(
            conn,
            snapshot_id=snapshot_id,
            source_path=character_source_path,
            source_record_key=op.game_id,
            record=op.provenance_record,
        )
        # A duplicate character id (UNIQUE(server, game_id)) or a repeated phase /
        # slot / talent index collides on a UNIQUE/PK constraint; translate to a
        # typed ImporterError instead of tearing down the build (§V33 / §V3).
        with integrity_guard(
            f"operator {op.game_id!r} collides on a UNIQUE/PK constraint (dup id or index)",
            ImporterError,
        ):
            cur = conn.execute(
                "INSERT INTO operators "
                "(server, game_id, display_name, rarity, profession, subclass_id, position, "
                "tag_json, obtainable, provenance_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    server,
                    op.game_id,
                    op.display_name,
                    op.rarity,
                    op.profession,
                    op.subclass_id,
                    op.position,
                    json_or_none(op.tags) if op.tags else None,
                    int(op.obtainable),
                    provenance_id,
                ),
            )
            operator_pk = int(cur.lastrowid or 0)
            counts["operators"] += 1
            counts["aliases"] += _insert_aliases(conn, operator_pk, op.aliases)
            counts["phases"] += _insert_phases(conn, operator_pk, op.phases)
            counts["links"] += _insert_skill_links(
                conn, operator_pk, op.game_id, op.skill_links, skill_pk_by_game_id
            )
            counts["talents"] += _insert_talents(conn, operator_pk, op.talents)
    return OperatorImportResult(
        operators_inserted=counts["operators"],
        skills_inserted=skills_inserted,
        phases_inserted=counts["phases"],
        talents_inserted=counts["talents"],
        skill_links_inserted=counts["links"],
        aliases_inserted=counts["aliases"],
    )


def _insert_aliases(conn: sqlite3.Connection, operator_pk: int, aliases: list[ParsedAlias]) -> int:
    for alias in aliases:
        conn.execute(
            "INSERT INTO operator_aliases "
            "(operator_pk, alias, language, normalized_alias, alias_type) VALUES (?, ?, ?, ?, ?)",
            (operator_pk, alias.alias, None, alias.normalized_alias, alias.alias_type),
        )
    return len(aliases)


def _insert_phases(conn: sqlite3.Connection, operator_pk: int, phases: list[ParsedPhase]) -> int:
    for ph in phases:
        conn.execute(
            "INSERT INTO operator_phases "
            "(operator_pk, phase, max_level, max_hp, atk, def, res, redeploy_time, cost, "
            "block_count, attack_interval, range_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operator_pk,
                ph.phase,
                ph.max_level,
                ph.max_hp,
                ph.atk,
                ph.def_,
                ph.res,
                ph.redeploy_time,
                ph.cost,
                ph.block_count,
                ph.attack_interval,
                ph.range_id,
            ),
        )
    return len(phases)


def _insert_skill_links(
    conn: sqlite3.Connection,
    operator_pk: int,
    game_id: str,
    links: list[ParsedSkillLink],
    skill_pk_by_game_id: dict[str, int],
) -> int:
    inserted = 0
    for link in links:
        skill_pk = skill_pk_by_game_id.get(link.skill_game_id)
        if skill_pk is None:
            # Operator names a skill absent from skill_table: skip the link rather
            # than violate the operator_skills.skill_pk FK (§21.2 unresolved ref).
            _LOG.warning(
                "operator %s references skill %r absent from skill_table; skipping link",
                game_id,
                link.skill_game_id,
            )
            continue
        conn.execute(
            "INSERT INTO operator_skills "
            "(operator_pk, skill_pk, slot_index, unlock_phase, unlock_level) "
            "VALUES (?, ?, ?, ?, ?)",
            (operator_pk, skill_pk, link.slot_index, link.unlock_phase, link.unlock_level),
        )
        inserted += 1
    return inserted


def _insert_talents(conn: sqlite3.Connection, operator_pk: int, talents: list[ParsedTalent]) -> int:
    for talent in talents:
        cur = conn.execute(
            "INSERT INTO talents (operator_pk, talent_index, display_name) VALUES (?, ?, ?)",
            (operator_pk, talent.talent_index, talent.display_name),
        )
        talent_pk = int(cur.lastrowid or 0)
        for variant in talent.variants:
            conn.execute(
                "INSERT INTO talent_levels "
                "(talent_pk, variant_index, unlock_phase, unlock_level, potential_rank, "
                "condition_json, blackboard_json, gameplay_description) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    talent_pk,
                    variant.variant_index,
                    variant.unlock_phase,
                    variant.unlock_level,
                    variant.potential_rank,
                    None,  # condition captured by the typed phase/level/rank columns
                    json_or_none(variant.blackboard),
                    None,  # prose excluded by default (§V16)
                ),
            )
    return len(talents)


def import_operators(
    conn: sqlite3.Connection,
    adapter: SourceAdapter,
    snapshot_id: str,
    *,
    character_table_path: str = "gamedata/excel/character_table.json",
    skill_table_path: str = "gamedata/excel/skill_table.json",
) -> OperatorImportResult:
    """Read character + skill tables via the adapter and import them.

    A snapshot without ``character_table.json`` (e.g. a combat-only fixture) yields
    an empty result rather than failing, so the operator domain is optional per
    snapshot. Skills import first so operator→skill links resolve to a real
    ``skill_pk`` (FK).
    """
    if not adapter.exists(character_table_path):
        return OperatorImportResult()
    character_raw = adapter.read_json(character_table_path)
    skill_raw = adapter.read_json(skill_table_path) if adapter.exists(skill_table_path) else {}
    parsed_skills = parse_skills(skill_raw)
    skill_pk_by_game_id = insert_skills(
        conn,
        parsed_skills,
        server=adapter.server,
        snapshot_id=snapshot_id,
        skill_source_path=skill_table_path,
    )
    parsed_operators = parse_operators(character_raw)
    return insert_operators(
        conn,
        parsed_operators,
        skill_pk_by_game_id=skill_pk_by_game_id,
        server=adapter.server,
        snapshot_id=snapshot_id,
        character_source_path=character_table_path,
        skills_inserted=len(parsed_skills),
    )
