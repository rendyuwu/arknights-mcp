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
    SearchLocale,
    StrictModel,
)

#: Entity domains searchable via the shared FTS index (§T31).
EntityType = Literal["operator", "enemy", "stage"]


class SearchEntitiesInput(StrictModel):
    """Parameters for ``search_entities`` (§I; §V19/§V57).

    ``query`` is free text, length-capped (§V18). ``server`` optionally scopes to
    one region (§V5); ``entity_type`` narrows the domain. ``limit`` is bounded to
    the §V19 window (default 10, max 50) -- an out-of-range value is rejected.

    ``locale`` (§V57, additive/optional §V21) filters to entities carrying a
    jp/kr NAME alias in that locale (``ja``/``ko`` only, B50). It is a NAME-tag axis,
    NOT a fact region: a match still returns the entity's OWN en/cn facts and never
    widens region availability (§V50). The fact-region locales (``en``/``zh``) are not
    filter values -- they are degenerate (≈ ``server=``) and asymmetric-broken (only
    operators self-alias). ``None`` = no locale filter (prior behavior).
    """

    query: str = Field(min_length=1, max_length=MAX_QUERY_LEN)
    server: Region | None = None
    entity_type: EntityType | None = None
    locale: SearchLocale | None = None
    limit: int = Field(default=SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT)
