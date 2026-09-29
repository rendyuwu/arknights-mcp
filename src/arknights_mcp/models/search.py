"""Bounded input model for ``search_entities``.

Mirrors the domain entry point in
:func:`arknights_mcp.services.search.search_entities`. The bounds here are the
enforcement point: ``limit`` is rejected outside ``[1, SEARCH_MAX_LIMIT]``
so no request can widen the search window into a bulk dump.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from arknights_mcp.models.common import (
    MAX_QUERY_LEN,
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
    Region,
    StrictModel,
)

#: Entity domains searchable via the shared FTS index (item added so
#: get_item_drops has a real name->id path).
EntityType = Literal["operator", "enemy", "stage", "item"]


class SearchEntitiesInput(StrictModel):
    """Parameters for ``search_entities``.

    ``query`` is free text, length-capped. ``server`` optionally scopes to
    one region; ``entity_type`` narrows the domain. ``limit`` is bounded to
    the window (default 10, max 50) -- an out-of-range value is rejected.

    The extra-locale (ja/ko) NAME-alias filter is RETIRED (founder
    2026-07-23, EN+CN only): there is no ``locale`` parameter, and ``extra="forbid"``
    rejects one if a client still sends it.
    """

    query: str = Field(min_length=1, max_length=MAX_QUERY_LEN)
    server: Region | None = None
    entity_type: EntityType | None = None
    limit: int = Field(default=SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT)
