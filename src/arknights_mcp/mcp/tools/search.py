"""FTS-backed search MCP tools: ``search_entities`` (§T32) + ``search_stages``
(§T33) (§V19/§V23; §I.tool).

Each bridges a bounded input model (§T30 -- the §V19 ``limit`` gate) to a shared
domain service (§T31) and wraps the outcome in the typed
:class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope` (§T29 -- one §V23 status per
result). Both transports dispatch these exact specs via the single registry
(§V14): a tool owns no query logic of its own, only the model -> service ->
envelope mapping, and the two share one guarded-run + locator-shaping path
(:func:`_guarded_search`, §V37). ``search_stages`` differs only in that an exact
``stage_code`` match is ranked first (§T33), enforced in the service.

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

from arknights_mcp.mcp.envelopes import ResponseEnvelope, error, ok
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._shared import ConnectionProvider, run_guarded
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.search import SearchEntitiesInput
from arknights_mcp.models.stages import SearchStagesInput
from arknights_mcp.services.search import SearchHit, SearchResult, search_entities, search_stages

#: The service call a search tool runs once it has a connection: it takes the
#: read-only connection and returns the domain :class:`SearchResult`. The
#: bound-model parsing happens in the tool handler *before* this runs (§V18/§V19).
SearchRunner = Callable[[sqlite3.Connection], SearchResult]

_ENTITIES_TOOL_NAME = "search_entities"
_ENTITIES_TOOL_TITLE = "Search entities"
_ENTITIES_TOOL_DESCRIPTION = (
    "Search indexed Arknights operators, enemies, stages, and items by name, alias, "
    "stage code, game id, or tag. Returns ranked, region-tagged locators; use "
    "get_operator / get_enemy / get_stage for full facts, or feed an item locator's "
    "game_id to get_item_drops. Only a stage locator carries stage_code and a "
    "difficulty variant tag (NORMAL, FOUR_STAR challenge, TOUGH, or EASY); other "
    "locators omit both keys. The difficulty tag lets a normal stage and its "
    "challenge, tough, or easy variant that share a code and name stay "
    "distinguishable. For a stage code like 4-4, prefer "
    "search_stages, which ranks an exact stage-code match first. "
    "Results are bounded (default 10, max 50) and en/cn are never mixed."
)
_STAGES_TOOL_NAME = "search_stages"
_STAGES_TOOL_TITLE = "Search stages"
_STAGES_TOOL_DESCRIPTION = (
    "Search indexed Arknights stages by stage code (e.g. 4-4), name, or game id. "
    "An exact stage-code match is ranked first. Returns ranked, region-tagged "
    "locators; use get_stage for full facts + map/spawns. Each locator carries a "
    "difficulty variant tag (NORMAL, FOUR_STAR challenge, TOUGH, or EASY), so a "
    "normal stage and its challenge, tough, or easy variant that share a code and "
    "name stay distinguishable. Results are bounded "
    "(default 10, max 50) and en/cn are never mixed."
)

#: Fixed, safe copy for the typed ``not_found`` envelopes (§V23 -- no query echo,
#: no stack trace, no local path). The shared DB-unavailable/internal fail-closed
#: copy + guard live in ``_shared.run_guarded`` (one failure mode, one home §V37).
_ENTITIES_NOT_FOUND_MESSAGE = "no indexed entity matched the search query"
_ENTITIES_NOT_FOUND_ACTION = (
    "broaden the query, drop the server/entity_type filter, or check the spelling"
)
_STAGES_NOT_FOUND_MESSAGE = "no indexed stage matched the search query"
_STAGES_NOT_FOUND_ACTION = "broaden the query, drop the server filter, or check the stage code"

#: Fixed, safe copy for the §V50 region-availability verdicts, shared by both
#: search tools (§V37): a region is gated *before* absence is asserted, so a client
#: never reads a bare ``not_found`` for an empty region index (B42). No query echo,
#: no path; the suggested action is an admin step, never a query-time download (§V24).
_UNSUPPORTED_SERVER_MESSAGE = "the requested region is not supported"
_UNSUPPORTED_SERVER_ACTION = "use a supported region: en or cn"
_DATA_STALE_MESSAGE = "no active snapshot for the requested region in the active build"
_DATA_STALE_ACTION = (
    "ask the server admin to run `arknights-mcp sync --server <region>` or `arknights-mcp import`"
)


def _hit_to_dict(hit: SearchHit) -> dict[str, object]:
    """One hit as a region-tagged locator (§V5: region travels on every row).

    ``stage_code`` + ``difficulty`` are STAGE-domain fields: an operator / enemy /
    item locator carries neither, so both keys are OMITTED (not emitted as an
    ambiguous ``null``) on a non-stage row (§V67 null discipline / B90; omitting an
    always-absent field is additive-safe, §V21). A stage locator always keys both --
    ``stage_code`` is present, and ``difficulty`` is kept even when ``null`` (a plain
    stage with no variant), because for a stage the tag is a domain-expected field a
    client reads unconditionally, not an ambiguous absence. ``difficulty`` is the
    §V70/§V80 stage variant tag: two stages that share a ``display_name`` +
    ``stage_code`` (a normal stage and its challenge / tough / easy variant) carry
    distinct difficulty values, so a client can tell them apart in one result set
    without parsing the game-data ``game_id`` suffix/prefix (B59/B84).
    """
    out: dict[str, object] = {
        "entity_type": hit.entity_type,
        "server": hit.server,
        "game_id": hit.game_id,
        "display_name": hit.display_name,
    }
    # §V67/B90: the stage-only locator fields ride only a stage row. A non-stage
    # locator omits both keys instead of emitting a bare null; a stage keeps both
    # (difficulty may be null there, a domain-expected tag, not an absence).
    if hit.entity_type == "stage":
        out["stage_code"] = hit.stage_code
        out["difficulty"] = hit.difficulty
    return out


def _guarded_search(
    get_conn: ConnectionProvider,
    run: SearchRunner,
    *,
    not_found_message: str,
    not_found_action: str,
) -> ResponseEnvelope:
    """Run a search service call and map it to a typed §V23 envelope.

    Shared by ``search_entities`` and ``search_stages``: the only per-tool
    variation is the runner (which service + params) and the ``not_found`` copy.
    The connection acquisition + fail-closed error handling is delegated to the
    shared :func:`run_guarded` (§V37); here we own only the search-specific
    ``ok`` locator shaping and the ``not_found`` mapping.
    """

    def shape(result: SearchResult) -> ResponseEnvelope:
        # §V50/§V24: the service gates region availability before asserting
        # absence, so an unsupported region or an empty region index surfaces as a
        # typed region verdict -- never a bare ``not_found`` that would wrongly
        # claim the entity is absent from a region that simply has no data (B42).
        if result.status == "unsupported_server":
            return error(
                "unsupported_server",
                _UNSUPPORTED_SERVER_MESSAGE,
                suggested_action=_UNSUPPORTED_SERVER_ACTION,
            )
        if result.status == "data_stale":
            return error("data_stale", _DATA_STALE_MESSAGE, suggested_action=_DATA_STALE_ACTION)
        if result.status == "not_found":
            return error("not_found", not_found_message, suggested_action=not_found_action)
        return ok(
            {
                "query": result.query,
                "count": len(result.hits),
                "results": [_hit_to_dict(hit) for hit in result.hits],
            }
        )

    return run_guarded(get_conn, run, shape)


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
        return _guarded_search(
            get_conn,
            lambda conn: search_entities(
                conn,
                query=parsed.query,
                server=parsed.server,
                entity_type=parsed.entity_type,
                limit=parsed.limit,
            ),
            not_found_message=_ENTITIES_NOT_FOUND_MESSAGE,
            not_found_action=_ENTITIES_NOT_FOUND_ACTION,
        )

    return ToolSpec(
        name=_ENTITIES_TOOL_NAME,
        title=_ENTITIES_TOOL_TITLE,
        description=_ENTITIES_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(SearchEntitiesInput),
    )


def build_search_stages_spec(get_conn: ConnectionProvider) -> ToolSpec:
    """Build the ``search_stages`` :class:`ToolSpec` (§T33; §V19/§I.tool).

    Stage-scoped sibling of ``search_entities``: an exact ``stage_code`` match is
    ranked first (the service enforces the ordering). Same §V18/§V19 model gate
    (out-of-range ``limit`` / over-length query / unknown parameter *rejected*
    before any query runs) and same fail-closed §V23 envelope path (§V14). The
    spec is read-only (§V2) for the single shared registry both transports use.
    """

    def handler(**params: object) -> ResponseEnvelope:
        parsed = SearchStagesInput.model_validate(params)
        return _guarded_search(
            get_conn,
            lambda conn: search_stages(
                conn,
                query=parsed.query,
                server=parsed.server,
                limit=parsed.limit,
            ),
            not_found_message=_STAGES_NOT_FOUND_MESSAGE,
            not_found_action=_STAGES_NOT_FOUND_ACTION,
        )

    return ToolSpec(
        name=_STAGES_TOOL_NAME,
        title=_STAGES_TOOL_TITLE,
        description=_STAGES_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(SearchStagesInput),
    )
