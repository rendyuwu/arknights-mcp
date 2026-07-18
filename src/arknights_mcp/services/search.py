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
from typing import Literal

from arknights_mcp.db.repositories.search import SearchRepository

#: §V19 search-result bounds.
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
#: Cap tokens so a pathological query cannot build an unbounded MATCH expression.
_MAX_TOKENS = 16
#: Word runs only: stripping every FTS/SQL metacharacter at the tokenizer means the
#: rebuilt MATCH expression can carry no operator, quote, or ``*`` from user input.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

#: Typed outcome; ``not_found`` == the query was well-formed but matched nothing.
#: The full §V23 status vocabulary is wired into the tool envelope in §T32.
SearchStatus = Literal["ok", "not_found"]


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


def _clamp_limit(limit: int) -> int:
    """Clamp to the §V19 window: at least 1, at most :data:`MAX_LIMIT`."""
    return max(1, min(int(limit), MAX_LIMIT))


def search_entities(
    conn: sqlite3.Connection,
    *,
    query: str,
    server: str | None = None,
    entity_type: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> SearchResult:
    """Search indexed entities for ``query``. Read-only; parameterized SQL only (§V2).

    ``server`` scopes the result to one region (§V5, never silently mixed);
    ``entity_type`` narrows to ``operator`` | ``enemy`` | ``stage``. ``limit`` is
    clamped to the §V19 window. Both transports call this same function (§V14).
    """
    bounded = _clamp_limit(limit)
    match = _match_expression(query)
    if match is None:
        return SearchResult(status="not_found", query=query, hits=())

    repo = SearchRepository(conn)
    rows = repo.search(match, server=server, entity_type=entity_type, limit=bounded)
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
