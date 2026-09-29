"""Entity search service: the single domain entry point both transports
call to search operators / enemies / stages / items by name, alias, code, id, or
tag. The item domain gives ``get_item_drops`` a name->id path.

Given a read-only SQLite connection and a free-text query, it tokenizes the
query into a safe FTS5 ``MATCH`` expression that can carry no operator or SQL
injection, then runs it through :class:`~arknights_mcp.db.repositories.search.SearchRepository`
(the sole parameterized SQL surface), and returns ranked, region-tagged hits.
Result size is bounded to a fixed window (default 10, max 50). It never
opens the connection or mutates the database; both transports share this exact
function.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Literal, get_args

from arknights_mcp.db.repositories.metadata import MetadataRepository
from arknights_mcp.db.repositories.search import SearchHitRow, SearchRepository
from arknights_mcp.models.common import SEARCH_DEFAULT_LIMIT, SEARCH_MAX_LIMIT, Region
from arknights_mcp.services.stage_variant import stage_variant

#: Search-result bounds. Single home is ``models.common``; re-exported
#: under the service-local names the rest of this module already uses.
DEFAULT_LIMIT = SEARCH_DEFAULT_LIMIT
MAX_LIMIT = SEARCH_MAX_LIMIT
#: Cap tokens so a pathological query cannot build an unbounded MATCH expression.
_MAX_TOKENS = 16
#: Word runs only: stripping every FTS/SQL metacharacter at the tokenizer means the
#: rebuilt MATCH expression can carry no operator, quote, or ``*`` from user input.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

#: Typed outcome (a subset of the status vocabulary wired into the tool
#: envelope). ``unsupported_server`` / ``data_stale`` are the
#: region-availability verdicts returned *before* absence is asserted at all.
#:
#: There is no ``not_found`` here: a search is a SET QUERY, and a
#: well-formed query that matched nothing is an ``ok`` result with an empty ``hits``
#: plus a limitation carrying the why -- an empty answer to a well-formed question is
#: not an error, and a client branching on ``status`` must not read "no name matched
#: 'Amyia'" as a failure while reading "no announcements in this window" as a success.
#: The gates above are REAL errors and still fire first.
#:
#: The extra-locale (ja/ko) axis and its ``locale_unavailable`` /
#: ``locale_not_applicable`` verdicts are RETIRED (founder 2026-07-23,
#: EN+CN only).
SearchStatus = Literal[
    "ok",
    "unsupported_server",
    "data_stale",
]

#: Why an ``ok`` search came back with no hits. ``None`` on a non-empty
#: result. The tool turns this into the client-facing limitation, so the DOMAIN records
#: the reason and the wire wording stays in one place at the tool layer.
#: ``no_match`` == the query tokenized fine and the FTS index simply had nothing;
#: ``no_searchable_tokens`` == tokenization stripped the query to nothing (a query of
#: punctuation alone), which is a different why and must not be reported as the first.
EmptyReason = Literal["no_match", "no_searchable_tokens"]

#: Supported regions as a runtime set, derived from the single ``Region``
#: literal home so the search gate and the input model never diverge.
_REGIONS: frozenset[str] = frozenset(get_args(Region))


@dataclass(frozen=True)
class SearchHit:
    """One ranked search hit, carrying its region.

    Full facts + provenance are fetched by the entity tools (``get_enemy`` /
    ``get_stage`` / ``get_operator``); a hit is a region-scoped locator.

    ``difficulty`` is the stage variant tag: a stage hit carries the same
    truthful variant ``get_stage`` returns, so two stages sharing a
    ``display_name`` + ``stage_code`` stay distinguishable in one result set without
    the game-data ``game_id`` suffix/prefix. It is derived through the one
    home (:func:`~arknights_mcp.services.stage_variant.stage_variant`): the
    source ``FOUR_STAR`` challenge variant (``#f#``) plus the prefix-derived
    ``TOUGH`` / ``EASY`` (``tough_*`` / ``easy_*``, never left ``NORMAL``). ``None``
    for a non-stage hit or a plain stage with no variant.

    ``zone_display_name`` is the stage's zone name and ``event_name`` the
    title of the event that zone belongs to. A stage can match a query
    through either (an event name finds that event's stages), and without them
    on the wire such a hit is unattributable: the client sees a stage whose own name and
    code have nothing to do with the query and cannot partition mixed hits by event.
    They are two different facts from two different source files -- the zone name is the
    sub-zone subtitle ("The Coming of The Future"), the event name the title a client
    would actually type ("Lone Trail") -- so neither substitutes for the other.
    ``None`` for a non-stage hit, for a stage whose zone is unnamed in source, and (for
    ``event_name``) for a zone belonging to no event.
    """

    entity_type: str
    server: str
    game_id: str
    display_name: str | None
    stage_code: str | None
    difficulty: str | None
    zone_display_name: str | None
    event_name: str | None


@dataclass(frozen=True)
class SearchResult:
    """Domain result of the search service: the echoed query + ranked hits."""

    status: SearchStatus
    query: str
    hits: tuple[SearchHit, ...]
    #: Why an ``ok`` result carries no hits, so the tool can say which of the
    #: two empty cases it is. Always ``None`` when ``hits`` is non-empty, and on a
    #: gate verdict (those carry their own typed status + copy).
    empty_reason: EmptyReason | None = None


def _match_expression(query: str) -> str | None:
    """Build a safe FTS5 prefix-MATCH expression from free text, or ``None``.

    Each word token becomes a quoted prefix term (``"tok"*``); quoting escapes the
    FTS syntax so no ``MATCH`` operator (``AND``/``OR``/``NEAR``/``*``/``"``/``:``)
    survives from the untrusted query. Returns ``None`` when the query
    holds no word characters (nothing to search).
    """
    tokens = _TOKEN_RE.findall(query)
    if not tokens:
        return None
    return " ".join(f'"{tok}"*' for tok in tokens[:_MAX_TOKENS])


def _validate_limit(limit: int) -> int:
    """Reject a ``limit`` outside the window -- never silently widen it.

    Mirrors :class:`~arknights_mcp.models.search.SearchEntitiesInput`
    (``ge=1, le=SEARCH_MAX_LIMIT``): the model is the MCP gate, but a caller
    reaching this service directly (or a future transport that skips model
    validation) must get the *same* rejection, not a silent clamp -- one
    contract, enforced identically in both places.
    """
    value = int(limit)
    if value < 1 or value > MAX_LIMIT:
        raise ValueError(f"limit {value} outside the window [1, {MAX_LIMIT}]")
    return value


def _region_gate(conn: sqlite3.Connection, server: str | None) -> SearchStatus | None:
    """Honor region availability before a search asserts absence.

    Returns the gating :data:`SearchStatus` to short-circuit with, or ``None`` when
    the region index is present and the search may proceed:

    * a ``server`` outside {en, cn} -> ``unsupported_server``;
    * a supported ``server`` with no active snapshot -> ``data_stale`` + a suggested
      admin action at the tool layer;
    * an unscoped search (``server`` is ``None``) against a build with *no* active
      snapshot at all -> ``data_stale`` (the whole index is empty).

    A bare ``not_found`` claims the entity is absent, which
    is not inferable when the region index is empty. Both ``search_entities`` and
    ``search_stages`` route through this single home; it mirrors the
    snapshot-presence verdict the status service makes over the same table.
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
    """Map repository rows to region-tagged hits + a typed status.

    Single home for the row -> :class:`SearchHit` shaping shared by
    :func:`search_entities` and :func:`search_stages`. A well-formed query that matched
    nothing is ``ok`` with empty ``hits`` and ``empty_reason="no_match"`` --
    the set query succeeded and returned an empty set, which is not an error.
    """
    hits = tuple(
        SearchHit(
            entity_type=row.entity_type,
            server=row.server,
            game_id=row.game_id,
            display_name=row.name,
            stage_code=row.stage_code,
            # The same truthful variant tag get_stage emits, through the
            # one home -- a ``tough_*`` / ``easy_*`` locator is never NORMAL.
            difficulty=stage_variant(row.game_id, row.difficulty),
            # The zone name and the event title a stage may
            # have matched through, so an alias-driven hit is attributable on the wire.
            zone_display_name=row.zone_display_name,
            event_name=row.event_name,
        )
        for row in rows
    )
    return SearchResult(
        status="ok",
        query=query,
        hits=hits,
        empty_reason=None if hits else "no_match",
    )


def search_entities(
    conn: sqlite3.Connection,
    *,
    query: str,
    server: str | None = None,
    entity_type: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> SearchResult:
    """Search indexed entities for ``query``. Read-only; parameterized SQL only.

    ``server`` scopes the result to one region (never silently mixed);
    ``entity_type`` narrows to ``operator`` | ``enemy`` | ``stage`` | ``item``.
    An ``item`` locator's ``game_id`` feeds ``get_item_drops``. ``limit`` is
    validated against the window -- an out-of-range value is *rejected*
    (``ValueError``), never silently widened. Region availability is honored
    *before* asserting absence: an unsupported region or a region with
    no active snapshot returns ``unsupported_server`` / ``data_stale``, never a bare
    ``not_found`` (see :func:`_region_gate`). Both transports call this.

    The extra-locale (ja/ko) NAME-alias filter is RETIRED (founder
    2026-07-23, EN+CN only): there is no ``locale`` parameter, and the alias tables
    are no longer consulted at query time (operator self-aliases still feed the FTS
    ``name`` document at build time).
    """
    bounded = _validate_limit(limit)
    gate = _region_gate(conn, server)
    if gate is not None:
        return SearchResult(status=gate, query=query, hits=())
    match = _match_expression(query)
    if match is None:
        # The query survived the model gate but tokenized to nothing (all
        # punctuation), so there is no MATCH expression to run. Still a set query with an
        # empty answer -- ``ok`` with its OWN reason, never conflated with a real miss.
        return SearchResult(status="ok", query=query, hits=(), empty_reason="no_searchable_tokens")

    repo = SearchRepository(conn)
    rows = repo.search(match, server=server, entity_type=entity_type, limit=bounded)
    return _result_from_rows(query, rows)


def search_stages(
    conn: sqlite3.Connection,
    *,
    query: str,
    server: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> SearchResult:
    """Search indexed stages for ``query`` -- exact ``stage_code`` first.

    Same safe, tokenized FTS path as :func:`search_entities`, scoped to
    the ``stage`` domain, but a stage whose ``stage_code`` equals the query (e.g.
    ``4-4``, case-insensitive) is ranked ahead of a fuzzier name/game-id hit.
    ``server`` scopes to one region (never silently mixed); ``limit`` is
    validated against the window -- an out-of-range value is *rejected*
    (``ValueError``), never silently widened. Region availability is honored before
    asserting absence (see :func:`_region_gate`). Both transports call
    this.
    """
    bounded = _validate_limit(limit)
    gate = _region_gate(conn, server)
    if gate is not None:
        return SearchResult(status=gate, query=query, hits=())
    match = _match_expression(query)
    if match is None:
        # The query survived the model gate but tokenized to nothing (all
        # punctuation), so there is no MATCH expression to run. Still a set query with an
        # empty answer -- ``ok`` with its OWN reason, never conflated with a real miss.
        return SearchResult(status="ok", query=query, hits=(), empty_reason="no_searchable_tokens")

    repo = SearchRepository(conn)
    rows = repo.search_stages(match, exact_code=query, server=server, limit=bounded)
    return _result_from_rows(query, rows)
