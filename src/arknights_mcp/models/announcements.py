"""Bounded input model for ``get_announcements`` (§T30; §T96; §V5/§V19/§V22/§V56).

An announcement listing is region-attributed (§V5) and metadata-only (§V56). The
optional ``since``/``until`` bounds narrow the list by ISO date; both are length
capped so a crafted value cannot carry an oversized blob (§V18).

The list is unbounded in principle (a live feed accretes over time), so it pages
through the bounded :class:`~arknights_mcp.models.common.PageParams` (§V22/§V19); the
page bounds surface in the tool ``inputSchema`` exactly as validated.
"""

from __future__ import annotations

from pydantic import Field

from arknights_mcp.models.common import MAX_ID_LEN, PageParams, Region, StrictModel


class GetAnnouncementsInput(StrictModel):
    """Parameters for ``get_announcements`` (§I; §V5/§V19/§V22/§V56).

    ``server`` is mandatory so the listing is region-attributed and en/cn are never
    silently mixed (§V5). ``since``/``until`` optionally window the announcements by
    their stored ISO date (inclusive); both are length capped (§V18). ``page`` pages
    the list through the bounded §V19 window so a single request never pulls an
    unbounded slice (§V22).
    """

    server: Region
    since: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LEN)
    until: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LEN)
    page: PageParams = Field(default_factory=PageParams)
