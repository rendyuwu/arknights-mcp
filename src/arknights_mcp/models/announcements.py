"""Bounded input model for ``get_announcements``.

An announcement listing is region-attributed and metadata-only. The
optional ``since``/``until`` bounds narrow the list by ISO date; both are length
capped so a crafted value cannot carry an oversized blob.

The list is unbounded in principle (a live feed accretes over time), so it pages
through the bounded :class:`~arknights_mcp.models.common.PageParams`; the
page bounds surface in the tool ``inputSchema`` exactly as validated.
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from arknights_mcp.models.common import (
    MAX_ID_LEN,
    PageParams,
    Region,
    StrictModel,
    normalize_iso_bound,
    validate_window_order,
)


class GetAnnouncementsInput(StrictModel):
    """Parameters for ``get_announcements``.

    ``server`` is mandatory so the listing is region-attributed and en/cn are never
    silently mixed. ``since``/``until`` optionally window the announcements by
    their stored ISO date (inclusive); both are length capped AND ISO-date-shape
    validated so a non-date bound is rejected rather than lexicographically
    emptying the result. An accepted bound is also NORMALIZED to the canonical ISO
    notation, because the window compares TEXT: a basic-format
    ``since="20260101"`` parses fine yet sorts above every stored ``2026-…`` date, which
    used to empty a wide-open window. The bound RELATION is checked too: a pair whose
    window can match nothing (``since`` after ``until``) is rejected rather than answered
    with an empty list a client cannot tell from a genuinely empty window. ``page`` pages
    the list through the bounded window so a single request never pulls an unbounded
    slice.
    """

    server: Region
    since: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LEN)
    until: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LEN)
    page: PageParams = Field(default_factory=PageParams)

    _normalize_since = field_validator("since")(normalize_iso_bound)
    _normalize_until = field_validator("until")(normalize_iso_bound)
    _validate_window = model_validator(mode="after")(validate_window_order)
