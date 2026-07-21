"""``get_banners`` MCP tool (§T114; §V5/§V19/§V22/§V23/§V26/§V62; §I.tool).

Bridges the bounded :class:`~arknights_mcp.models.banners.GetBannersInput` model (§T30)
to the shared :func:`~arknights_mcp.services.banners.get_banners` service (§V14) and
wraps the outcome in the typed
:class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope` (§T29). It owns no query logic --
only the model -> service -> envelope mapping -- so both transports dispatch identical
read-only (§V2) behaviour from the single registry, and it never fetches gacha_table at
query time (§V1); it only reads the archive a CLI sync/import promoted.

The load-bearing invariants:

* **§V5** -- ``server`` is required, so every listed banner is region-attributed + the
  envelope carries the region-scoped provenance; en/cn are never silently mixed.
* **§V62/§V16/§V18** -- METADATA-ONLY: the shaper emits only the schedule/identity fields
  + the TYPED featured operators (char id, resolved flag, resolved operator name); there
  is no gacha summary/detail/html/image to surface, and the 0013 schema cannot hold one.
* **§V26/§V62** -- a standard banner carries no typed featured-op, surfaced as a listing
  ``limitations`` caveat (never a fabricated rate-up); an unresolved featured op is
  surfaced as its raw char id plus a caveat.
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
from arknights_mcp.models.banners import GetBannersInput
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.services.banners import (
    BannerFacts,
    BannersResult,
    FeaturedOpFacts,
    get_banners,
)

_TOOL_NAME = "get_banners"
_TOOL_TITLE = "Get banners"
_TOOL_DESCRIPTION = (
    "List Arknights banner ARCHIVE metadata by region (en/cn), sourced from the game "
    "data gacha_table: each entry carries only its pool id, display name, open/end "
    "schedule, rule type, and the TYPED featured operators -- never gacha summary, "
    "detail, html, or image prose. A standard banner (NORMAL/SINGLE/DOUBLE/LINKAGE) "
    "carries no typed featured operator (its rate-up is not in the typed game data); "
    "that is reported as a limitation, never fabricated. This is a historical schedule "
    "FACT, not gacha PLANNING (no pull-probability, pity, or spark). Optional since/until "
    "bounds window the list by ISO open-time (inclusive); results are newest-first and "
    "paged (bounded page/page_size). en/cn are never mixed."
)


def _featured_op_to_dict(op: FeaturedOpFacts) -> dict[str, object]:
    """One typed featured operator for the wire (§V62; no internal pk leak)."""
    return {
        "char_id": op.char_id,
        "resolved": op.resolved,
        "operator_name": op.operator_name,
    }


def _banner_to_dict(banner: BannerFacts) -> dict[str, object]:
    """One banner's metadata fields for the wire (metadata-only, §V62/§V16)."""
    return {
        "game_id": banner.game_id,
        "display_name": banner.display_name,
        "open_time": banner.open_time,
        "end_time": banner.end_time,
        "rule_type": banner.rule_type,
        "region": banner.region,
        "featured_ops": [_featured_op_to_dict(op) for op in banner.featured_ops],
    }


def _shape(result: BannersResult) -> ResponseEnvelope:
    """Map the domain result to a typed §V23 ``ok`` envelope (§V5 region + provenance).

    A region with no banners is a legitimate empty list (gacha_table is fetched
    tolerant-absent, §V41/B36), so this is always an ``ok`` result -- never a
    ``not_found`` (this is a list tool, not an entity lookup). The list is paged
    (§V19/§V22): the ``page`` descriptor reports the full ``total`` + ``has_more`` while
    ``banners`` holds only the requested page. Provenance is the distinct banner
    snapshots backing the FULL filtered set (§V5, derived in the service so a later page
    never drops one). The §V62/§V26 caveats ride the envelope ``limitations``.
    """
    data: dict[str, object] = {
        "banners": [_banner_to_dict(b) for b in result.banners],
        "page": page_to_dict(result.page),
    }
    return ok(
        data,
        provenance=tuple(
            Provenance(server=result.server, snapshot_id=p.snapshot_id, imported_at=p.imported_at)
            for p in result.provenance
        ),
        limitations=result.limitations,
    )


def build_get_banners_spec(get_conn: ConnectionProvider) -> ToolSpec:
    """Build the ``get_banners`` :class:`ToolSpec` (§T114; §V14).

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
        parsed = GetBannersInput.model_validate(params)
        return run_guarded(
            get_conn,
            lambda conn: get_banners(
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
        input_schema=tool_input_schema(GetBannersInput),
    )
