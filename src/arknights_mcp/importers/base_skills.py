"""Base (RIIC) skill importer: building_data.json -> base_skills + operator_base_skills.

Parses the primary ``building_data.json`` (the SAME ``arknights_assets_gamedata``
snapshot as enemy/stage/operator -- NOT a new source; ADR 0021):

* ``buffs.<buffId>`` -> one ``base_skills`` row: id, name, facility ``roomType``, and
  the in-game mechanic effect text (read raw through ``template_text``, tags stripped,
  like the ADR 0010 templates). Icons, colors, sort ids, and targets are dropped by
  the allowlist.
* ``chars.<charId>.buffChar[]`` -> the operator's slots; each slot's ``buffData[]``
  stages carry an unlock gate ``{phase: "PHASE_n", level}``. ``slot_index`` and
  ``stage_index`` are 1-based positions; a later stage replaces the earlier one in its
  slot once unlocked.

Must run AFTER operators: char ids resolve to a present ``operator_pk`` and a char
without one is skipped (counted). The domain is optional: an absent file imports
nothing, and the tables are outside CRITICAL_TABLES.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.field_policy import (
    BASE_SKILL_ALLOWLIST,
    BASE_SKILL_STAGE_ALLOWLIST,
    apply_allowlist,
)
from arknights_mcp.importers.guards import guard_not_silently_empty
from arknights_mcp.importers.manifest import insert_record_provenance
from arknights_mcp.importers.operators import operator_pk_by_game_id
from arknights_mcp.sources.base import SourceAdapter
from arknights_mcp.util.coerce import as_dict, as_int, as_str, suffix_int
from arknights_mcp.util.sqlite import integrity_guard
from arknights_mcp.util.text import template_text

_LOG = logging.getLogger(__name__)

BUILDING_DATA_PATH = "gamedata/excel/building_data.json"


@dataclass(frozen=True)
class ParsedBaseSkill:
    buff_id: str
    display_name: str | None
    room_type: str
    description: str | None
    provenance_record: dict[str, Any]


@dataclass(frozen=True)
class ParsedBaseSkillStage:
    slot_index: int
    stage_index: int
    buff_id: str
    unlock_phase: int
    unlock_level: int


@dataclass(frozen=True)
class ParsedOperatorBaseSkills:
    char_id: str
    stages: tuple[ParsedBaseSkillStage, ...]
    provenance_record: dict[str, Any]


@dataclass(frozen=True)
class BaseSkillImportResult:
    base_skills_inserted: int = 0
    operator_links_inserted: int = 0
    operators_resolved: int = 0


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def parse_base_skills(
    building_raw: Any,
) -> tuple[list[ParsedBaseSkill], list[ParsedOperatorBaseSkills]]:
    """Transform raw ``building_data`` into typed base skills + per-char stages.

    Key-sorted iteration keeps provenance ids deterministic. A buff without an id or
    room, or a stage without an id, phase, or level, is skipped rather than guessed.
    """
    if not isinstance(building_raw, dict):
        raise ImporterError("building_data is not a JSON object")
    buffs = building_raw.get("buffs")
    chars = building_raw.get("chars")
    if not isinstance(buffs, dict) or not isinstance(chars, dict):
        raise ImporterError("building_data carries no buffs/chars objects")

    skills: list[ParsedBaseSkill] = []
    for key in sorted(buffs, key=str):
        entry = buffs[key]
        if not isinstance(entry, dict):
            continue
        kept = apply_allowlist(entry, BASE_SKILL_ALLOWLIST).kept
        buff_id = as_str(kept.get("buffId"))
        room_type = as_str(kept.get("roomType"))
        if not buff_id or not room_type:
            continue
        skills.append(
            ParsedBaseSkill(
                buff_id=buff_id,
                display_name=as_str(kept.get("buffName")) or None,
                room_type=room_type,
                description=template_text(entry.get("description")),
                provenance_record=kept,
            )
        )

    operators: list[ParsedOperatorBaseSkills] = []
    for char_id in sorted(chars, key=str):
        if not isinstance(char_id, str):
            continue
        stages: list[ParsedBaseSkillStage] = []
        kept_slots: list[list[dict[str, Any]]] = []
        for si, slot in enumerate(_as_list(as_dict(chars[char_id]).get("buffChar")), 1):
            kept_stages: list[dict[str, Any]] = []
            for sj, raw in enumerate(_as_list(as_dict(slot).get("buffData")), 1):
                kept = apply_allowlist(as_dict(raw), BASE_SKILL_STAGE_ALLOWLIST).kept
                cond = as_dict(kept.get("cond"))
                buff_id = as_str(kept.get("buffId"))
                phase = suffix_int(cond.get("phase"), "PHASE_")
                level = as_int(cond.get("level"))
                if not buff_id or phase is None or level is None:
                    continue
                kept_stages.append(kept)
                stages.append(ParsedBaseSkillStage(si, sj, buff_id, phase, level))
            kept_slots.append(kept_stages)
        if stages:
            operators.append(
                ParsedOperatorBaseSkills(
                    char_id=char_id,
                    stages=tuple(stages),
                    provenance_record={"charId": char_id, "buffChar": kept_slots},
                )
            )
    return skills, operators


def insert_base_skills(
    conn: sqlite3.Connection,
    skills: list[ParsedBaseSkill],
    operators: list[ParsedOperatorBaseSkills],
    *,
    server: str,
    snapshot_id: str,
    source_path: str,
) -> BaseSkillImportResult:
    """Insert base skills, then each present operator's slot stages.

    A duplicate ``buffId`` or a repeated slot/stage collides on a constraint and maps
    to a typed :class:`ImporterError`. A char with no operator row, or a stage naming
    an unknown buff, is skipped and counted.
    """
    pk_by_buff: dict[str, int] = {}
    for skill in skills:
        provenance_id = insert_record_provenance(
            conn,
            snapshot_id=snapshot_id,
            source_path=source_path,
            source_record_key=skill.buff_id,
            record=skill.provenance_record,
        )
        with integrity_guard(
            f"base skill {skill.buff_id!r} collides on UNIQUE(server, buff_id)", ImporterError
        ):
            cur = conn.execute(
                "INSERT INTO base_skills "
                "(server, buff_id, display_name, room_type, description, provenance_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    server,
                    skill.buff_id,
                    skill.display_name,
                    skill.room_type,
                    skill.description,
                    provenance_id,
                ),
            )
        pk_by_buff[skill.buff_id] = int(cur.lastrowid or 0)

    op_map = operator_pk_by_game_id(conn, server)
    resolved = links = skipped_chars = unknown_buffs = 0
    for op in operators:
        operator_pk = op_map.get(op.char_id)
        if operator_pk is None:
            skipped_chars += 1
            continue
        resolved += 1
        provenance_id = insert_record_provenance(
            conn,
            snapshot_id=snapshot_id,
            source_path=source_path,
            source_record_key=op.char_id,
            record=op.provenance_record,
        )
        for stage in op.stages:
            base_skill_pk = pk_by_buff.get(stage.buff_id)
            if base_skill_pk is None:
                unknown_buffs += 1
                continue
            with integrity_guard(
                f"operator {op.char_id!r} base-skill slot {stage.slot_index} stage "
                f"{stage.stage_index} collides on its primary key",
                ImporterError,
            ):
                conn.execute(
                    "INSERT INTO operator_base_skills "
                    "(operator_pk, slot_index, stage_index, base_skill_pk, unlock_phase, "
                    "unlock_level, provenance_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        operator_pk,
                        stage.slot_index,
                        stage.stage_index,
                        base_skill_pk,
                        stage.unlock_phase,
                        stage.unlock_level,
                        provenance_id,
                    ),
                )
            links += 1
    guard_not_silently_empty(
        candidates=resolved,
        produced=links,
        scope=server,
        source="building_data",
        unit="operator(s) with base skills",
        resolution="linked to a base skill",
        outcome="empty base-skill build",
    )
    if skipped_chars or unknown_buffs:
        _LOG.warning(
            "%s: base skills: %d char(s) without an operator row and %d stage(s) naming "
            "an unknown buff skipped",
            server,
            skipped_chars,
            unknown_buffs,
        )
    return BaseSkillImportResult(
        base_skills_inserted=len(pk_by_buff),
        operator_links_inserted=links,
        operators_resolved=resolved,
    )


def import_base_skills(
    conn: sqlite3.Connection,
    adapter: SourceAdapter,
    snapshot_id: str,
    *,
    building_data_path: str = BUILDING_DATA_PATH,
) -> BaseSkillImportResult:
    """Read ``building_data.json`` via the adapter and import the base-skill domain.

    An absent file yields an empty result (optional per snapshot). A non-empty
    ``buffs`` object that parses to zero base skills fails closed.
    """
    if not adapter.exists(building_data_path):
        return BaseSkillImportResult()
    building_raw = adapter.read_json(building_data_path)
    skills, operators = parse_base_skills(building_raw)
    guard_not_silently_empty(
        candidates=sum(1 for v in building_raw["buffs"].values() if isinstance(v, dict)),
        produced=len(skills),
        scope=adapter.server,
        source="building_data",
        unit="base skill entr(y|ies)",
        resolution="parsed to a base skill",
        outcome="empty base-skill build",
    )
    return insert_base_skills(
        conn,
        skills,
        operators,
        server=adapter.server,
        snapshot_id=snapshot_id,
        source_path=building_data_path,
    )
