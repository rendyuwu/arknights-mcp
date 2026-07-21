"""``get_announcements`` MCP tool (§T96; §V5/§V19/§V22/§V23/§V56; §I.tool).

Bridges the bounded :class:`~arknights_mcp.models.announcements.GetAnnouncementsInput`
model (§T30) to the shared :func:`~arknights_mcp.services.announcements.get_announcements`
service (§V14) and wraps the outcome in the typed
:class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope` (§T29). It owns no query logic --
only the model -> service -> envelope mapping -- so both transports dispatch identical
read-only (§V2) behaviour from the single registry, and it never fetches the feed at
query time (§V1); it only reads the metadata cache a CLI sync/import promoted.

The load-bearing invariants:

* **§V5** -- ``server`` is required, so every listed announcement is region-attributed +
  the envelope carries the region-scoped provenance; en/cn are never silently mixed.
* **§V56/§V16/§V18** -- METADATA-ONLY: the shaper emits exactly the five metadata fields
  (announce_id/title/date/url/category); there is no body/html/prose to surface, and the
  0010 schema cannot hold one.
* **§V19/§V22** -- the list is paged through a bounded window (``page``); the ranking is
  fixed (newest first) + provenance computed over the FULL set upstream, so a page never
  shifts them, and the size-capped envelope keeps the default response small.
* **§V23** -- every result is a typed-status envelope; a database failure or any
  unexpected error fails closed to a fixed, path/trace-free envelope via the shared
  :func:`~arknights_mcp.mcp.tools._shared.run_guarded` guard.
"""

from __future__ import annotations

from arknights_mcp.mcp.envelopes import Provenance, ResponseEnvelope, ok
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._shared import (
    ConnectionProvider,
    page_to_dict,
    run_guarded,
)
from arknights_mcp.models.announcements import GetAnnouncementsInput
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.services.announcements import (
    AnnouncementFacts,
    AnnouncementsResult,
    get_announcements,
)

_TOOL_NAME = "get_announcements"
_TOOL_TITLE = "Get announcements"
_TOOL_DESCRIPTION = (
    "List official Arknights announcement METADATA by region (en/cn), sourced from "
    "the official news feed: each entry carries only its announce_id, title, date, "
    "url, and category -- never the article body, html, or prose. Optional since/until "
    "bounds window the list by ISO date (inclusive); results are newest-first and paged "
    "(bounded page/page_size). en/cn are never mixed. The announcement source is "
    "disabled by default, so a region with no imported feed returns an empty list."
)


def _announcement_to_dict(ann: AnnouncementFacts) -> dict[str, object]:
    """One announcement's metadata fields for the wire (metadata-only, §V56/§V16)."""
    return {
        "announce_id": ann.announce_id,
        "title": ann.title,
        "date": ann.date,
        "url": ann.url,
        "category": ann.category,
        "region": ann.region,
    }


def _shape(result: AnnouncementsResult) -> ResponseEnvelope:
    """Map the domain result to a typed §V23 ``ok`` envelope (§V5 region + provenance).

    A region with no announcements is a legitimate empty list (the adapter is disabled
    by default, D14/§V56), so this is always an ``ok`` result -- never a ``not_found``
    (this is a list tool, not an entity lookup). The list is paged (§V19/§V22): the
    ``page`` descriptor reports the full ``total`` + ``has_more`` while ``announcements``
    holds only the requested page. Provenance is the distinct announcement snapshots
    backing the FULL filtered set (§V5, derived in the service so a later page never
    drops one); the comparison is region-scoped, so every provenance row shares the
    requested region.
    """
    data: dict[str, object] = {
        "announcements": [_announcement_to_dict(a) for a in result.announcements],
        "page": page_to_dict(result.page),
    }
    return ok(
        data,
        provenance=tuple(
            Provenance(server=result.server, snapshot_id=p.snapshot_id, imported_at=p.imported_at)
            for p in result.provenance
        ),
    )


def build_get_announcements_spec(get_conn: ConnectionProvider) -> ToolSpec:
    """Build the ``get_announcements`` :class:`ToolSpec` (§T96; §V14).

    ``get_conn`` returns the process-wide read-only connection to the promoted build.
    The returned spec is read-only (§V2) for the single shared registry both transports
    dispatch from (§V14); its ``input_schema`` is the bounded model's JSON Schema, so
    the §V5 required ``server`` + the optional since/until window + the bounded ``page``
    land on the wire exactly as validated.
    """

    def handler(**params: object) -> ResponseEnvelope:
        # §V5/§V18/§V19 gate: the bounded model requires a region, caps the date-bound
        # strings, rejects an out-of-range page_size, and rejects an unknown parameter
        # *before* any query runs -- a ValidationError propagates as a protocol-level
        # rejection, never a silently widened page (§V19).
        parsed = GetAnnouncementsInput.model_validate(params)
        return run_guarded(
            get_conn,
            lambda conn: get_announcements(
                conn,
                server=parsed.server,
                since=parsed.since,
                until=parsed.until,
                page=parsed.page.page,
                page_size=parsed.page.page_size,
            ),
            _shape,
        )

    return ToolSpec(
        name=_TOOL_NAME,
        title=_TOOL_TITLE,
        description=_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(GetAnnouncementsInput),
    )
