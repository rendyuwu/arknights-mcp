"""Bounded input model for ``search_entities`` (§T30; §V19/§V22).

Mirrors the domain entry point in
:func:`arknights_mcp.services.search.search_entities`. The bounds here are the
§V19 enforcement point: ``limit`` is rejected outside ``[1, SEARCH_MAX_LIMIT]``
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

#: Entity domains searchable via the shared FTS index (§T31).
EntityType = Literal["operator", "enemy", "stage"]


class SearchEntitiesInput(StrictModel):
    """Parameters for ``search_entities`` (§I; §V19).

    ``query`` is free text, length-capped (§V18). ``server`` optionally scopes to
    one region (§V5); ``entity_type`` narrows the domain. ``limit`` is bounded to
    the §V19 window (default 10, max 50) -- an out-of-range value is rejected.
    """

    query: str = Field(min_length=1, max_length=MAX_QUERY_LEN)
    server: Region | None = None
    entity_type: EntityType | None = None
    limit: int = Field(default=SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT)
