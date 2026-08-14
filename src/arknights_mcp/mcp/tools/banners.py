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
* **§V120** -- the max page window used to overrun the frame cap, and ``page_size`` is a
  knob that bounds it, so this tool declares an ordered shed plan (image refs, then rows)
  the envelope chokepoint applies: an over-cap page comes back as a smaller ``ok`` answer
  with a limitation naming what left, never as the empty payload the cap used to return for
  a window ``page_size<=80`` serves fine (B167). Since §T219 hoisted the per-ref
  ``source_id`` no live window overruns (the heaviest is 86.5% of cap), so the plan is
  declared ``dead_today`` -- kept because it is reachable by construction and proven by
  synthetic pages, not because a build exercises it (§V113 b/§V117).
* **§V23** -- every result is a typed-status envelope; a database failure or any
  unexpected error fails closed to a fixed, path/trace-free envelope via the shared
  :func:`~arknights_mcp.mcp.tools._shared.run_guarded` guard.
"""

from __future__ import annotations

from arknights_mcp.mcp.envelopes import Provenance, ResponseEnvelope, ok
from arknights_mcp.mcp.shed import ShedFrame, ShedPlan, ShedStep
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._enum_legend import (
    TOOL_ENUM_LEGEND_FIELDS,
    attach_enum_legend,
)
from arknights_mcp.mcp.tools._shared import (
    IMAGE_REFS_HOISTED_KEYS,
    IMAGE_REFS_LIMITATION,
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
    "(inclusive). Write both bounds as YYYY-MM-DD. Do not put since after until: such a "
    "window can match nothing, so it is rejected as an invalid request rather than "
    "answered with an empty list. An optional query narrows the list to banners whose "
    "display name "
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


#: §V120 (b): the ORDERED shed plan for this tool, heaviest part first. Counted over the
#: promoted build rather than guessed: on the heaviest live window (en page 1 at
#: ``page_size`` 100) the ref step alone takes the frame from 173003 bytes to 68444 with
#: every one of the 100 rows still on the wire, while trimming ROWS to reach that same size
#: keeps 6 of them. Rows are the fallback, and only for a page whose bare metadata will not
#: fit.
#:
#: Every frame figure here is :func:`~arknights_mcp.mcp.envelopes.wire_size` of the envelope
#: this tool actually emits, on ``2026-08-13T220624Z-en-cn``. §V121 (f) (i) is why a
#: correction has to reach this comment and not only the §T217 (a) row -- a figure
#: documenting a §V-enforced measure is the same claim wherever it lives, and this is the
#: comment the next shed plan gets copied from. Two corrections have already landed that
#: way: B171 (the pre-shed figure was taken on a hand-assembled ``{server, banners, page}``
#: subset, under-measuring by 4141 bytes, the one direction §V22 forbids) and §T219 (the
#: same shape reads 173003 rather than 220131 now that ``image_refs_source_id`` is hoisted;
#: the post-shed 68444 is unchanged, because this step retires the hoisted keys along with
#: the refs either way).
#:
#: §T219/§V120 (f): that leaves this plan DEAD ON TODAY'S CORPUS -- 173003 is 86.5% of the
#: cap, so no live window reaches the shed at all, and the declaration in
#: :data:`~arknights_mcp.mcp.cap_pressure.FRAME_PRESSURE` says ``dead_today`` rather than
#: ``live``. The plan STAYS because it is reachable by construction, not by hope:
#: ``page_size`` reaches 100 while a banner's ``featured_ops`` count is the source's to
#: decide, so one fat event re-crosses the cap and 86.5% is 13.5 points of margin, not a
#: guarantee. §V113 (b) then owes a proof rather than a claim, which
#: ``tests/unit/test_cap_shed.py`` supplies by driving this plan through the real
#: :func:`~arknights_mcp.mcp.envelopes.ok` chokepoint on synthetic pages.
_SHED_ORDER = ("image_refs", "banners")

#: §V120 (c): what left, and the knob that returns it (§V108 routing). The counts are
#: untouched by this shed -- only the derived links are gone -- so the list still reads as
#: complete. Client-facing text, so no internal cites/jargon (§V71 b): the cites live in
#: this comment, never the string; short sentences (§V71 f).
_REFS_SHED_LIMITATION = (
    "Image references were left out of this page to keep the response under its size "
    "limit. The banner list itself is complete and its counts are unchanged. Request a "
    "smaller page_size to receive the image references."
)


def _rows_shed_limitation(kept: int, on_page: int) -> str:
    """§V120 (c): the rows that left + the ``page_size`` that returns them.

    ``page.total`` is deliberately NOT rewritten to ``kept`` -- a trimmed list whose
    total shrank with it would read as §V67 CONFIRMED-none ("that is all there is"),
    which is the false claim §V120 (c) exists to prevent. So the count stays truthful and
    the shortfall is stated here, naming the page_size that fits as the way back to the
    rows this response dropped. Client-facing text, so no cites/jargon (§V71 b).
    """
    return (
        f"Only the first {kept} of this page's {on_page} banners are included, to keep "
        f"the response under its size limit. The total count is unchanged. Request a "
        f"page_size of {kept} or smaller to receive every banner."
    )


def _rows(frame: ShedFrame) -> list[dict[str, object]]:
    """This page's banner rows, or an empty list when the payload carries none."""
    rows = frame.payload.get("banners")
    return list(rows) if isinstance(rows, list) else []


def _ref_depths(frame: ShedFrame) -> int:
    """One shed depth when this page emits any image ref, else nothing to shed."""
    return 1 if "image_refs_base_url" in frame.payload else 0


def _shed_refs(frame: ShedFrame, depth: int) -> ShedFrame:
    """Drop every image ref on the page, and every field coupled to them.

    §V63/§V66: the :data:`IMAGE_REFS_HOISTED_KEYS` set and :data:`IMAGE_REFS_LIMITATION`
    ride a response exactly when it emits refs -- one predicate, one home
    (:func:`attach_image_ref_disclosures`). Shedding the refs and leaving those behind
    would ship a base URL for paths that are gone, an attribution for references nothing
    carries, a legend for labels nothing uses, and a caveat about links this response does
    not contain, so the step retires them together and states its own reason instead.

    The retirement set is DERIVED from the attach home rather than restated here (§V37):
    §V66 (4) (ii) makes joining this coupler a duty of every newly hoisted key, and a
    literal tuple in this function is exactly where that duty would be forgotten -- the
    ``image_refs_source_id`` §T219 added would have shipped a shed page still claiming
    attribution for refs it had just removed.

    The refs go from the WHOLE page, never from some rows only: an ``image_refs`` key
    absent on one row and present on another would mean "this operator has no derived
    art" in one place and "the response dropped it" in the other (§V67).
    """
    del depth  # one depth only: the part is shed whole (§V67), never half a page
    payload: dict[str, object] = {}
    for key, value in frame.payload.items():
        if key in IMAGE_REFS_HOISTED_KEYS:
            continue
        if key != "banners":
            payload[key] = value
            continue
        payload[key] = [
            {
                field: (
                    [{k: v for k, v in op.items() if k != "image_refs"} for op in cell]
                    if field == "featured_ops" and isinstance(cell, list)
                    else cell
                )
                for field, cell in row.items()
            }
            for row in (r for r in _rows(frame) if isinstance(r, dict))
        ]
    limitations = tuple(limit for limit in frame.limitations if limit != IMAGE_REFS_LIMITATION)
    return ShedFrame(payload=payload, limitations=(*limitations, _REFS_SHED_LIMITATION))


def _row_depths(frame: ShedFrame) -> int:
    """Depths available on the rows: one per row that can be dropped down to a single one.

    A page of one row offers no depth -- there is nothing left to trim that would still
    be an answer, so the plan runs out and the §V22 withhold takes over.
    """
    return max(len(_rows(frame)) - 1, 0)


def _shed_rows(frame: ShedFrame, depth: int) -> ShedFrame:
    """Keep the first ``len(rows) - 1 - depth`` rows: deeper index, strictly fewer rows.

    A prefix, not a sample: the ranking is fixed newest-first upstream (§V19), so the
    rows a client keeps are the same rows the head of a smaller ``page_size`` would have
    returned. ``page`` is passed through untouched (§V120 c).
    """
    rows = _rows(frame)
    kept = len(rows) - 1 - depth
    payload = dict(frame.payload)
    payload["banners"] = rows[:kept]
    return ShedFrame(
        payload=payload,
        limitations=(*frame.limitations, _rows_shed_limitation(kept, len(rows))),
    )


def _shed_plan() -> ShedPlan:
    """The declared §V120 plan for ``get_banners``: refs, then rows.

    Both steps are paginated (§V120 d) -- what a shed returns here is a smaller window
    of the same page, reachable by a ``page_size`` the caller may already request, so the
    result keeps its ``ok`` status rather than reporting a section the client asked for
    and did not get. Built from :data:`_SHED_ORDER` so the declared order and the applied
    order have one home.
    """
    steps: dict[str, ShedStep] = {
        "image_refs": ShedStep(
            part="image_refs", paginated=True, depths=_ref_depths, apply=_shed_refs
        ),
        "banners": ShedStep(part="banners", paginated=True, depths=_row_depths, apply=_shed_rows),
    }
    return tuple(steps[part] for part in _SHED_ORDER)


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
    # §V120 (a)/(b): the max page window is the one real shape that overruns the §V22
    # frame cap, and page_size is the knob that bounds it -- so the chokepoint shrinks
    # this payload along the declared plan instead of withholding all of it (B167).
    return ok(
        data,
        provenance=tuple(
            Provenance(server=result.server, snapshot_id=p.snapshot_id, imported_at=p.imported_at)
            for p in result.provenance
        ),
        limitations=limitations,
        shed_plan=_shed_plan(),
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
