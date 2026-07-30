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
from arknights_mcp.mcp.tools._enum_legend import (
    TOOL_ENUM_LEGEND_FIELDS,
    attach_enum_legend,
)
from arknights_mcp.mcp.tools._shared import (
    IMAGE_REFS_PATH_NOTE,
    ConnectionProvider,
    attach_image_ref_disclosures,
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
from arknights_mcp.services.image_refs import image_ref_to_dict, operator_banner_refs

_TOOL_NAME = "get_banners"
_TOOL_TITLE = "Get banners"
_TOOL_DESCRIPTION = (
    "List Arknights banner archive metadata by region (en/cn), sourced from the game "
    "data gacha_table. Each entry carries only its pool id, display name, open/end "
    "schedule, rule type, and the typed featured operators. It never carries gacha "
    "summary, detail, html, or image prose. The response's enum_legend gives the values "
    "of rule_type. Most pools carry no typed featured operator, because their rate-up is "
    "not in the typed game data. Those pools omit the featured_ops key instead of sending "
    "an empty list. A limitation names the rule types that happened for. Absence is "
    "always reported as a limitation, never fabricated. This "
    "is a historical schedule fact, not gacha planning. It has no pull-probability, "
    "pity, or spark. Optional since/until bounds window the list by ISO open-time "
    "(inclusive). An optional query narrows the list to banners whose display name "
    "contains that text (case-insensitive). Results are newest-first and paged (bounded "
    "page/page_size). When the "
    "image-reference source is enabled, a featured operator that resolved to a present "
    "operator also carries an image_refs list with its derived portrait and avatar "
    "references, decoded by the image_refs_legend the response carries. "
    + IMAGE_REFS_PATH_NOTE
    + " en/cn are never mixed."
)


def _featured_op_to_dict(op: FeaturedOpFacts, *, image_refs_enabled: bool) -> dict[str, object]:
    """One typed featured operator for the wire (§V62; no internal pk leak).

    When ``image_refs_enabled`` (the combined §T120 config + registry gate) AND the
    featured op soft-resolved to a present operator (§V62), an additive ``image_refs``
    list with its DERIVED portrait + avatar refs rides along (relative paths under the
    ``data``-level ``image_refs_base_url``, §T183/§V66) -- the resolved ``char_id``
    IS that operator's ``game_id`` (§V63). The avatar rides ALONGSIDE the portrait (§V72):
    the mirror's portrait tree lags newer operators, so a portrait-only ref may be dead
    while the avatar returns 200 one category over. An unresolved featured op carries no
    ref (its raw char id may not name a present operator), and when the gate is off the
    field is absent entirely (backward-compatible default, §V21).
    """
    data: dict[str, object] = {
        "char_id": op.char_id,
        "resolved": op.resolved,
        "operator_name": op.operator_name,
    }
    if image_refs_enabled and op.resolved:
        # §V63: DERIVED from the resolved featured char id (== the operator's game_id);
        # §V5: rides this banner's OWN region envelope; §V19: a bounded per-op attach.
        # §V72: portrait + avatar so a lagging portrait tree never leaves a dead-only ref.
        data["image_refs"] = [image_ref_to_dict(r) for r in operator_banner_refs(op.char_id)]
    return data


def _banner_to_dict(banner: BannerFacts, *, image_refs_enabled: bool) -> dict[str, object]:
    """One banner's metadata fields for the wire (metadata-only, §V62/§V16).

    §V77/§V66 (B79): no per-row ``region`` -- the response is single-region (``server``
    is required, §V5), so region is stated ONCE on the parent ``server`` field, never
    repeated on every banner row.

    §V67/§T199 (B138): a pool with no typed featured op OMITS ``featured_ops`` entirely
    instead of sending ``[]``. An empty list is §V67's CONFIRMED-none, and it was false for
    every pool here: the game data simply has no featured-operator array for them (counted
    over the pinned upstream, an EMPTY array occurs on zero pools in either region), so
    absence of the key is the honest encoding and the reason rides ``limitations``. The
    caveat is split by rule type there, so a FESCLASSIC pool is no longer described as a
    standard banner whose rate-up lives in prose.
    """
    data: dict[str, object] = {
        "game_id": banner.game_id,
        "display_name": banner.display_name,
        "open_time": banner.open_time,
        "end_time": banner.end_time,
        "rule_type": banner.rule_type,
    }
    if banner.featured_ops:
        data["featured_ops"] = [
            _featured_op_to_dict(op, image_refs_enabled=image_refs_enabled)
            for op in banner.featured_ops
        ]
    return data


def _shape(result: BannersResult, *, image_refs_enabled: bool) -> ResponseEnvelope:
    """Map the domain result to a typed §V23 ``ok`` envelope (§V5 region + provenance).

    A region with no banners is a legitimate empty list (gacha_table is fetched
    tolerant-absent, §V41/B36), so this is always an ``ok`` result -- never a
    ``not_found`` (this is a list tool, not an entity lookup). The list is paged
    (§V19/§V22): the ``page`` descriptor reports the full ``total`` + ``has_more`` while
    ``banners`` holds only the requested page. Provenance is the distinct banner
    snapshots backing the FULL filtered set (§V5, derived in the service so a later page
    never drops one). The §V62/§V26 caveats ride the envelope ``limitations``.

    §V77/§V66 (B79): region is stated ONCE on the parent ``server`` field (and the
    envelope provenance), never repeated on every banner row.
    """
    data: dict[str, object] = {
        "server": result.server,
        "banners": [
            _banner_to_dict(b, image_refs_enabled=image_refs_enabled) for b in result.banners
        ],
        "page": page_to_dict(result.page),
    }
    # §V72/§V26 (§T135, B61): the standing derived-unverified limitation rides the envelope
    # ONLY when the page actually emits an image_refs list -- i.e. the gate is on AND at
    # least one featured op on this page resolved (an unresolved op / standard banner
    # carries no ref). So the caveat appears exactly when there is a link to caveat, never
    # on a page that emitted none.
    emits_refs = image_refs_enabled and any(
        op.resolved for b in result.banners for op in b.featured_ops
    )
    # §T183/§V66 + §V72 (ADR 0014): the shared attach (one §V37 home) hoists the mirror
    # base ONCE onto data and appends the derived-unverified limitation, exactly when
    # this page actually emits refs; every ref carries only its relative path.
    limitations = attach_image_ref_disclosures(data, result.limitations, emits_refs=emits_refs)
    # §V104 (b)/(c): every banner row carries rule_type, so its STATIC 12-token domain +
    # the source-defined "may grow" caveat ride the response instead of the description
    # (§V111 a). B142/B138 is exactly this field: the description named 4 of 12, so the one
    # classification it delegated was undecidable for the other 7.
    # §V67: a region with no banners (gacha_table is tolerant-absent, B36) emits no
    # rule_type at all, so it ships neither the legend nor its openness caveat.
    limitations = attach_enum_legend(
        data, TOOL_ENUM_LEGEND_FIELDS[_TOOL_NAME] if result.banners else (), limitations
    )
    return ok(
        data,
        provenance=tuple(
            Provenance(server=result.server, snapshot_id=p.snapshot_id, imported_at=p.imported_at)
            for p in result.provenance
        ),
        limitations=limitations,
    )


def build_get_banners_spec(
    get_conn: ConnectionProvider, *, image_refs_enabled: bool = False
) -> ToolSpec:
    """Build the ``get_banners`` :class:`ToolSpec` (§T114; §V14).

    ``get_conn`` returns the process-wide read-only connection to the promoted build.
    ``image_refs_enabled`` is the combined §T120 emission gate (config private-only
    posture AND the ``arknights_game_resource`` source enabled, computed once at wiring
    time via :func:`~arknights_mcp.services.image_refs.refs_enabled`); it defaults
    ``False`` so a resolved featured op carries the additive ``image_refs`` portrait list
    only when the source is enabled (§V21/§V63). The returned spec is read-only (§V2) for
    the single shared registry both transports dispatch from (§V14); its ``input_schema``
    is the bounded model's JSON Schema, so the §V5 required ``server`` + the optional
    since/until window + the bounded ``page`` land on the wire exactly as validated.
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
                query=parsed.query,
                page=parsed.page.page,
                page_size=parsed.page.page_size,
            ),
            lambda result: _shape(result, image_refs_enabled=image_refs_enabled),
        )

    return ToolSpec(
        name=_TOOL_NAME,
        title=_TOOL_TITLE,
        description=_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(GetBannersInput),
    )
