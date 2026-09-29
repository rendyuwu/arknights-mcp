"""Account roster MCP tools (ADR 0020): ``get_my_roster``, ``get_my_operator``,
``get_my_inventory``.

Read-only views of the owner's synced account. Model -> shared service -> envelope
only: the services in :mod:`arknights_mcp.services.account` read the account
database through a SELECT-only role and the promoted build read-only, and never
touch the network. Sync is CLI-only and never a tool. The services catch
account-database failures themselves, so an outage answers ``database_unavailable``
here instead of reaching :func:`run_guarded`'s ``internal_error`` path.
"""

from __future__ import annotations

import sqlite3
from functools import partial

from arknights_mcp.db.account import AccountStore
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
from arknights_mcp.models.account import (
    GetMyInventoryInput,
    GetMyOperatorInput,
    GetMyRosterInput,
)
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.services.account import (
    AccountResult,
    get_my_inventory,
    get_my_operator,
    get_my_roster,
)

ACCOUNT_UNAVAILABLE_MESSAGE = "no synced account roster is reachable from this server"
ACCOUNT_UNAVAILABLE_ACTION = (
    "ask the server admin to run `arknights-mcp account sync` on the machine that holds "
    "the account session, and to check this server's account database connection"
)
ACCOUNT_SCHEMA_MESSAGE = (
    "the synced account roster was written by a different version of this server"
)
ACCOUNT_SCHEMA_ACTION = "ask the server admin to run `arknights-mcp account sync` to rewrite it"
ACCOUNT_REGION_MESSAGE = "account sync covers the en server only"
ACCOUNT_REGION_ACTION = "call this tool again with server en"
ACCOUNT_NOT_OWNED_MESSAGE = "the synced account does not own this operator"
ACCOUNT_NOT_OWNED_ACTION = (
    "check the game_id with search_entities or get_my_roster; an operator recruited after "
    "the last sync appears after the next one"
)

_ROSTER_DESCRIPTION = (
    "Takes a region (server, en only) and lists the operators owned by the Arknights "
    "account the server admin synced to this server, one bounded page at a time. Each row "
    "carries game_id, display_name, rarity (star count 1 to 6), profession, elite "
    "(promotion 0 to 2), level, potential (1 to 6, as shown in game), skill_level (1 to "
    "7), masteries (mastery 0 to 3 per skill, in skill slot order), the unlocked modules "
    "with their level, and equipped_module_id. min_rarity and min_elite narrow the list. "
    "Rows are ordered by rarity, then elite, then level, highest first. synced_at says "
    "when the account was read; nothing newer is known. Call get_my_operator for one "
    "operator's skills, modules, and owned skins by name. room_type, faction, and collab "
    "narrow the list the same way as in find_operators; with room_type each row adds "
    "base_skills for that facility, and in_effect marks the entry of each slot the "
    "account's elite and level have unlocked."
)
_OPERATOR_DESCRIPTION = (
    "Takes an operator game_id (such as char_002_amiya, from search_entities or "
    "get_my_roster) and a region (server, en only), and returns that operator's progress "
    "on the Arknights account the server admin synced to this server. It carries elite, "
    "level, potential (1 to 6), skill_level, every skill with its mastery (0 to 3), every "
    "unlocked module with its level and whether it is equipped, and the skins the account "
    "owns for the operator. An alternate playable form, such as Amiya's Guard form, is "
    "reported on its base operator, with form_id on its skills and modules. An operator "
    "the account does not own is not_found. synced_at says when the account was read; "
    "nothing newer is known."
)
_INVENTORY_DESCRIPTION = (
    "Takes a region (server, en only) and lists the item stacks held by the Arknights "
    "account the server admin synced to this server, one bounded page at a time, ordered "
    "by item_id. Each row carries item_id, display_name when this build names the item, "
    "and count. Upgrade-cost ids from get_operator match item_id. lmd is the account's "
    "LMD balance. synced_at says when the account was read; nothing newer is known."
)


def _shape(tool: str, result: AccountResult) -> ResponseEnvelope:
    if result.status == "unsupported_server":
        return error(
            "unsupported_server", ACCOUNT_REGION_MESSAGE, suggested_action=ACCOUNT_REGION_ACTION
        )
    if result.status == "account_unavailable":
        return error(
            "database_unavailable",
            ACCOUNT_UNAVAILABLE_MESSAGE,
            suggested_action=ACCOUNT_UNAVAILABLE_ACTION,
        )
    if result.status == "schema_incompatible":
        return error(
            "schema_incompatible", ACCOUNT_SCHEMA_MESSAGE, suggested_action=ACCOUNT_SCHEMA_ACTION
        )
    if result.status == "not_found":
        return error(
            "not_found", ACCOUNT_NOT_OWNED_MESSAGE, suggested_action=ACCOUNT_NOT_OWNED_ACTION
        )
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
        legend_fields=TOOL_ENUM_LEGEND_FIELDS.get(tool, ()),
    )


def build_get_my_roster_spec(
    get_conn: ConnectionProvider, *, account_store: AccountStore | None
) -> ToolSpec:
    def handler(**params: object) -> ResponseEnvelope:
        parsed = GetMyRosterInput.model_validate(params)

        def run(conn: sqlite3.Connection) -> AccountResult:
            return get_my_roster(
                conn,
                account_store,
                server=parsed.server,
                min_rarity=parsed.min_rarity,
                min_elite=parsed.min_elite,
                room_type=parsed.room_type,
                faction=parsed.faction,
                collab=parsed.collab,
                page=parsed.page.page,
                page_size=parsed.page.page_size,
            )

        return run_guarded(get_conn, run, partial(_shape, "get_my_roster"))

    return ToolSpec(
        name="get_my_roster",
        title="Get my roster",
        description=_ROSTER_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(GetMyRosterInput),
    )


def build_get_my_operator_spec(
    get_conn: ConnectionProvider, *, account_store: AccountStore | None
) -> ToolSpec:
    def handler(**params: object) -> ResponseEnvelope:
        parsed = GetMyOperatorInput.model_validate(params)

        def run(conn: sqlite3.Connection) -> AccountResult:
            return get_my_operator(
                conn, account_store, server=parsed.server, game_id=parsed.game_id
            )

        return run_guarded(get_conn, run, partial(_shape, "get_my_operator"))

    return ToolSpec(
        name="get_my_operator",
        title="Get my operator",
        description=_OPERATOR_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(GetMyOperatorInput),
    )


def build_get_my_inventory_spec(
    get_conn: ConnectionProvider, *, account_store: AccountStore | None
) -> ToolSpec:
    def handler(**params: object) -> ResponseEnvelope:
        parsed = GetMyInventoryInput.model_validate(params)

        def run(conn: sqlite3.Connection) -> AccountResult:
            return get_my_inventory(
                conn,
                account_store,
                server=parsed.server,
                page=parsed.page.page,
                page_size=parsed.page.page_size,
            )

        return run_guarded(get_conn, run, partial(_shape, "get_my_inventory"))

    return ToolSpec(
        name="get_my_inventory",
        title="Get my inventory",
        description=_INVENTORY_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(GetMyInventoryInput),
    )
