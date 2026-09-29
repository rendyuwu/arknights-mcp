"""Base (RIIC) skills, factions, and the collab flag (ADR 0021): shaping + ``find_operators``.

The entry shapers are shared by ``get_operator``, ``find_operators``, and
``get_my_roster`` so the three surfaces emit one shape. The server returns facts --
the skills, their unlock gates, and which stage is in effect for the account -- and
builds no combos or rankings.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from arknights_mcp.db.repositories.operators import BaseSkillRow, FactionRow, OperatorRepository
from arknights_mcp.models.common import PAGE_SIZE_DEFAULT
from arknights_mcp.models.operators import FIND_FILTER_REQUIRED
from arknights_mcp.services.operators import operator_identity
from arknights_mcp.services.stages import (
    SectionPage,
    StageProvenance,
    _section_page,
    _validate_page,
)

BASE_SKILL_SLOT_NOTE = (
    "base_skills entries that share a slot are stages of one skill: once an entry's "
    "unlock_elite and unlock_level are reached it replaces the earlier entry in that slot, "
    "so one entry per slot is in effect at a time."
)
FIND_EMPTY_LIMITATION = (
    "No operator in this build matches these filters. Loosen or drop one to widen the list."
)
#: Emitted only when the build predates migration 0021 -- the one case where a sync is the
#: right advice; a plain empty match on a current build gets FIND_EMPTY_LIMITATION instead.
BASE_SKILL_DOMAIN_MISSING_LIMITATION = (
    "This build was synced before base skills, factions, and the collab flag were "
    "imported, so it carries none of them; ask the server admin to run "
    "`arknights-mcp sync` to add them."
)
COLLAB_LIMITATION = (
    "collab marks the operators the game data flags as collaboration operators. The data "
    "does not name the collaboration itself; team factions such as rainbow name some of them."
)


def faction_entries(rows: Sequence[FactionRow]) -> list[dict[str, object]]:
    """``[{faction_id, display_name?, main?}]``; ``main`` only on a main faction."""
    out: list[dict[str, object]] = []
    for r in rows:
        entry: dict[str, object] = {"faction_id": r.faction_id}
        if r.display_name is not None:
            entry["display_name"] = r.display_name
        if r.is_main:
            entry["main"] = True
        out.append(entry)
    return out


def base_skill_entries(
    rows: Sequence[BaseSkillRow],
    *,
    room_type: str | None = None,
    progress: tuple[int, int] | None = None,
) -> list[dict[str, object]]:
    """Shape base-skill stages, optionally narrowed to a room and joined with progress.

    With ``progress=(elite, level)`` each slot keeps its in-effect stage (the reached
    stage with the highest ``stage_index``) and every stage not reached yet; a reached
    stage a later one replaced is dropped, so ``in_effect: false`` always means locked.
    ``room_type`` keeps that room's stages and drops the per-entry ``room_type`` key.
    """
    in_effect: dict[int, int] = {}
    if progress is not None:
        for r in rows:
            if progress >= (r.unlock_phase, r.unlock_level):
                in_effect[r.slot_index] = max(in_effect.get(r.slot_index, 0), r.stage_index)
    out: list[dict[str, object]] = []
    for r in rows:
        active = in_effect.get(r.slot_index) == r.stage_index
        if progress is not None and not active and progress >= (r.unlock_phase, r.unlock_level):
            continue
        if room_type is not None and r.room_type != room_type:
            continue
        entry: dict[str, object] = {"slot": r.slot_index}
        if r.display_name is not None:
            entry["display_name"] = r.display_name
        if room_type is None:
            entry["room_type"] = r.room_type
        if r.description is not None:
            entry["description"] = r.description
        entry["unlock_elite"] = r.unlock_phase
        entry["unlock_level"] = r.unlock_level
        if progress is not None:
            entry["in_effect"] = active
        out.append(entry)
    return out


FindOperatorsStatus = Literal["ok", "unknown_faction"]


@dataclass(frozen=True)
class FindOperatorsResult:
    """``find_operators`` outcome; ``data`` is the wire payload (``page`` excluded)."""

    status: FindOperatorsStatus
    server: str
    data: dict[str, object]
    page: SectionPage | None
    provenance: tuple[StageProvenance, ...]
    limitations: tuple[str, ...]


def find_operators(
    conn: sqlite3.Connection,
    *,
    server: str,
    room_type: str | None = None,
    faction: str | None = None,
    collab: bool | None = None,
    page: int = 1,
    page_size: int = PAGE_SIZE_DEFAULT,
) -> FindOperatorsResult:
    """Obtainable operators matching the filters, one bounded page, rarity-first.

    At least ``room_type`` or ``faction``, or ``collab=True``, is required (the model
    enforces the same rule): ``collab=False`` alone would page every operator.
    """
    p, size = _validate_page(page, page_size)
    if room_type is None and faction is None and collab is not True:
        raise ValueError(FIND_FILTER_REQUIRED)
    repo = OperatorRepository(conn)
    faction_id: str | None = None
    if faction is not None:
        faction_id = repo.resolve_faction(server, faction)
        if faction_id is None:
            return FindOperatorsResult("unknown_faction", server, {}, None, (), ())

    matching = repo.filter_operators(
        server, room_type=room_type, faction_id=faction_id, collab=collab
    )
    window = matching[(p - 1) * size : p * size]
    pks = [op.operator_pk for op in window]
    factions = repo.factions(pks)
    flags = repo.collab_flags(pks)
    skills = repo.base_skills(pks) if room_type is not None else {}

    rows: list[dict[str, object]] = []
    for op in window:
        row = operator_identity(op.game_id, op)
        row["factions"] = faction_entries(factions.get(op.operator_pk, []))
        if op.operator_pk in flags:
            row["collab"] = flags[op.operator_pk]
        if room_type is not None:
            row["base_skills"] = base_skill_entries(
                skills.get(op.operator_pk, []), room_type=room_type
            )
        rows.append(row)

    # Distinct (snapshot_id, imported_at) over the FULL match set, first-seen order.
    provenance = tuple(
        StageProvenance(snapshot_id, imported_at)
        for snapshot_id, imported_at in dict.fromkeys(
            (op.snapshot_id, op.imported_at) for op in matching
        )
    )

    limitations: list[str] = []
    if not matching:
        limitations.append(
            FIND_EMPTY_LIMITATION
            if repo.has_base_skill_domain()
            else BASE_SKILL_DOMAIN_MISSING_LIMITATION
        )
    if any(row.get("base_skills") for row in rows):
        limitations.append(BASE_SKILL_SLOT_NOTE)
    if collab is not None:
        limitations.append(COLLAB_LIMITATION)

    data: dict[str, object] = {"server": server}
    if room_type is not None:
        data["room_type"] = room_type
    if faction_id is not None:
        data["faction_id"] = faction_id
    data["operators"] = rows
    return FindOperatorsResult(
        status="ok",
        server=server,
        data=data,
        page=_section_page(p, size, len(matching)),
        provenance=provenance,
        limitations=tuple(limitations),
    )
