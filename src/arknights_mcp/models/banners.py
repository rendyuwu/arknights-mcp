"""Bounded input model for ``get_banners`` (§T114; §V5/§V19/§V22/§V62).

A banner listing is region-attributed (§V5) and metadata-only (§V62). The optional
``since``/``until`` bounds narrow the list by the banner's ISO ``open_time`` schedule;
both are length capped (§V18) AND ISO-date-shape validated (§V19) via the shared
:func:`~arknights_mcp.models.common.validate_iso_bound` (§V37 -- the same gate the
``get_announcements`` since/until window uses, B48) so a non-date bound is rejected at
the model gate rather than lexicographically emptying the query.

The list is unbounded in principle (the archive accretes past + near-future banners),
so it pages through the bounded :class:`~arknights_mcp.models.common.PageParams`
(§V22/§V19); the page bounds surface in the tool ``inputSchema`` exactly as validated.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from arknights_mcp.models.common import (
    MAX_ID_LEN,
    PageParams,
    Region,
    StrictModel,
    validate_iso_bound,
)


class GetBannersInput(StrictModel):
    """Parameters for ``get_banners`` (§I; §V5/§V19/§V22/§V62).

    ``server`` is mandatory so the listing is region-attributed and en/cn are never
    silently mixed (§V5). ``since``/``until`` optionally window the banners by their
    stored ISO ``open_time`` (inclusive); both are length capped (§V18) AND ISO-date-
    shape validated (§V19) so a non-date bound is rejected rather than lexicographically
    emptying the result (B48). ``page`` pages the list through the bounded §V19 window so
    a single request never pulls an unbounded slice (§V22).
    """

    server: Region
    since: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LEN)
    until: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LEN)
    page: PageParams = Field(default_factory=PageParams)

    _validate_since = field_validator("since")(validate_iso_bound)
    _validate_until = field_validator("until")(validate_iso_bound)
