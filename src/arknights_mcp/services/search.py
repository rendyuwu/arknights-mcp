"""Entity search service (§T31): the single domain entry point both transports
call to search operators / enemies / stages by name, alias, code, id, or tag
(§V14).

Given a read-only SQLite connection and a free-text query, it tokenizes the
query into a safe FTS5 ``MATCH`` expression (§V2/§V18 -- no operator or SQL
injection), runs it through :class:`~arknights_mcp.db.repositories.search.SearchRepository`
(the sole parameterized SQL surface), and returns ranked, region-tagged hits
(§V5). Result size is bounded to the §V19 window (default 10, max 50). It never
opens the connection or mutates the database; both transports share this exact
function (§V14).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Literal, get_args

from arknights_mcp.db.repositories.metadata import MetadataRepository
from arknights_mcp.db.repositories.search import SearchHitRow, SearchRepository
from arknights_mcp.models.common import SEARCH_DEFAULT_LIMIT, SEARCH_MAX_LIMIT, Region

#: §V19 search-result bounds. Single home is ``models.common`` (§V37); re-exported
#: under the service-local names the rest of this module already uses.
DEFAULT_LIMIT = SEARCH_DEFAULT_LIMIT
MAX_LIMIT = SEARCH_MAX_LIMIT
#: Cap tokens so a pathological query cannot build an unbounded MATCH expression.
_MAX_TOKENS = 16
#: Word runs only: stripping every FTS/SQL metacharacter at the tokenizer means the
#: rebuilt MATCH expression can carry no operator, quote, or ``*`` from user input.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

#: Typed outcome (a subset of the §V23 status vocabulary wired into the tool
#: envelope in §T32). ``not_found`` == the region index is present but the
#: well-formed query matched nothing; ``unsupported_server`` / ``data_stale`` are
#: the §V50 region-availability verdicts returned *before* asserting absence.
SearchStatus = Literal["ok", "not_found", "unsupported_server", "data_stale"]

#: §V5 supported regions as a runtime set, derived from the single ``Region``
#: literal home (§V37) so the search gate and the input model never diverge.
_REGIONS: frozenset[str] = frozenset(get_args(Region))


@dataclass(frozen=True)
class SearchHit:
    """One ranked search hit, carrying its region (§V5).

    Full facts + provenance are fetched by the entity tools (``get_enemy`` /
    ``get_stage`` / ``get_operator``); a hit is a region-scoped locator.
    """

    entity_type: str
    server: str
    game_id: str
    display_name: str | None
    stage_code: str | None


@dataclass(frozen=True)
class SearchResult:
    """Domain result of the search service: the echoed query + ranked hits."""

    status: SearchStatus
    query: str
    hits: tuple[SearchHit, ...]


def _match_expression(query: str) -> str | None:
    """Build a safe FTS5 prefix-MATCH expression from free text, or ``None``.

    Each word token becomes a quoted prefix term (``"tok"*``); quoting escapes the
    FTS syntax so no ``MATCH`` operator (``AND``/``OR``/``NEAR``/``*``/``"``/``:``)
    survives from the untrusted query (§V2/§V18). Returns ``None`` when the query
    holds no word characters (nothing to search).
    """
    tokens = _TOKEN_RE.findall(query)
    if not tokens:
        return None
    return " ".join(f'"{tok}"*' for tok in tokens[:_MAX_TOKENS])


def _validate_limit(limit: int) -> int:
    """Reject a ``limit`` outside the §V19 window -- never silently widen it.

    Mirrors :class:`~arknights_mcp.models.search.SearchEntitiesInput`
    (``ge=1, le=SEARCH_MAX_LIMIT``): the model is the MCP gate, but a caller
    reaching this service directly (or a future transport that skips model
    validation) must get the *same* rejection, not a silent clamp -- one §V19
    contract, enforced identically in both places.
    """
    value = int(limit)
    if value < 1 or value > MAX_LIMIT:
        raise ValueError(f"limit {value} outside the §V19 window [1, {MAX_LIMIT}]")
    return value


def _region_gate(conn: sqlite3.Connection, server: str | None) -> SearchStatus | None:
    """Honor region availability (§V24/§V50) before a search asserts absence.

    Returns the gating :data:`SearchStatus` to short-circuit with, or ``None`` when
    the region index is present and the search may proceed:

    * a ``server`` outside {en, cn} -> ``unsupported_server`` (§V5);
    * a supported ``server`` with no active snapshot -> ``data_stale`` + a suggested
      admin action at the tool layer;
    * an unscoped search (``server`` is ``None``) against a build with *no* active
      snapshot at all -> ``data_stale`` (the whole index is empty).

    This is the fix for B42: a bare ``not_found`` claims the entity is absent, which
    is not inferable when the region index is empty. Both ``search_entities`` and
    ``search_stages`` route through this single home (§V37); it mirrors the
    snapshot-presence verdict the status service (B24) makes over the same table.
    """
    if server is not None and server not in _REGIONS:
        return "unsupported_server"
    available = MetadataRepository(conn).active_servers()
    if server is not None:
        if server not in available:
            return "data_stale"
    elif not available:
        return "data_stale"
    return None


def _result_from_rows(query: str, rows: list[SearchHitRow]) -> SearchResult:
    """Map repository rows to region-tagged hits + a typed status (§V5/§V23).

    Single home (§V37) for the row -> :class:`SearchHit` shaping shared by
    :func:`search_entities` and :func:`search_stages`; ``not_found`` == the query
    was well-formed but the FTS match returned nothing.
    """
    hits = tuple(
        SearchHit(
            entity_type=row.entity_type,
            server=row.server,
            game_id=row.game_id,
            display_name=row.name,
            stage_code=row.stage_code,
        )
        for row in rows
    )
    return SearchResult(status="ok" if hits else "not_found", query=query, hits=hits)


def search_entities(
    conn: sqlite3.Connection,
    *,
    query: str,
    server: str | None = None,
    entity_type: str | None = None,
    locale: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> SearchResult:
    """Search indexed entities for ``query``. Read-only; parameterized SQL only (§V2).

    ``server`` scopes the result to one region (§V5, never silently mixed);
    ``entity_type`` narrows to ``operator`` | ``enemy`` | ``stage``. ``limit`` is
    validated against the §V19 window -- an out-of-range value is *rejected*
    (``ValueError``), never silently widened. Region availability is honored
    *before* asserting absence (§V24/§V50): an unsupported region or a region with
    no active snapshot returns ``unsupported_server`` / ``data_stale``, never a bare
    ``not_found`` (see :func:`_region_gate`). Both transports call this (§V14).

    ``locale`` (§V57) filters to entities carrying a name/alias in that locale
    (``en``/``zh``/``ja``/``ko``); it is a NAME-tag axis, NOT a fact region. The
    region gate runs *independently* of ``locale`` -- a jp/kr locale search over a
    region with no snapshot is still ``data_stale``, so a locale match never widens
    region availability (§V50/§V57). ``None`` = no locale filter.
    """
    bounded = _validate_limit(limit)
    gate = _region_gate(conn, server)
    if gate is not None:
        return SearchResult(status=gate, query=query, hits=())
    match = _match_expression(query)
    if match is None:
        return SearchResult(status="not_found", query=query, hits=())

    repo = SearchRepository(conn)
    rows = repo.search(match, server=server, entity_type=entity_type, locale=locale, limit=bounded)
    return _result_from_rows(query, rows)


def search_stages(
    conn: sqlite3.Connection,
    *,
    query: str,
    server: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> SearchResult:
    """Search indexed stages for ``query`` -- exact ``stage_code`` first (§T33).

    Same safe, tokenized FTS path as :func:`search_entities` (§V2/§V18), scoped to
    the ``stage`` domain, but a stage whose ``stage_code`` equals the query (e.g.
    ``4-4``, case-insensitive) is ranked ahead of a fuzzier name/game-id hit.
    ``server`` scopes to one region (§V5, never silently mixed); ``limit`` is
    validated against the §V19 window -- an out-of-range value is *rejected*
    (``ValueError``), never silently widened. Region availability is honored before
    asserting absence (§V24/§V50, see :func:`_region_gate`). Both transports call
    this (§V14).
    """
    bounded = _validate_limit(limit)
    gate = _region_gate(conn, server)
    if gate is not None:
        return SearchResult(status=gate, query=query, hits=())
    match = _match_expression(query)
    if match is None:
        return SearchResult(status="not_found", query=query, hits=())

    repo = SearchRepository(conn)
    rows = repo.search_stages(match, exact_code=query, server=server, limit=bounded)
    return _result_from_rows(query, rows)
