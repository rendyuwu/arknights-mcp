"""``search_entities`` MCP tool (§T32; §V19/§V23; §I.tool).

Bridges the bounded :class:`~arknights_mcp.models.search.SearchEntitiesInput`
(§T30 -- the §V19 ``limit`` gate) to the shared
:func:`~arknights_mcp.services.search.search_entities` domain service (§T31) and
wraps the outcome in the typed :class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope`
(§T29 -- one §V23 status per result). Both transports dispatch this exact spec via
the single registry (§V14): the tool owns no query logic of its own, only the
model -> service -> envelope mapping.

Two invariants are load-bearing here:

* **§V19** -- the search window is bounded on one contract enforced twice: the
  input model rejects an out-of-range ``limit`` (or an over-length query / bad
  region / unknown parameter) *before* the handler runs, and the service rejects
  it again. A malformed request is *rejected* (the ``ValidationError`` propagates
  as a protocol-level error), never silently widened into a bulk dump.
* **§V23** -- every delivered result is a typed-status envelope
  (``ok``/``not_found``); a database failure or any unexpected error fails closed
  to a fixed, path/trace-free envelope (``database_unavailable``/``internal_error``),
  never a leaked exception.

Search hits are region-tagged *locators* (the ``server`` field on each row keeps
en/cn from mixing, §V5); a client fetches full facts + provenance through the
``get_operator`` / ``get_enemy`` / ``get_stage`` tools.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from arknights_mcp.db.connection import DatabaseUnavailable
from arknights_mcp.mcp.envelopes import ResponseEnvelope, error, internal_error, ok
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.search import SearchEntitiesInput
from arknights_mcp.services.search import SearchHit, SearchResult, search_entities

#: Supplies the process-wide read-only connection to the promoted build. The
#: app/transport layer owns the connection's lifecycle (opened once, reused); the
#: handler only reads through it and never opens or closes it.
ConnectionProvider = Callable[[], sqlite3.Connection]

_TOOL_NAME = "search_entities"
_TOOL_TITLE = "Search entities"
_TOOL_DESCRIPTION = (
    "Search indexed Arknights operators, enemies, and stages by name, alias, "
    "stage code, game id, or tag. Returns ranked, region-tagged locators; use "
    "get_operator / get_enemy / get_stage for full facts. Results are bounded "
    "(default 10, max 50) and en/cn are never mixed."
)

#: Fixed, safe copy for the typed error envelopes (§V23 -- no query echo, no
#: stack trace, no local path).
_NOT_FOUND_MESSAGE = "no indexed entity matched the search query"
_NOT_FOUND_ACTION = "broaden the query, drop the server/entity_type filter, or check the spelling"
_DB_UNAVAILABLE_MESSAGE = "the active database is unavailable"
_DB_UNAVAILABLE_ACTION = "run `arknights-mcp status` to check the active build"


def _hit_to_dict(hit: SearchHit) -> dict[str, object]:
    """One hit as a region-tagged locator (§V5: region travels on every row)."""
    return {
        "entity_type": hit.entity_type,
        "server": hit.server,
        "game_id": hit.game_id,
        "display_name": hit.display_name,
        "stage_code": hit.stage_code,
    }


def _to_envelope(result: SearchResult) -> ResponseEnvelope:
    """Map the domain :class:`SearchResult` to a typed §V23 envelope."""
    if result.status == "not_found":
        return error("not_found", _NOT_FOUND_MESSAGE, suggested_action=_NOT_FOUND_ACTION)
    return ok(
        {
            "query": result.query,
            "count": len(result.hits),
            "results": [_hit_to_dict(hit) for hit in result.hits],
        }
    )


def build_search_entities_spec(get_conn: ConnectionProvider) -> ToolSpec:
    """Build the ``search_entities`` :class:`ToolSpec` (§T32; §V14).

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build. The returned spec is read-only (§V2) and is meant to register into the
    single shared registry both transports dispatch from (§V14); its
    ``input_schema`` is the bounded model's JSON Schema, so the §V19 ``limit``
    bound + §V18 caps land on the wire exactly as validated.
    """

    def handler(**params: object) -> ResponseEnvelope:
        # §V18/§V19 gate: the bounded model rejects an out-of-range limit, an
        # over-length query, a bad region, or an unknown parameter *before* any
        # query runs. A ValidationError here propagates as a protocol-level
        # rejection -- never a silently widened search (§V19).
        parsed = SearchEntitiesInput.model_validate(params)
        try:
            conn = get_conn()
            result = search_entities(
                conn,
                query=parsed.query,
                server=parsed.server,
                entity_type=parsed.entity_type,
                limit=parsed.limit,
            )
        except DatabaseUnavailable:
            return error(
                "database_unavailable",
                _DB_UNAVAILABLE_MESSAGE,
                suggested_action=_DB_UNAVAILABLE_ACTION,
            )
        except Exception:
            # §V23 fail-closed: no exception text, stack trace, or local path
            # reaches the client -- that detail belongs in the redacted log.
            return internal_error()
        return _to_envelope(result)

    return ToolSpec(
        name=_TOOL_NAME,
        title=_TOOL_TITLE,
        description=_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(SearchEntitiesInput),
    )
