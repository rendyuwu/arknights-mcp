"""T217 (a): §V120 -- what an over-cap response EMITS (B167).

§V22 said what the cap is and how it is measured; nothing said what the reply looks like
when it bites. ``_enforce_cap`` therefore had one move -- drop all ``data``, flip to
``partial`` -- and once T216 re-based the cap onto the full result frame (§V119 e), two
legal ``get_banners`` windows started answering with nothing at all while
``page_size<=80`` returned the same rows fine.

The rule these guards pin: when the request carries a knob that bounds the oversized
part, shrink that part until the frame fits and say what left; withholding is the floor
for a payload no knob bounds, not the answer to every over-cap reply.

Driven through :func:`~arknights_mcp.mcp.envelopes.ok` on purpose (§V120 b). The shed is
a chokepoint rule, so a test that called the shed helpers directly would pass just as
well against a per-service measure-and-trim copy -- the shape B135's four per-surface
rollouts are the standing argument against.
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
from arknights_mcp.mcp.shed import ShedFrame, ShedStep, shed_to_fit
from arknights_mcp.mcp.tools._shared import IMAGE_REFS_LIMITATION
from arknights_mcp.mcp.tools.banners import _SHED_ORDER, _shed_plan
from arknights_mcp.services.image_refs import IMAGE_REFS_BASE_URL


def _prov() -> Provenance:
    return Provenance(server="en", snapshot_id="snap-1", imported_at="2026-08-14T00:00:00+00:00")


def _row(index: int, *, refs: int, ref_bytes: int, name_bytes: int) -> dict[str, Any]:
    """One banner row in the real ``get_banners`` wire shape (§V62)."""
    return {
        "game_id": f"pool_{index:04d}",
        "display_name": f"Banner {index:04d} " + "n" * name_bytes,
        "open_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-15T00:00:00+00:00",
        "rule_type": "NORMAL",
        "featured_ops": [
            {
                "char_id": f"char_{index:04d}_op",
                "resolved": True,
                "operator_name": f"Operator {index:04d}",
                "image_refs": [
                    {
                        "category": "portrait",
                        "variant": "e2",
                        "path": f"portrait/{index:04d}/" + "p" * ref_bytes,
                    }
                    for _ in range(refs)
                ],
            }
        ],
    }


def _payload(
    *, rows: int, refs: int = 4, ref_bytes: int = 200, name_bytes: int = 0, total: int = 4321
) -> dict[str, Any]:
    """A ``get_banners`` payload with the image-ref coupler attached (§V63/§V66)."""
    return {
        "server": "en",
        "banners": [
            _row(i, refs=refs, ref_bytes=ref_bytes, name_bytes=name_bytes) for i in range(rows)
        ],
        "page": {"page": 1, "page_size": rows, "total": total, "has_more": True},
        "image_refs_base_url": IMAGE_REFS_BASE_URL,
        "image_refs_legend": {"category": {"portrait": "full-body art"}},
    }


def _banners_ok(payload: dict[str, Any]) -> ResponseEnvelope:
    """The payload through the real chokepoint with the real declared plan."""
    return ok(
        payload,
        provenance=[_prov()],
        limitations=(IMAGE_REFS_LIMITATION,),
        shed_plan=_shed_plan(),
    )


#: Rows heavy enough in refs that the frame is over cap, light enough that it fits once
#: the refs go -- the live en page-1 ``page_size=100`` shape (220131 -> 68444 bytes,
#: ``wire_size`` on ``2026-08-13T220624Z-en-cn``). B171: this read 215990 -> 65110, both
#: measured on a hand-assembled subset envelope that under-counts the frame by 4141 bytes.
_REF_HEAVY = dict(rows=100, refs=4, ref_bytes=200)

#: Rows whose own metadata still overruns the frame after every ref is shed, so the
#: second step of the plan has to fire.
_ROW_HEAVY = dict(rows=100, refs=4, ref_bytes=200, name_bytes=2000)


# --- §V120 (a): shrink the knob-bounded part; do not withhold the answer ---------


def test_over_cap_with_a_plan_answers_instead_of_withholding() -> None:
    # B167 verbatim: the payload is over the frame cap and the request carries a
    # page_size that bounds it, so the client gets rows rather than an empty ``data``.
    raw = _payload(**_REF_HEAVY)
    assert wire_size(ResponseEnvelope(status="ok", data=raw)) > MAX_RESPONSE_BYTES

    env = _banners_ok(raw)
    assert env.data != {}
    assert env.data["banners"]
    assert wire_size(env) <= MAX_RESPONSE_BYTES


def test_shed_output_is_under_the_cap_on_both_steps() -> None:
    # Whatever the plan does, the emitted frame is bounded -- §V22 is not traded away
    # for §V120; the shed is how the cap is met, not an exemption from it.
    for shape in (_REF_HEAVY, _ROW_HEAVY):
        env = _banners_ok(_payload(**shape))
        assert wire_size(env) <= MAX_RESPONSE_BYTES, shape


# --- §V120 (b): ordered, heaviest first; applied at the one chokepoint -----------


def test_shed_order_is_refs_before_rows() -> None:
    # The counted order (refs are 55-71% of row bytes on every real page). A rows-first
    # plan would have trimmed this page to roughly a third of its rows and kept the refs;
    # refs-first keeps every row. This is the assertion that fails if _SHED_ORDER flips.
    raw = _payload(**_REF_HEAVY)
    env = _banners_ok(raw)

    assert len(env.data["banners"]) == _REF_HEAVY["rows"]
    assert all("image_refs" not in op for row in env.data["banners"] for op in row["featured_ops"])
    assert not any("banners are included" in limit for limit in env.limitations)


def test_declared_order_is_the_applied_order() -> None:
    # §V37: the declared plan and the steps that run are one home, so a plan cannot be
    # documented in one order and executed in another.
    assert tuple(step.part for step in _shed_plan()) == _SHED_ORDER


def test_shed_runs_at_the_chokepoint_and_leaves_the_callers_payload_alone() -> None:
    # §V120 (b): the shed is the envelope builder's, not a measure-and-trim copy in the
    # service -- the dict the caller handed in still holds everything it did.
    raw = _payload(**_REF_HEAVY)
    _banners_ok(raw)
    assert "image_refs_base_url" in raw
    assert all("image_refs" in op for row in raw["banners"] for op in row["featured_ops"])


def test_a_page_that_fits_is_not_shed_at_all() -> None:
    # §V96 non-degenerate: a plan that shed unconditionally would pass every assertion
    # above. Under the cap, nothing moves -- refs, coupler and limitations all survive.
    env = _banners_ok(_payload(rows=5))
    assert env.status == "ok"
    assert env.data["image_refs_base_url"] == IMAGE_REFS_BASE_URL
    assert all("image_refs" in op for row in env.data["banners"] for op in row["featured_ops"])
    assert env.limitations == (IMAGE_REFS_LIMITATION,)


# --- §V120 (c): name what left + the knob that returns it; keep the count honest --


def test_shed_limitation_names_the_part_and_the_knob() -> None:
    # §V108 routing: a client reading only this response must learn both that image
    # references are missing and which knob brings them back.
    env = _banners_ok(_payload(**_REF_HEAVY))
    shed = [limit for limit in env.limitations if "page_size" in limit]
    assert shed, env.limitations
    assert any("mage reference" in limit for limit in shed)


def test_page_total_survives_the_shed() -> None:
    # A trimmed list whose ``total`` shrank with it reads as §V67 CONFIRMED-none -- "that
    # is all there is" -- which is the one thing the shed must not claim.
    for shape in (_REF_HEAVY, _ROW_HEAVY):
        env = _banners_ok(_payload(**shape))
        assert env.data["page"]["total"] == 4321, shape
        assert env.data["page"]["page_size"] == shape["rows"], shape


def test_row_shed_limitation_names_the_fitting_page_size() -> None:
    env = _banners_ok(_payload(**_ROW_HEAVY))
    kept = len(env.data["banners"])
    assert 0 < kept < _ROW_HEAVY["rows"]
    assert any(
        f"first {kept} of this page's {_ROW_HEAVY['rows']} banners" in limit
        for limit in env.limitations
    )
    assert any(f"page_size of {kept} or smaller" in limit for limit in env.limitations)


# --- §V63/§V67: the ref coupler is one predicate, shed or kept as a whole --------


def test_ref_shed_retires_base_url_legend_and_limitation() -> None:
    # Leaving these behind ships a base URL for paths that are gone, a legend for labels
    # nothing carries, and a caveat about links the response does not contain.
    env = _banners_ok(_payload(**_REF_HEAVY))
    assert "image_refs_base_url" not in env.data
    assert "image_refs_legend" not in env.data
    assert IMAGE_REFS_LIMITATION not in env.limitations


def test_refs_leave_the_whole_page_never_half_of_it() -> None:
    # §V67: an ``image_refs`` key absent on one row and present on another would mean
    # "this operator has no derived art" in one place and "the response dropped it" in
    # the other. So the part is shed whole.
    env = _banners_ok(_payload(**_ROW_HEAVY))
    carriers = ["image_refs" in op for row in env.data["banners"] for op in row["featured_ops"]]
    assert carriers and not any(carriers)


# --- §V120 (d): the status split -------------------------------------------------


def test_paginated_shed_stays_ok() -> None:
    # A smaller page is a smaller LEGAL window (§V106 set-query language), so it is not a
    # degraded result -- and T216 rammed exactly this case into ``partial``.
    for shape in (_REF_HEAVY, _ROW_HEAVY):
        assert _banners_ok(_payload(**shape)).status == "ok", shape


def test_a_flagged_section_shed_reports_partial() -> None:
    # The other half of the split, proven on the mechanism: a client that named a section
    # and did not get it is told so. ``get_banners`` declares no such step, so this drives
    # the shed directly rather than asserting a branch no tool reaches today.
    section = ShedStep(
        part="map_image",
        paginated=False,
        depths=lambda frame: 1,
        apply=lambda frame, depth: ShedFrame(
            payload={k: v for k, v in frame.payload.items() if k != "map_image"},
            limitations=(*frame.limitations, "the map image was left out"),
        ),
    )
    env = ok({"map_image": "s" * 120_000, "stage_code": "4-4"}, shed_plan=(section,))
    assert env.status == "partial"
    assert env.data["stage_code"] == "4-4"
    assert "map_image" not in env.data


# --- §V22: the fail-closed floor is intact --------------------------------------


def test_a_payload_no_knob_bounds_still_withholds() -> None:
    # No plan -> the §V22 behaviour is exactly what it was: nothing oversized is emitted.
    env = ok({"blob": "x" * 120_000}, provenance=[_prov()])
    assert env.status == "partial"
    assert dict(env.data) == {}
    assert any("cap" in limit for limit in env.limitations)
    assert wire_size(env) <= MAX_RESPONSE_BYTES


def test_a_plan_that_cannot_fit_still_withholds() -> None:
    # One row bigger than the whole cap: the refs go, the rows cannot (a page of one has
    # nothing left to trim that is still an answer), so the plan runs out and the floor
    # takes over rather than emitting an oversized frame.
    env = _banners_ok(_payload(rows=1, refs=4, ref_bytes=200, name_bytes=150_000))
    assert env.status == "partial"
    assert dict(env.data) == {}
    assert wire_size(env) <= MAX_RESPONSE_BYTES


# --- the mechanism's own contract ------------------------------------------------


def test_row_step_is_reachable_by_construction() -> None:
    # §V113 (b): the second step of the plan does not fire on any live page today (the
    # ref shed alone closes every real shape), so its reachability is proven here rather
    # than declared -- an unfirable step is an absent step.
    env = _banners_ok(_payload(**_ROW_HEAVY))
    assert len(env.data["banners"]) < _ROW_HEAVY["rows"]


def test_shed_picks_the_shallowest_depth_that_fits() -> None:
    # The binary search must return the FIRST fitting depth, not merely a fitting one --
    # a search that overshot would drop rows a smaller trim would have kept.
    applied: list[int] = []

    def apply(frame: ShedFrame, depth: int) -> ShedFrame:
        applied.append(depth)
        return ShedFrame(payload={"n": 20 - depth}, limitations=frame.limitations)

    step = ShedStep(part="rows", paginated=True, depths=lambda frame: 20, apply=apply)
    result = shed_to_fit(
        ShedFrame(payload={"n": 21}, limitations=()),
        (step,),
        lambda frame: int(frame.payload["n"]) <= 13,  # type: ignore[call-overload]
    )
    assert result is not None
    frame, section_shed = result
    assert frame.payload == {"n": 13}
    assert section_shed is False
    assert len(applied) <= 6  # log2(20) probes, not a 20-step linear walk


def test_shed_skips_a_step_with_nothing_to_shed() -> None:
    # A page carrying no refs offers the ref step no depth; the plan moves on instead of
    # emitting a limitation about links that were never there.
    empty = ShedStep(part="refs", paginated=True, depths=lambda frame: 0, apply=lambda f, d: f)
    trim = ShedStep(
        part="rows",
        paginated=True,
        depths=lambda frame: 1,
        apply=lambda frame, depth: ShedFrame(payload={"n": 1}, limitations=("trimmed",)),
    )
    result = shed_to_fit(ShedFrame(payload={"n": 9}, limitations=()), (empty, trim), lambda f: True)
    assert result == (ShedFrame(payload={"n": 1}, limitations=("trimmed",)), False)


def test_shed_returns_none_when_the_plan_is_exhausted() -> None:
    step = ShedStep(
        part="rows",
        paginated=True,
        depths=lambda frame: 3,
        apply=lambda frame, depth: ShedFrame(payload={"n": 3 - depth}, limitations=()),
    )
    frame = ShedFrame(payload={"n": 9}, limitations=())
    assert shed_to_fit(frame, (step,), lambda f: False) is None
    assert shed_to_fit(frame, (), lambda f: True) is None


def test_steps_are_cumulative_across_the_plan() -> None:
    # A later step starts from everything the earlier one could shed, so a plan is one
    # descent rather than a set of independent attempts.
    first = ShedStep(
        part="a",
        paginated=True,
        depths=lambda frame: 1,
        apply=lambda frame, depth: ShedFrame(
            payload={k: v for k, v in frame.payload.items() if k != "a"},
            limitations=(*frame.limitations, "a left"),
        ),
    )
    second = ShedStep(
        part="b",
        paginated=True,
        depths=lambda frame: 1,
        apply=lambda frame, depth: ShedFrame(
            payload={k: v for k, v in frame.payload.items() if k != "b"},
            limitations=(*frame.limitations, "b left"),
        ),
    )
    result = shed_to_fit(
        ShedFrame(payload={"a": 1, "b": 2, "c": 3}, limitations=()),
        (first, second),
        lambda frame: set(frame.payload) == {"c"},
    )
    assert result is not None
    assert result[0] == ShedFrame(payload={"c": 3}, limitations=("a left", "b left"))
