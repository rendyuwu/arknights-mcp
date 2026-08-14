"""``get_stage``'s §V120 shed plan (B169).

``get_banners`` (T217 a) was the first tool to declare one, and its plan is a fixed
order: ``image_refs`` are 55-71% of row bytes on *every* real page, so one counted
ranking holds for the whole corpus. ``get_stage`` is not like that. Counted over the
promoted build (6716 stages x every include-flag combination, the §V22 cap lifted so the
pre-shed payload is visible), its heavy responses fall into two families:

* **route-heavy** -- ``cn/act1football_01`` (``routes`` 67.6% of the payload),
  ``camp_r_20`` (57.0%), ``act42side_ex01`` (61.4%);
* **image-heavy** -- ``act2multi_raft`` (``map_image`` 96.3%, at 95.5% of the frame
  cap), ``act49side_10`` (87.1%).

Either fixed order over-sheds the family it was not counted on, so what this module
declares is the step SET plus the ordering RULE, and the applied order is the measured
weight of the response in hand (§V120 b as amended, §V121 d). The order first written
into §V120 (b) -- ``map_image`` then ``spawns`` then ``routes`` -- was a guess, and the
build refutes it: shedding the image and the spawns off the max-detail call leaves 210269
bytes, still over the 200000 cap, so that plan sheds three parts and reports ``partial``
where trimming routes alone fits at ``ok``. That is the rows-first defect T217 (a)
rejected, re-introduced by the invariant's own illustration (B169).

The live harm this closes: ``cn/act1football_01`` answered ``partial`` with ``data: {}``
for every one of the eight flag combinations that include routes at ``page_size=100``
(208261-302656 bytes), and for ``routes_page_size`` 43-100 with all four flags on --
while a smaller ``routes_page.page_size`` returned the same routes fine.

**``spawns`` declares no step, and that is counted, not overlooked** (§V117:
unreachable ⇒ retire). The section is bounded by construction -- ``PAGE_SIZE_MAX`` is
100 and a spawn row is a fixed set of scalars -- so it maxes at 19055 bytes on the whole
build. Once the two declared steps are exhausted (routes down to a single row, the image
gone), the largest remainder any stage can present is about 25 KB of payload, well under
the cap. A spawns step could therefore never be the one that made a response fit; adding
it would declare an arm that cannot fire. If those bounds ever move, the §V120 (f)
tripwire is what says so.

The §V120 (d) status split falls out of the two parts themselves: ``routes`` is a paged
section, so a trim hands back a smaller *legal* window the caller can page (``ok``);
``map_image`` has only an on/off flag, so losing it is a section the client asked for and
did not get (``partial``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from arknights_mcp.mcp.shed import ShedFrame, ShedPlan, ShedStep

_ROUTES = "routes"
_MAP_IMAGE = "map_image"

#: §V120 (b)/§V37: the declared step set, and the tie-break when two parts weigh the
#: same. One home for "which parts may be shed" and "how ties resolve"; the *applied*
#: order is derived from this by :func:`stage_shed_plan` counting the response, so the
#: declaration and the application cannot drift apart.
_SHED_PARTS = (_ROUTES, _MAP_IMAGE)


def _routes_shed_limitation(kept: int, on_page: int) -> str:
    """§V120 (c): the routes that left + the page knob that returns them.

    ``routes_page.total`` is NOT rewritten to ``kept``. A trimmed list whose total shrank
    with it would read as §V67 CONFIRMED-none -- "that is all this stage has" -- which is
    the false claim §V120 (c) exists to prevent.

    ``on_page`` is the count on THIS page, never the section total: a page of 100 cut to
    41 on a stage with 116 routes must not say "41 of this stage's 100", which would be
    false twice over. Naming the knob *and* a number is safe here for the same reason it
    is on ``get_banners``' row step -- ``page_size=kept`` walks the whole section -- and
    the total stays in the untouched descriptor beside it. Client-facing text, so no
    cites or internal jargon (§V71 b); short sentences (§V71 f).
    """
    return (
        f"Only the first {kept} of this page's {on_page} routes are included, to keep the "
        f"response under its size limit. The total count is unchanged. Set "
        f"routes_page.page_size to {kept} or smaller to receive every route."
    )


#: §V120 (c) for the one part with no page knob. ``include_map_image`` is already ON when
#: this fires -- that is why the image was rendered -- so the route back is narrowing the
#: OTHER sections, and "request include_map_image" alone would be advice that changes
#: nothing (§V114 (b) class: never mischaracterise what the caller would get).
_MAP_IMAGE_SHED_LIMITATION = (
    "The rendered map image was left out to keep the response under its size limit. "
    "Every other section of this response is complete. include_map_image is still set, "
    "so ask for the stage again with fewer other sections -- turn off include_routes or "
    "include_spawns, or lower their page sizes -- to receive the image."
)


def _rows(payload: Mapping[str, object], key: str) -> list[object]:
    """The list under ``key``, or empty when this response carries none."""
    value = payload.get(key)
    return list(value) if isinstance(value, list) else []


def _route_depths(frame: ShedFrame) -> int:
    """Depths on the route page: one per row droppable down to a single one.

    A page of one route offers no depth -- there is nothing left to trim that would still
    be an answer -- so the step is skipped and the next part in the plan takes over.
    """
    return max(len(_rows(frame.payload, _ROUTES)) - 1, 0)


def _shed_routes(frame: ShedFrame, depth: int) -> ShedFrame:
    """Keep the first ``len(rows) - 1 - depth`` routes: deeper index, strictly fewer.

    A prefix, not a sample: the section is ordered upstream (distinct route geometry in
    source order, digested before paging so page 1 is stable), so the routes kept are the
    routes the head of a smaller ``page_size`` would have returned -- which is what makes
    the §V120 (e) claim true. ``routes_page`` passes through untouched (§V120 c).
    """
    rows = _rows(frame.payload, _ROUTES)
    kept = len(rows) - 1 - depth
    payload = dict(frame.payload)
    payload[_ROUTES] = rows[:kept]
    return ShedFrame(
        payload=payload,
        limitations=(*frame.limitations, _routes_shed_limitation(kept, len(rows))),
    )


def _map_image_depths(frame: ShedFrame) -> int:
    """One depth when this response rendered an image, else nothing to shed."""
    return 1 if _MAP_IMAGE in frame.payload else 0


def _shed_map_image(frame: ShedFrame, depth: int) -> ShedFrame:
    """Drop the rendered SVG whole.

    Nothing else is retired with it. The SVG carries its own colour legend inside the
    ``map_image`` object, and the route-side caveats (an unknown checkpoint type, a
    truncated route read) describe the route DATA, which is still on the wire whenever
    routes were requested -- unlike ``get_banners``, where the base URL, the legend and
    the derived-link caveat all ride the refs and had to leave together (§V63/§V66).
    """
    del depth  # one depth only: the section goes whole, never half an image
    return ShedFrame(
        payload={key: value for key, value in frame.payload.items() if key != _MAP_IMAGE},
        limitations=(*frame.limitations, _MAP_IMAGE_SHED_LIMITATION),
    )


_STEPS: dict[str, ShedStep] = {
    # §V120 (d): a trimmed page is a smaller LEGAL window, reachable by a page_size this
    # caller may already request, so the result keeps its ``ok`` status...
    _ROUTES: ShedStep(part=_ROUTES, paginated=True, depths=_route_depths, apply=_shed_routes),
    # ...while the image is a section the client named through include_map_image and did
    # not get, which is the other half of the split. It fires on zero live shapes today
    # (the heaviest real image response sits at 95.5% of the cap) but stays reachable BY
    # CONSTRUCTION: MAX_MAP_IMAGE_BYTES is 128000, and a 128 KB image alone puts the
    # doubled frame over the 200000 cap (§V113 b -- proven by a synthetic frame in the
    # unit guard, never merely declared).
    _MAP_IMAGE: ShedStep(
        part=_MAP_IMAGE, paginated=False, depths=_map_image_depths, apply=_shed_map_image
    ),
}


def _weight(payload: Mapping[str, object], part: str) -> int:
    """Bytes this part contributes to the response in hand (§V120 b).

    An absent part costs nothing to weigh, which is the common case: the heavy sections
    are opt-in and off by default, so a plain ``get_stage`` call serializes nothing here.
    Compact separators because the ordering only needs the parts compared on one basis.
    """
    if part not in payload:
        return 0
    return len(json.dumps(payload[part], separators=(",", ":")).encode("utf-8"))


def stage_shed_plan(payload: Mapping[str, object]) -> ShedPlan:
    """The §V120 plan for this ``get_stage`` response: heaviest measured part first.

    Counting the response rather than fixing an order is the whole point (§V121 d): the
    corpus carries a route-heavy family and an image-heavy family, and whichever order a
    constant declares, it over-sheds the other one. On the only shape that exceeds the cap
    today the measurement puts ``routes`` first, which fits the frame with the image and
    the spawns untouched and the status still ``ok`` -- where the order originally written
    into §V120 (b) would have shed all three and reported ``partial``.

    Ties fall back to :data:`_SHED_PARTS`, so the plan is deterministic for a payload
    where both parts weigh the same (including the all-zero case, where every step reports
    no depth and the plan is a no-op).
    """
    weights = {part: _weight(payload, part) for part in _SHED_PARTS}
    ordered = sorted(_SHED_PARTS, key=lambda part: (-weights[part], _SHED_PARTS.index(part)))
    return tuple(_STEPS[part] for part in ordered)
