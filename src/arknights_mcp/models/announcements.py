"""Bounded input model for ``get_announcements`` (§T30; §T96; §V5/§V19/§V22/§V56).

An announcement listing is region-attributed (§V5) and metadata-only (§V56). The
optional ``since``/``until`` bounds narrow the list by ISO date; both are length
capped so a crafted value cannot carry an oversized blob (§V18).

The list is unbounded in principle (a live feed accretes over time), so it pages
through the bounded :class:`~arknights_mcp.models.common.PageParams` (§V22/§V19); the
page bounds surface in the tool ``inputSchema`` exactly as validated.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from arknights_mcp.models.common import MAX_ID_LEN, PageParams, Region, StrictModel


def _validate_iso_bound(value: str | None) -> str | None:
    """Reject a since/until bound that is not an ISO date/datetime (§V19/§V56).

    The stored ``date`` is compared lexicographically (the T95 shape is a full ISO
    datetime, the §V61 shape a ``YYYY-MM-DD``), so a non-date bound like ``"july"`` is
    accepted by the length cap yet sorts BEFORE every ISO date, silently emptying the
    windowed query with no error -- a caller cannot tell "no announcements in range"
    from "malformed bound" (B48). :func:`datetime.fromisoformat` accepts both a bare
    ISO date and a full ISO datetime (and validates the calendar parts), so a value it
    rejects surfaces as a protocol-level ``ValidationError`` at the model gate.
    """
    if value is None:
        return value
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "since/until must be an ISO date (YYYY-MM-DD) or ISO datetime "
            f"(§V19/§V56); got {value!r}"
        ) from exc
    return value


class GetAnnouncementsInput(StrictModel):
    """Parameters for ``get_announcements`` (§I; §V5/§V19/§V22/§V56).

    ``server`` is mandatory so the listing is region-attributed and en/cn are never
    silently mixed (§V5). ``since``/``until`` optionally window the announcements by
    their stored ISO date (inclusive); both are length capped (§V18) AND ISO-date-shape
    validated (§V19) so a non-date bound is rejected rather than lexicographically
    emptying the result. ``page`` pages the list through the bounded §V19 window so a
    single request never pulls an unbounded slice (§V22).
    """

    server: Region
    since: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LEN)
    until: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LEN)
    page: PageParams = Field(default_factory=PageParams)

    _validate_since = field_validator("since")(_validate_iso_bound)
    _validate_until = field_validator("until")(_validate_iso_bound)
