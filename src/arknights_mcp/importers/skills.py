"""Skill importer (PRD section 12.3) -- the ``skill_table`` half of the operator domain.

Parses the real ``skill_table.json`` (a top-level id-keyed dict, no wrapper) into
``skills`` + ``skill_levels``, applying the explicit field allowlist and string
sanitization and attaching per-record provenance to each skill row
(level rows link through their parent). The per-level effect description TEMPLATE is
imported into ``gameplay_description`` and emitted alongside the blackboard for
grounding (ADR 0010).

Split out of :mod:`~arknights_mcp.importers.operators` -- that module had crossed the
800-line hard cap, and the skill table is a distinct responsibility group with a
distinct source file -- the same forced split as ``effect_changes`` and
``enemy_normalization``. The dependency runs one way, ``operators`` -> here,
so the operator importer keeps a single entry point per source file.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.field_policy import (
    SKILL_LEVEL_ALLOWLIST,
    SP_DATA_ALLOWLIST,
    allowlist_blackboard,
    apply_allowlist,
)
from arknights_mcp.importers.manifest import insert_record_provenance
from arknights_mcp.util.coerce import (
    as_dict,
    as_float,
    as_int,
    as_str,
    json_or_none,
    uniform_str,
)
from arknights_mcp.util.sqlite import integrity_guard
from arknights_mcp.util.text import template_text


@dataclass(frozen=True)
class ParsedSkillLevel:
    level: int
    sp_cost: int | None
    initial_sp: int | None
    duration: float | None
    range_id: str | None
    blackboard: Any
    #: In-game skill effect description TEMPLATE (mechanic text referencing the
    #: blackboard keys; ADR 0010). Allowlisted + sanitized + capped
    #: at parse time; ``None`` when the source level carries no description.
    description: str | None
    #: This level's OWN name / enum values. The source scopes all four per
    #: level; they are carried here so a level that disagrees with its siblings keeps its
    #: value instead of being overwritten by level 1's.
    display_name: str | None
    skill_type: str | None
    sp_type: str | None
    duration_type: str | None


@dataclass(frozen=True)
class ParsedSkill:
    game_id: str
    #: The value every level shares, or ``None`` when the levels disagree.
    #: ``None`` is not "absent from the source": it means the fact is per level, and the
    #: level rows carry it. Never level 1's value standing in for the rest.
    display_name: str | None
    skill_type: str | None
    sp_type: str | None
    duration_type: str | None
    levels: list[ParsedSkillLevel]
    provenance_record: dict[str, Any]


def _enum_text(value: Any) -> str | None:
    """Enum-ish field (``skillType``/``spType``/``durationType``) → text or ``None``.

    The value is read from an already-allowlisted+sanitized ``kept`` dict, so a ``str``
    needs no further cleaning; an ``int`` code is stringified so the source's numeric form
    still round-trips into a ``TEXT`` column. ``bool`` is rejected (an ``int`` subclass,
    never a real code).

    The numeric arm is NOT a legacy encoding. The pinned upstream ``413a81a3`` ships BOTH
    forms in the SAME file at the SAME pin: ``spType`` is a name on 8674 en / 9108 cn
    skill-level rows and the bare int ``8`` on 1515 en / 1745 cn, and skill
    ``sktok_mjcsdw`` carries both across its own levels. A second, independent export of
    the same game data emits that same bare ``8`` on every one of the 1352 skill ids it
    shares with the pin, with zero disagreements, so the NAME does not exist upstream
    rather than having been missed here.

    Stringifying is therefore the honest coercion (one type per key is wanted, but not at
    the price of a fabricated one): dropping the int would erase the field on 1145 rows of
    the promoted build, and mapping it to a name would invent one, which the real-value
    rule forbids. What the client gets instead is disclosure --
    ``OPEN_ENUM_LIMITATIONS['sp_type']``, a floor rather than a resolution.

    ``skillType``/``durationType`` are 100% strings at the same pin (10189 en / 10853 cn
    level rows each), so the two clean siblings are clean by DATA, not by construction:
    one upstream int would land here and reach the wire the same silent way. That is what
    ``tests/contract/test_enum_domain_coverage.py`` pins for all three columns.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, int):
        return str(value)
    return None


def parse_skills(skill_raw: Any) -> list[ParsedSkill]:
    """Transform raw ``skill_table`` (id-keyed dict) into typed, allowlisted skills.

    ``name`` / ``skillType`` / ``durationType`` / ``spData.spType`` are scoped PER LEVEL
    upstream, so each :class:`ParsedSkillLevel` keeps its own value and the skill-wide
    scalar is the one every level shares -- ``None`` when they disagree.
    Reading level 1 and calling it the skill's value discarded the others: ``sktok_mjcsdw``
    stored the unnamed ``spType`` code ``8`` from its level 1 while its level 2 sends
    ``INCREASE_WITH_TIME``, and ``sktok_sunmao`` stored "Connect" while its level 5 is
    "Engrave". The uniform case (1597 of 1598 EN skills) is unchanged: the value rides the
    skill row and every level row leaves it ``NULL``, the same hoist ``gameplay_description``
    already uses.
    """
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
            sp = apply_allowlist(as_dict(raw_level.get("spData")), SP_DATA_ALLOWLIST).kept
            blackboard = allowlist_blackboard(raw_level.get("blackboard"))
            kept_levels.append({**kept, "spData": sp, "blackboard": blackboard})
            levels.append(
                ParsedSkillLevel(
                    level=i + 1,
                    sp_cost=as_int(sp.get("spCost")),
                    initial_sp=as_int(sp.get("initSp")),
                    duration=as_float(kept.get("duration")),
                    range_id=as_str(kept.get("rangeId")),
                    blackboard=blackboard,
                    # ADR 0010: the effect template rides the blackboard as its
                    # grounding. Read from the RAW level, not `kept`: the allowlist cap
                    # lands before the tag strip and cuts the template mid-sentence.
                    # `description` is on SKILL_LEVEL_ALLOWLIST either way.
                    description=template_text(raw_level.get("description")),
                    # this level's OWN four, not level 1's.
                    display_name=as_str(kept.get("name")),
                    skill_type=_enum_text(kept.get("skillType")),
                    sp_type=_enum_text(sp.get("spType")),
                    duration_type=_enum_text(kept.get("durationType")),
                )
            )
        parsed.append(
            ParsedSkill(
                game_id=game_id,
                # a scalar the skill may claim only when every level agrees;
                # `uniform_str` returns None the moment they diverge, and the diverging
                # values stay on their level rows.
                display_name=uniform_str(lv.display_name for lv in levels),
                skill_type=uniform_str(lv.skill_type for lv in levels),
                sp_type=uniform_str(lv.sp_type for lv in levels),
                duration_type=uniform_str(lv.duration_type for lv in levels),
                levels=levels,
                provenance_record={"skill_id": game_id, "levels": kept_levels},
            )
        )
    return parsed


def _level_only(
    skill: ParsedSkill, level: ParsedSkillLevel
) -> tuple[str | None, str | None, str | None, str | None]:
    """The four per-level values to STORE on ``level``: its own, or ``NULL`` when hoisted.

    A field the skill row already carries (every level agreed) is redundant on
    each level row, so it is stored once on the skill and ``NULL`` here -- the same
    hoist ``gameplay_description`` and the module change bundles use. A field
    the skill row left ``NULL`` is either varying or absent from the source;
    in both cases this level's own value is the honest one to store.
    """
    return (
        level.display_name if skill.display_name is None else None,
        level.skill_type if skill.skill_type is None else None,
        level.sp_type if skill.sp_type is None else None,
        level.duration_type if skill.duration_type is None else None,
    )


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
                    "blackboard_json, gameplay_description, display_name, skill_type, "
                    "sp_type, duration_type) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        skill_pk,
                        level.level,
                        level.sp_cost,
                        level.initial_sp,
                        level.duration,
                        level.range_id,
                        json_or_none(level.blackboard),
                        level.description,  # effect template (ADR 0010)
                        # a value the whole skill shares rides the skill
                        # row once; a level stores its own only when the levels disagree,
                        # so NULL here reads as "see the skill row" and the uniform case
                        # costs no repeated bytes.
                        *_level_only(skill, level),
                    ),
                )
        skill_pk_by_game_id[skill.game_id] = skill_pk
    return skill_pk_by_game_id


# --- operators ---------------------------------------------------------------
