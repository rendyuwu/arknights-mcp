"""``find_operators`` MCP tool (ADR 0021): operators by facility, faction, or collab.

Model -> shared service (:func:`arknights_mcp.services.base_skills.find_operators`) ->
envelope. Read-only; one bounded page per call, and a narrowing filter is required.
"""

from __future__ import annotations

import sqlite3

from arknights_mcp.mcp.envelopes import ResponseEnvelope, error
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._enum_legend import TOOL_ENUM_LEGEND_FIELDS
from arknights_mcp.mcp.tools._shared import (
    FACTION_NOT_FOUND_ACTION,
    FACTION_NOT_FOUND_MESSAGE,
    ConnectionProvider,
    operator_rows_ok,
    run_guarded,
)
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.operators import FindOperatorsInput
from arknights_mcp.services.base_skills import FindOperatorsResult, find_operators

_TOOL_NAME = "find_operators"
_TOOL_TITLE = "Find operators"

_TOOL_DESCRIPTION = (
    "Takes a region (server, en or cn) plus room_type, faction, or collab true, and lists "
    "the matching operators of this build, one bounded page at a time, ordered by rarity "
    "then game_id. room_type keeps operators with a base (RIIC) skill for that facility: "
    "TRADING (Trading Post), MANUFACTURE (Factory), POWER (Power Plant), CONTROL (Control "
    "Center), DORMITORY, MEETING (Reception Room), HIRE (Office), WORKSHOP, TRAINING "
    "(Training Room); each row then carries base_skills for that facility with slot, "
    "display_name, description, unlock_elite and unlock_level. faction takes a faction id "
    "or name, such as yan, lungmen, sui, babel, rhine, abyssal or rainbow, and matches an "
    "operator's main or secondary faction. collab true keeps only collaboration operators "
    "and works on its own; collab false drops them and needs room_type or faction. Each "
    "row carries game_id, display_name, rarity (1 to 6), profession, factions, and collab. "
    "Only obtainable operators are listed. For one operator's base skills in every "
    "facility use get_operator with include_base_skills; for the synced account's own "
    "operators use get_my_roster with the same filters. en/cn are never mixed."
)


def _shape(result: FindOperatorsResult) -> ResponseEnvelope:
    if result.status == "unknown_faction":
        return error(
            "not_found", FACTION_NOT_FOUND_MESSAGE, suggested_action=FACTION_NOT_FOUND_ACTION
        )
    return operator_rows_ok(
        result.data,
        server=result.server,
        page=result.page,
        provenance=result.provenance,
        limitations=result.limitations,
        legend_fields=TOOL_ENUM_LEGEND_FIELDS[_TOOL_NAME],
    )


def build_find_operators_spec(get_conn: ConnectionProvider) -> ToolSpec:
    def handler(**params: object) -> ResponseEnvelope:
        parsed = FindOperatorsInput.model_validate(params)

        def run(conn: sqlite3.Connection) -> FindOperatorsResult:
            return find_operators(
                conn,
                server=parsed.server,
                room_type=parsed.room_type,
                faction=parsed.faction,
                collab=parsed.collab,
                page=parsed.page.page,
                page_size=parsed.page.page_size,
            )

        return run_guarded(get_conn, run, _shape)

    return ToolSpec(
        name=_TOOL_NAME,
        title=_TOOL_TITLE,
        description=_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(FindOperatorsInput),
    )
