"""The shed order for ``get_stage`` is COUNTED, not fixed.

Arm (a) taught ``get_banners`` to shrink instead of withhold. ``get_stage`` never got a
plan, so one real stage answered ``partial`` with ``data: {}`` for every flag combination
that asked for its routes -- while a smaller ``routes_page.page_size`` returned the same
routes fine. That is the harm on a second tool, and the reason it stayed open is the
reason the defect shipped: no test drove the widest legal request over the real corpus.

What these guards pin beyond "it answers" is the ORDER. The rule originally illustrated
``get_stage`` with ``map_image`` then ``spawns`` then ``routes``, uncounted; the promoted
build says the over-cap frame is route-heavy, and shedding the image and the spawns first
still leaves it over the cap -- so that plan sheds three parts and reports ``partial``
where trimming routes alone fits at ``ok``. A test that only asserted "under the cap and
non-empty" would pass on both, which is exactly how a guessed order ships.

Driven through :func:`~arknights_mcp.mcp.envelopes.ok`: the shed is a
chokepoint rule, so calling the plan helpers directly would pass equally against a
per-service trim, the shape four per-surface rollouts argue against.

Synthetic payloads here, the real corpus next door. Both are needed: only the build says
the order was counted right, and only a synthetic frame can reach the ``map_image`` step,
which fires on zero live shapes today (reachability proven, never declared).
"""

from __future__ import annotations

from typing import Any

from arknights_mcp.mcp.envelopes import (
    MAX_RESPONSE_BYTES,
    Provenance,
    ResponseEnvelope,
    ok,
    wire_size,
)
from arknights_mcp.mcp.tools._stage_shed import _SHED_PARTS, stage_shed_plan
from arknights_mcp.models.common import PAGE_SIZE_MAX
from arknights_mcp.services.stage_map_render import MAX_MAP_IMAGE_BYTES

#: The join-key gloss ``get_stage`` attaches whenever it emits spawn rows.
#: Carried verbatim so a trim that dropped the last spawn row would orphan it here.
_SPAWN_NOTE = "An enemy's stats are listed per level_variant"


def _prov() -> Provenance:
    return Provenance(server="cn", snapshot_id="snap-1", imported_at="2026-08-14T00:00:00+00:00")


def _route(index: int, *, checkpoints: int) -> dict[str, Any]:
    """One route in the ``get_stage`` wire shape (checkpoints are always a list)."""
    return {
        "motion_mode": "WALK",
        "start": {"row": index % 8, "col": index % 12},
        "end": {"row": (index + 3) % 8, "col": (index + 5) % 12},
        "checkpoints": [
            {"type": "MOVE", "position": {"row": step % 8, "col": (step * 2) % 12}}
            for step in range(checkpoints)
        ],
        "count": 1,
    }


def _spawn(index: int) -> dict[str, Any]:
    return {
        "wave_index": index // 10,
        "enemy_game_id": f"enemy_{index:04d}_drone",
        "enemy_level_variant": 0,
        "route_index": index % 7,
        "spawn_time": 1.5 * index,
        "count": 1,
        "interval": 0.0,
        "spawn_group": f"group_{index % 3}",
        "hidden": False,
    }


def _payload(
    *,
    routes: int = 0,
    checkpoints: int = 40,
    spawns: int = 0,
    image_bytes: int = 0,
    routes_total: int = 116,
    spawns_total: int = 226,
) -> dict[str, Any]:
    """A ``get_stage`` payload in the real emitted shape, sized by the caller."""
    data: dict[str, Any] = {"stage": {"game_id": "act1football_01", "stage_code": "OF-1"}}
    if routes:
        data["routes"] = [_route(i, checkpoints=checkpoints) for i in range(routes)]
        data["routes_page"] = {
            "page": 1,
            "page_size": routes,
            "total": routes_total,
            "has_more": True,
        }
    if spawns:
        data["spawns"] = [_spawn(i) for i in range(spawns)]
        data["spawns_page"] = {
            "page": 1,
            "page_size": spawns,
            "total": spawns_total,
            "has_more": True,
        }
    if image_bytes:
        data["map_image"] = {
            "format": "svg",
            "media_type": "image/svg+xml",
            "content": "<svg>" + "d" * image_bytes + "</svg>",
            "pixel_width": 960,
            "pixel_height": 640,
            "tile_count": 220,
            "legend": [{"label": "wall", "fill": "#333333"}],
        }
    return data


def _emit(payload: dict[str, Any], *, limitations: tuple[str, ...] = ()) -> ResponseEnvelope:
    """One ``get_stage`` response through the chokepoint, with its declared plan."""
    return ok(
        payload,
        provenance=(_prov(),),
        limitations=limitations,
        shed_plan=stage_shed_plan(payload),
    )


def _over_cap_route_heavy() -> dict[str, Any]:
    """The live shape's proportions: routes dominant, image and spawns real but smaller."""
    return _payload(routes=100, checkpoints=90, spawns=100, image_bytes=28_000)


def _over_cap_image_heavy() -> dict[str, Any]:
    """The other real family: an image-dominant board with a few routes beside it."""
    return _payload(routes=7, checkpoints=12, spawns=21, image_bytes=110_000)


# ---------------------------------------------------------------------------


def test_an_over_cap_stage_answers_instead_of_withholding() -> None:
    # The original defect verbatim: this shape returned ``partial`` with ``data: {}``,
    # while the same routes came back fine at a smaller routes_page.page_size. The answer
    # is now on the wire and under the cap.
    env = _emit(_over_cap_route_heavy())
    assert env.data != {}
    assert env.data["stage"]["game_id"] == "act1football_01"
    assert wire_size(env) <= MAX_RESPONSE_BYTES


def test_a_response_that_fits_is_left_alone() -> None:
    # The non-degenerate control: the plan is built on every call, so a shed that fired
    # on measure rather than on the cap would strip this one too.
    payload = _payload(routes=8, checkpoints=10, spawns=12, image_bytes=4_000)
    env = _emit(payload)
    assert env.status == "ok"
    assert len(env.data["routes"]) == 8
    assert "map_image" in env.data
    assert env.limitations == ()


# ---------------------------------------------------------------------------


def test_plan_order_follows_the_measured_weight() -> None:
    # The ordering RULE, both directions, on the two families the corpus actually holds.
    # A constant order passes one of these and fails the other -- which is the whole
    # reason the declaration is the step set plus the rule.
    route_heavy = [step.part for step in stage_shed_plan(_over_cap_route_heavy())]
    image_heavy = [step.part for step in stage_shed_plan(_over_cap_image_heavy())]
    assert route_heavy[0] == "routes"
    assert image_heavy[0] == "map_image"
    assert sorted(route_heavy) == sorted(image_heavy) == sorted(_SHED_PARTS)


def test_the_route_heavy_shape_keeps_its_image_and_spawns() -> None:
    # The counted order sheds ONE part. The order the rule first illustrated would shed
    # the image and the spawns first, still be over the cap, and trim the routes anyway.
    env = _emit(_over_cap_route_heavy())
    assert "map_image" in env.data
    assert len(env.data["spawns"]) == 100
    assert 0 < len(env.data["routes"]) < 100


def test_the_declared_order_would_shed_three_parts_where_one_fits() -> None:
    # The refutation itself, pinned: dropping the image and the spawns whole is not
    # enough to fit this frame, so a map_image-first plan cannot stop before the routes.
    payload = _over_cap_route_heavy()
    without_image_and_spawns = {
        key: value for key, value in payload.items() if key not in ("map_image", "spawns")
    }
    stripped = ok(without_image_and_spawns, provenance=(_prov(),), shed_plan=())
    assert stripped.status == "partial"
    assert stripped.data == {}


def test_ties_fall_back_to_the_declared_order() -> None:
    # A payload with nothing to weigh still yields a deterministic plan in the declared
    # order, so the tie-break is not left to dict iteration order.
    assert [step.part for step in stage_shed_plan({})] == list(_SHED_PARTS)


# ---------------------------------------------------------------------------


def test_page_totals_survive_the_shed() -> None:
    # A total that shrank with the trim would read as CONFIRMED-none: "this stage
    # has 41 routes". The count stays truthful; the shortfall is a limitation.
    env = _emit(_over_cap_route_heavy())
    assert env.data["routes_page"]["total"] == 116
    assert env.data["routes_page"]["page_size"] == 100
    assert env.data["spawns_page"]["total"] == 226


def test_the_shed_limitation_names_the_knob_that_returns_what_left() -> None:
    env = _emit(_over_cap_route_heavy())
    shed_note = next(limit for limit in env.limitations if "size limit" in limit)
    kept = len(env.data["routes"])
    assert "routes_page.page_size" in shed_note
    assert f"first {kept} of this page's 100 routes" in shed_note
    # The page count, never the section total -- the stage has 116, the page had 100.
    assert "116" not in shed_note


def test_the_image_shed_limitation_names_its_flag() -> None:
    env = _emit(_over_cap_image_heavy())
    shed_note = next(limit for limit in env.limitations if "map image" in limit)
    assert "include_map_image" in shed_note
    assert "include_routes" in shed_note


# ---------------------------------------------------------------------------


def test_a_paginated_trim_stays_ok() -> None:
    # A smaller window of the same page is a smaller LEGAL window, reachable by a
    # page_size this caller may already request -- not a section they asked for and
    # did not get.
    assert _emit(_over_cap_route_heavy()).status == "ok"


def test_a_section_shed_is_partial() -> None:
    # The other half of the split. Collapsing both into ``partial`` means nothing;
    # collapsing both into ``ok`` lets a missing section go silent.
    env = _emit(_over_cap_image_heavy())
    assert env.status == "partial"
    assert "map_image" not in env.data
    assert env.data["stage"]["game_id"] == "act1football_01"


# ---------------------------------------------------------------------------


def test_the_image_step_is_reachable() -> None:
    # The image step fires on ZERO live shapes on the promoted build (the heaviest real
    # image response sits at 95.5% of the cap), so its reachability is PROVEN here rather
    # than declared -- the earlier arm owed the same proof for its own row step. The image is
    # sized UNDER MAX_MAP_IMAGE_BYTES: an image the renderer would refuse proves nothing,
    # and the point is that the shipped bound already permits one that overruns the frame.
    assert MAX_MAP_IMAGE_BYTES > 120_000
    env = _emit(_payload(image_bytes=120_000))
    assert env.status == "partial"
    assert "map_image" not in env.data
    assert wire_size(env) <= MAX_RESPONSE_BYTES


def test_spawns_declares_no_step_because_it_cannot_reach_the_cap() -> None:
    # Unreachable, so retire -- and the retirement carries the count that makes it
    # true. PAGE_SIZE_MAX bounds the section at 100 rows of fixed scalars (19055 bytes on
    # the whole promoted build), so once routes are down to one row and the image is gone,
    # no stage can present a remainder near the cap. A spawns step would be an arm that
    # cannot fire; if the bounds move, the tripwire is what says so.
    assert "spawns" not in _SHED_PARTS
    fat_spawns = _payload(routes=2, checkpoints=4, spawns=PAGE_SIZE_MAX, image_bytes=0)
    env = _emit(fat_spawns)
    assert env.status == "ok"
    assert len(env.data["spawns"]) == PAGE_SIZE_MAX
    assert wire_size(env) < MAX_RESPONSE_BYTES // 2


# ----------------------------------------------------------------------------


def test_a_trim_keeps_a_prefix_never_a_partial_row() -> None:
    # Rows leave whole and from the tail: a half-written route, or rows dropped from the
    # middle, would make the kept list something no page_size ever returns.
    payload = _over_cap_route_heavy()
    env = _emit(payload)
    kept = env.data["routes"]
    assert kept == payload["routes"][: len(kept)]


def test_a_route_trim_never_empties_the_section() -> None:
    # The floor is only observable where NO depth fits -- a page whose single heaviest
    # route already overruns the cap. Without it the step would return ``routes: []``
    # beside ``routes_page.total: 116``, which is a lie no page_size can produce
    # and which orphans every caveat describing the route data. The plan runs out
    # instead, and the withhold takes over (the floor, not the first answer).
    env = _emit(_payload(routes=2, checkpoints=2_400))
    assert env.data.get("routes") != []
    assert env.status == "partial"
    assert env.data == {}


def test_the_floor_still_answers_where_one_route_fits() -> None:
    # ...and the step is not simply giving up early: one route below that threshold comes
    # back, so the withhold above is the cap biting, not the floor short-circuiting.
    env = _emit(_payload(routes=2, checkpoints=1_200))
    assert env.status == "ok"
    assert len(env.data["routes"]) == 1


def test_the_spawn_note_survives_a_route_shed() -> None:
    # The join-key gloss describes the spawn rows. The spawns are never touched by this
    # plan, so a caveat about their key must still be standing on a shed response.
    payload = _payload(routes=100, checkpoints=90, spawns=100, image_bytes=28_000)
    env = _emit(payload, limitations=(_SPAWN_NOTE,))
    assert _SPAWN_NOTE in env.limitations
    assert len(env.data["spawns"]) == 100
