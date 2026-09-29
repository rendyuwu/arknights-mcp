"""Bounded input model for ``get_banners``.

A banner listing is region-attributed and metadata-only. The optional
``since``/``until`` bounds narrow the list by the banner's ISO ``open_time`` schedule;
both are length capped AND ISO-date-shape validated via the shared
:func:`~arknights_mcp.models.common.normalize_iso_bound` (the same gate the
``get_announcements`` since/until window uses) so a non-date bound is rejected at
the model gate rather than lexicographically emptying the query.

The list is unbounded in principle (the archive accretes past + near-future banners),
so it pages through the bounded :class:`~arknights_mcp.models.common.PageParams`;
the page bounds surface in the tool ``inputSchema`` exactly as validated.
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from arknights_mcp.models.common import (
    MAX_ID_LEN,
    MAX_QUERY_LEN,
    PageParams,
    Region,
    StrictModel,
    normalize_iso_bound,
    validate_window_order,
)


class GetBannersInput(StrictModel):
    """Parameters for ``get_banners``.

    ``server`` is mandatory so the listing is region-attributed and en/cn are never
    silently mixed. ``since``/``until`` optionally window the banners by their
    stored ISO ``open_time`` (inclusive); both are length capped AND ISO-date-
    shape validated so a non-date bound is rejected rather than lexicographically
    emptying the result; an accepted bound is NORMALIZED to the canonical ISO
    notation, since the window compares TEXT and a basic-format bound
    collates against the stored timestamps arbitrarily (``until="20260101"`` used to be
    ignored outright); the bound RELATION is checked too, so a pair
    whose window can match nothing is rejected rather than answered with an empty list.
    ``query`` optionally narrows the list to banners whose
    display name contains it (case-insensitive substring); it is a free-text field so it
    is length capped at :data:`MAX_QUERY_LEN` and, being an additive optional
    filter over a still-paged list, does not weaken the no-dump bound. ``page`` pages
    the list through the bounded window so a single request never pulls an unbounded
    slice.
    """

    server: Region
    since: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LEN)
    until: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LEN)
    query: str | None = Field(default=None, min_length=1, max_length=MAX_QUERY_LEN)
    page: PageParams = Field(default_factory=PageParams)

    _normalize_since = field_validator("since")(normalize_iso_bound)
    _normalize_until = field_validator("until")(normalize_iso_bound)
    _validate_window = model_validator(mode="after")(validate_window_order)
