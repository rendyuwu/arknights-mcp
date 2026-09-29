"""Operator / talent importer (PRD section 12.3).

Parses the real ``character_table.json`` shape (a top-level id-keyed dict, no
wrapper) into the normalized operator domain: ``operators`` + ``operator_aliases``
+ ``operator_phases`` + ``operator_skills`` + ``talents`` + ``talent_levels``, and
drives :mod:`~arknights_mcp.importers.skills` for the ``skill_table`` half so one
call still imports the whole domain (the split is the size cap's, not the pipeline's).

``uniequip_table.json`` is read here too, for one field: ``subProfDict`` supplies
each operator's subclass DISPLAY NAME (it shipped as a bare id before). The file
was already fetched for the module importer, so the pairing costs no new source --
it was fetched and never read.

Applies the explicit field allowlist and string sanitization and
attaches per-record provenance to each core row (operators + skills);
sub-tables link through their parent. The skill-level + talent-candidate effect
description TEMPLATE (mechanic text that references the blackboard keys) is
imported into the ``gameplay_description`` columns and emitted alongside the
blackboard for grounding (ADR 0010). Lore/story prose -- the
operator's own ``description``, ``itemUsage``, ``itemDesc`` -- is never allowlisted
and is excluded (ADR 0010). Pure parsing
(:func:`parse_operators` / :func:`parse_skills`) is separated from insertion so it
is unit-testable without a database.

Nested numeric blocks (``spData``, phase attribute ``data``) and ``blackboard``
parameter lists are each re-allowlisted from the raw source rather than stored
whole, so no unallowlisted dict/list leaf reaches a ``*_json`` column.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.field_policy import (
    CHARACTER_ALLOWLIST,
    PHASE_ALLOWLIST,
    PHASE_ATTR_ALLOWLIST,
    REGION_TO_NAME_LOCALE,
    SKILL_LINK_ALLOWLIST,
    SUBPROF_ALLOWLIST,
    TALENT_CANDIDATE_ALLOWLIST,
    allowlist_blackboard,
    apply_allowlist,
)
from arknights_mcp.importers.manifest import insert_record_provenance
from arknights_mcp.importers.skills import insert_skills, parse_skills
from arknights_mcp.sources.base import SourceAdapter
from arknights_mcp.util.coerce import (
    as_dict,
    as_float,
    as_int,
    as_str,
    json_or_none,
    suffix_int,
)
from arknights_mcp.util.sqlite import integrity_guard
from arknights_mcp.util.text import template_text

_LOG = logging.getLogger(__name__)

#: character_table professions that are not player operators (summon tokens,
#: map traps); excluded so search + operator intel cover operators only.
_NON_OPERATOR_PROFESSIONS: frozenset[str] = frozenset({"TOKEN", "TRAP"})


# --- parsed shapes -----------------------------------------------------------


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
    #: In-game talent effect description TEMPLATE (mechanic text referencing the
    #: blackboard keys; ADR 0010). Allowlisted + sanitized + capped
    #: at parse time; ``None`` when the candidate carries no description.
    description: str | None


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
    subclass_name: str | None
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


def operator_pk_by_game_id(conn: sqlite3.Connection, server: str) -> dict[str, int]:
    """``{operator game_id: operator_pk}`` for ``server`` (the single home).

    Shared by the module importer (``modules.operator_pk`` FK resolution) and the banner
    importer (featured-op soft-resolve): both need to map a source char id to
    an internal ``operator_pk`` for the same server, so the lookup lives in exactly one
    place rather than being copy-pasted per consumer.
    """
    return {
        str(game_id): int(operator_pk)
        for game_id, operator_pk in conn.execute(
            "SELECT game_id, operator_pk FROM operators WHERE server = ?", (server,)
        )
    }


# --- skills ------------------------------------------------------------------


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
        unlock = as_dict(kept.get("unlockCond"))
        links.append(
            ParsedSkillLink(
                skill_game_id=skill_id,
                slot_index=i + 1,  # 1-based skill slot
                unlock_phase=suffix_int(unlock.get("phase"), "PHASE_"),
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
            blackboard = allowlist_blackboard(cand.get("blackboard"))
            kept_cands.append({**kept, "blackboard": blackboard})
            if display_name is None:
                display_name = as_str(kept.get("name"))
            cond = as_dict(kept.get("unlockCondition"))
            variants.append(
                ParsedTalentVariant(
                    variant_index=vi,
                    unlock_phase=suffix_int(cond.get("phase"), "PHASE_"),
                    unlock_level=as_int(cond.get("level")),
                    potential_rank=as_int(kept.get("requiredPotentialRank")),
                    blackboard=blackboard,
                    # ADR 0010: as for skill levels above -- read the template
                    # from the RAW candidate so the tag strip precedes the cap.
                    # `description` is on TALENT_CANDIDATE_ALLOWLIST.
                    description=template_text(cand.get("description")),
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


def parse_subclass_names(uniequip_raw: Any) -> dict[str, str]:
    """``uniequip_table.subProfDict`` → ``{subProfessionId: display name}``.

    The name for the ``subProfessionId`` every operator row already stores lives in the
    module table, keyed by that same id: ``{"corecaster": {"subProfessionId":
    "corecaster", "subProfessionName": "Core Caster", ...}}``. Verified against the
    pinned upstream for both regions; CN ships the Chinese label under the same key, so
    the map is region-scoped exactly like every other display string.

    Returns an empty map for an absent/foreign shape, which leaves ``subclass_name``
    NULL rather than guessing -- the limitation arm. An entry with no id or no
    name is skipped for the same reason; the id is read from the entry's own
    ``subProfessionId`` rather than the dict key so a mismatch cannot invent a pairing.
    """
    sub_dict = uniequip_raw.get("subProfDict") if isinstance(uniequip_raw, dict) else None
    if not isinstance(sub_dict, dict):
        return {}
    names: dict[str, str] = {}
    for entry in sub_dict.values():
        if not isinstance(entry, dict):
            continue
        kept = apply_allowlist(entry, SUBPROF_ALLOWLIST).kept
        subclass_id = as_str(kept.get("subProfessionId"))
        display_name = as_str(kept.get("subProfessionName"), sanitize=True)
        if subclass_id and display_name:
            names[subclass_id] = display_name
    return names


def parse_operators(
    character_raw: Any, subclass_names: dict[str, str] | None = None
) -> list[ParsedOperator]:
    """Transform raw ``character_table`` (id-keyed dict) into typed operators.

    Summon tokens + map traps (``profession`` in ``TOKEN``/``TRAP``) are skipped.
    ``subclass_names`` pairs each ``subProfessionId`` with its display name;
    an id the map does not cover keeps a NULL name rather than a fabricated one, which a
    snapshot without ``uniequip_table.json`` makes the norm rather than the exception.
    """
    if not isinstance(character_raw, dict):
        raise ImporterError("character table is not a JSON object")
    names = subclass_names or {}
    parsed: list[ParsedOperator] = []
    for game_id in sorted(character_raw):
        entry = character_raw[game_id]
        if not isinstance(entry, dict) or not isinstance(game_id, str):
            continue
        kept = apply_allowlist(entry, CHARACTER_ALLOWLIST).kept
        profession = as_str(kept.get("profession"))
        if profession in _NON_OPERATOR_PROFESSIONS:
            continue
        subclass_id = as_str(kept.get("subProfessionId"))
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
                rarity=suffix_int(kept.get("rarity"), "TIER_"),
                profession=profession,
                subclass_id=subclass_id,
                subclass_name=names.get(subclass_id) if subclass_id else None,
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
    """Insert operators + aliases + phases + skill links + talents."""
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
        # typed ImporterError instead of tearing down the build.
        with integrity_guard(
            f"operator {op.game_id!r} collides on a UNIQUE/PK constraint (dup id or index)",
            ImporterError,
        ):
            cur = conn.execute(
                "INSERT INTO operators "
                "(server, game_id, display_name, rarity, profession, subclass_id, "
                "subclass_name, position, tag_json, obtainable, provenance_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    server,
                    op.game_id,
                    op.display_name,
                    op.rarity,
                    op.profession,
                    op.subclass_id,
                    op.subclass_name,
                    op.position,
                    json_or_none(op.tags) if op.tags else None,
                    int(op.obtainable),
                    provenance_id,
                ),
            )
            operator_pk = int(cur.lastrowid or 0)
            counts["operators"] += 1
            counts["aliases"] += _insert_aliases(conn, operator_pk, op.aliases, server)
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


def _insert_aliases(
    conn: sqlite3.Connection, operator_pk: int, aliases: list[ParsedAlias], server: str
) -> int:
    # Stamp each alias with its language-locale tag. An en/cn operator's
    # canonical name+appellation are in that region's language, so the locale is
    # derived from the fact region via the shared REGION_TO_NAME_LOCALE map (en->en,
    # cn->zh; single home). This is the real fresh-build "backfill": migration
    # 0011's UPDATE runs against an empty candidate, so the importer is the guarantor
    # that new alias rows carry a locale. The tag is NOT a fact region -- the operator
    # still returns its own region facts. An unmapped region falls back to the
    # raw server string rather than NULL, so the column is always populated.
    locale = REGION_TO_NAME_LOCALE.get(server, server)
    for alias in aliases:
        conn.execute(
            "INSERT INTO operator_aliases "
            "(operator_pk, alias, language, normalized_alias, alias_type, locale) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (operator_pk, alias.alias, None, alias.normalized_alias, alias.alias_type, locale),
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
            # than violate the operator_skills.skill_pk FK (section 21.2 unresolved ref).
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
                    variant.description,  # effect template (ADR 0010)
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
    uniequip_table_path: str = "gamedata/excel/uniequip_table.json",
) -> OperatorImportResult:
    """Read character + skill tables via the adapter and import them.

    A snapshot without ``character_table.json`` (e.g. a combat-only fixture) yields
    an empty result rather than failing, so the operator domain is optional per
    snapshot. Skills import first so operator→skill links resolve to a real
    ``skill_pk`` (FK).

    ``uniequip_table.json`` is read here for its ``subProfDict`` alone -- the display
    name of each operator's subclass. It is read tolerantly for the same
    reason the skill table is: a combat-only snapshot has neither, and its absence
    leaves ``subclass_name`` NULL rather than failing the domain. The file is already
    in the sync's supplementary set for the module importer, so pairing the name
    costs no new source; the module importer reads its own keys from the same file.
    """
    if not adapter.exists(character_table_path):
        return OperatorImportResult()
    character_raw = adapter.read_json(character_table_path)
    skill_raw = adapter.read_json(skill_table_path) if adapter.exists(skill_table_path) else {}
    uniequip_raw = (
        adapter.read_json(uniequip_table_path) if adapter.exists(uniequip_table_path) else {}
    )
    parsed_skills = parse_skills(skill_raw)
    skill_pk_by_game_id = insert_skills(
        conn,
        parsed_skills,
        server=adapter.server,
        snapshot_id=snapshot_id,
        skill_source_path=skill_table_path,
    )
    parsed_operators = parse_operators(character_raw, parse_subclass_names(uniequip_raw))
    return insert_operators(
        conn,
        parsed_operators,
        skill_pk_by_game_id=skill_pk_by_game_id,
        server=adapter.server,
        snapshot_id=snapshot_id,
        character_source_path=character_table_path,
        skills_inserted=len(parsed_skills),
    )
