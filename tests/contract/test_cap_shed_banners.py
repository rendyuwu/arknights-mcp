"""Cap shed on the corpus the harm was counted over.

The unit guard beside this one drives synthetic payloads through the chokepoint. This one
drives the two calls a client actually made: ``get_banners`` en page 1 at ``page_size`` 100
and 90. Those returned ``status: partial`` with ``data: {}`` after the cap was re-based
onto the full result frame -- while ``page_size<=80`` returned the same rows fine, so
the data was reachable by a legal request and the server withheld it anyway. The rows came
back by shedding the image refs instead of the answer.

**The recovery finished.** Those pages were still incomplete: they came back without
their ``image_refs``, and en page 1 at ``page_size=100`` was the only reachable window that
lost them. Hoisting the per-ref ``source_id`` took that frame from 220131 to 173003 bytes
(110.1% -> 86.5% of cap), so the references ride the answer again. That is what these guards
now assert -- rows AND refs, not rows instead of refs -- and it is why the assertions here
inverted rather than being deleted: the shed firing on this shape would today mean the
economy regressed.

Only the build can say either thing. A rows-first plan would also pass "the response is
under the cap and non-empty" while shipping six rows of a hundred, and only the build
carries the window that used to sit at 99.0% of the cap (cn page 2 at ``page_size=100``,
now 79.1%) -- the control proving nothing sheds on a page that fits.

Skipped without a promoted build, like every other real-corpus contract guard. Not
added to ``ci.yml``'s enumerated module list on purpose -- that list names the modules
guarded by the live-upstream job, and this needs no network;
``ci.yml`` already runs the whole suite.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from tests.support.tool_calls import active_build, registry_for

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.mcp.envelopes import MAX_RESPONSE_BYTES, ResponseEnvelope, wire_size
from arknights_mcp.mcp.tools._shared import IMAGE_REFS_HOISTED_KEYS

BUILD = active_build()

pytestmark = pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)

#: The two shapes counted as withheld, and the region/page they were counted on.
#: Those shapes still lost their image refs after the first fix; once the refs came back,
#: the same two shapes carry the recovery guards.
_WITHHELD_SHAPES = (("en", 1, 100), ("en", 1, 90))

#: The tightest control: 79.1% of the frame cap on the promoted build (99.0% before),
#: so it fits and must come back untouched. A shed that ran unconditionally would strip this
#: one too.
_FITTING_SHAPE = ("cn", 2, 100)

#: Every reachable window at the maximum ``page_size``, region by region: four en pages and
#: five cn. Enumerated because the count is a COUNTED fact about the corpus and a
#: sweep that silently found three would report green on a third of the surface.
_REACHABLE_MAX_WINDOWS = 9


@pytest.fixture(scope="module")
def conn() -> Iterator[sqlite3.Connection]:
    assert BUILD is not None
    with open_read_only(BUILD) as connection:
        yield connection


def _call(conn: sqlite3.Connection, server: str, page: int, page_size: int) -> ResponseEnvelope:
    """One ``get_banners`` call with image refs enabled (the emitting posture)."""
    spec = registry_for(conn).get("get_banners")
    return spec.handler(server=server, page={"page": page, "page_size": page_size})


def _refs(envelope: ResponseEnvelope) -> int:
    rows = envelope.data.get("banners", [])
    return sum(
        len(op.get("image_refs", ()))
        for row in rows  # type: ignore[union-attr]
        for op in row.get("featured_ops", ())
    )


@pytest.mark.parametrize(("server", "page", "page_size"), _WITHHELD_SHAPES)
def test_the_withheld_windows_answer(
    conn: sqlite3.Connection, server: str, page: int, page_size: int
) -> None:
    # Verbatim: these two returned ``partial`` + ``data: {}``. Every row the window
    # asked for is now on the wire, under a status that says the answer is complete for
    # the window it describes.
    env = _call(conn, server, page, page_size)
    assert env.status == "ok"
    assert len(env.data["banners"]) == page_size
    assert wire_size(env) <= MAX_RESPONSE_BYTES


@pytest.mark.parametrize(("server", "page", "page_size"), _WITHHELD_SHAPES)
def test_the_recovered_windows_keep_their_refs(
    conn: sqlite3.Connection, server: str, page: int, page_size: int
) -> None:
    # Verbatim, in the direction the second fix set. The first fix got these two windows
    # their rows back by shedding the image refs; en page 1 at page_size=100 was the ONE
    # reachable window that answered ref-less, and hoisting the per-ref source_id (110.1% ->
    # 86.5% of cap) is what put the references back on the wire. So the shed must NOT fire
    # here: the refs are present, the whole coupler rides along, and no limitation mentions
    # page_size.
    #
    # This assertion is the inverse of the one it replaces, deliberately. The shed firing on
    # this shape again would mean the frame crossed the cap once more -- which is news, not a
    # detail, and is exactly what the old assertion would have hidden by expecting it.
    env = _call(conn, server, page, page_size)
    assert _refs(env) > 0
    for key in IMAGE_REFS_HOISTED_KEYS:
        assert key in env.data, key
    assert not any("page_size" in limit for limit in env.limitations)


@pytest.mark.parametrize(("server", "page", "page_size"), _WITHHELD_SHAPES)
def test_the_shed_window_reports_the_same_total_as_a_window_that_fits(
    conn: sqlite3.Connection, server: str, page: int, page_size: int
) -> None:
    # The shed touches the payload, never the count. A ``total`` that moved
    # with the shed would read as CONFIRMED-none.
    shed = _call(conn, server, page, page_size)
    unshed = _call(conn, server, page, 80)
    assert shed.data["page"]["total"] == unshed.data["page"]["total"]
    assert shed.data["page"]["page_size"] == page_size


@pytest.mark.parametrize(("server", "page", "page_size"), _WITHHELD_SHAPES)
def test_the_shed_window_returns_rows_a_smaller_page_size_already_serves(
    conn: sqlite3.Connection, server: str, page: int, page_size: int
) -> None:
    # Page bounds are protected by the bounded window and the absence of a walk, not
    # by an over-cap reply being empty. The shed hands back a prefix of the SAME page, so
    # every row it returns is one this caller may already request at page_size<=80.
    shed = _call(conn, server, page, page_size)
    fitting = _call(conn, server, page, 80)
    ids = [row["game_id"] for row in shed.data["banners"]]
    assert ids[:80] == [row["game_id"] for row in fitting.data["banners"]]


def test_a_window_that_fits_is_left_alone(conn: sqlite3.Connection) -> None:
    # Non-degenerate: this is the tightest live window on the build -- 79.1% of the cap
    # since the fix, 99.0% before it -- and it must come back with every ref. A shed that fired
    # on measure alone, or a cap that drifted a few KB low, fails here.
    server, page, page_size = _FITTING_SHAPE
    env = _call(conn, server, page, page_size)
    assert env.status == "ok"
    assert _refs(env) > 0
    assert "image_refs_base_url" in env.data
    assert not any("page_size" in limit for limit in env.limitations)


@pytest.mark.parametrize("server", ["en", "cn"])
@pytest.mark.parametrize("page_size", [1, 10, 50, 80, 90, 100])
@pytest.mark.parametrize("page", [1, 2, 3])
def test_every_reachable_banner_window_is_under_the_cap_and_answers(
    conn: sqlite3.Connection, server: str, page: int, page_size: int
) -> None:
    # The sweep the cap tests never ran: no max-page_size window over the real corpus was
    # driven at all before, which is why two of them shipped empty for a day.
    env = _call(conn, server, page, page_size)
    assert wire_size(env) <= MAX_RESPONSE_BYTES
    assert env.status == "ok"
    assert env.data["banners"] or env.data["page"]["total"] < (page - 1) * page_size + 1


def test_every_reachable_max_window_is_under_the_cap_with_its_refs(
    conn: sqlite3.Connection,
) -> None:
    # The economy claim, on every window rather than on the shape it was counted on
    # (one counted member is what made the scope claim wrong about two others).
    # Walks page 1 to the end of both regions at page_size=100, the widest legal window, and
    # requires each page to be under the cap AND still carrying its refs.
    #
    # The exception is stated rather than tolerated: a page whose featured ops all failed to
    # resolve emits no refs at all, so it emits no hoisted key either -- cn's last
    # page is one. That is an absence of refs, not a shed, and the two are told apart by the
    # shed limitation, which is the only thing that would name page_size.
    seen = 0
    for server in ("en", "cn"):
        for page in range(1, 100):
            env = _call(conn, server, page, 100)
            seen += 1
            where = f"{server} page {page}"
            assert env.status == "ok", where
            assert wire_size(env) <= MAX_RESPONSE_BYTES, where
            assert not any("page_size" in limit for limit in env.limitations), where
            carries_refs = _refs(env) > 0
            for key in IMAGE_REFS_HOISTED_KEYS:
                assert (key in env.data) is carries_refs, (where, key)
            if not env.data["page"]["has_more"]:
                break
    assert seen == _REACHABLE_MAX_WINDOWS, (
        f"walked {seen} windows at page_size=100; {_REACHABLE_MAX_WINDOWS} were counted on "
        "the promoted build. Re-count -- the corpus grew or shrank a page"
    )
