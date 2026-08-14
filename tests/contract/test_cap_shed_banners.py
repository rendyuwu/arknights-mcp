"""T217 (a): §V120 on the corpus the harm was counted over (B167).

The unit guard beside this one drives synthetic payloads through the chokepoint. This
one drives the two calls a client actually made: ``get_banners`` en page 1 at
``page_size`` 100 and 90, which returned ``status: partial`` with ``data: {}`` after
T216 re-based the §V22 cap onto the full result frame -- while ``page_size<=80``
returned the same rows fine, so the data was reachable by a legal request and the
server withheld it anyway.

Both arms matter and neither replaces the other. Only the build says whether the shed
ORDER was counted right: a rows-first plan would also pass "the response is under the
cap and non-empty", and would ship a third of the page. And only the build carries a
window that sits just under the cap (cn page 2 at ``page_size=100``, 99.0%), which is
the control proving the plan does not fire on a page that fits.

Skipped without a promoted build, like every other real-corpus contract guard. Not
added to ``ci.yml``'s enumerated module list on purpose -- that list names the modules
guarded by the live-upstream job, and this needs no network (T201/T203/T212 precedent);
``ci.yml`` already runs the whole suite.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from tests.support.tool_calls import active_build, registry_for

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.mcp.envelopes import MAX_RESPONSE_BYTES, ResponseEnvelope, wire_size

BUILD = active_build()

pytestmark = pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)

#: The two shapes B167 counted as withheld, and the region/page they were counted on.
_WITHHELD_SHAPES = (("en", 1, 100), ("en", 1, 90))

#: The near-cap control: 99.0% of the frame cap on the promoted build, so it fits and
#: must come back untouched. A shed that ran unconditionally would strip this one too.
_FITTING_SHAPE = ("cn", 2, 100)


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
    # B167 verbatim: these two returned ``partial`` + ``data: {}``. Every row the window
    # asked for is now on the wire, under a status that says the answer is complete for
    # the window it describes (§V120 a/d).
    env = _call(conn, server, page, page_size)
    assert env.status == "ok"
    assert len(env.data["banners"]) == page_size
    assert wire_size(env) <= MAX_RESPONSE_BYTES


@pytest.mark.parametrize(("server", "page", "page_size"), _WITHHELD_SHAPES)
def test_the_shed_window_keeps_its_rows_and_sheds_its_refs(
    conn: sqlite3.Connection, server: str, page: int, page_size: int
) -> None:
    # §V120 (b) on the corpus the order was counted over: image refs are 55-71% of row
    # bytes on every real page, so shedding them fits the frame with every row intact.
    # A rows-first plan fits too -- by dropping about two thirds of the page.
    env = _call(conn, server, page, page_size)
    assert _refs(env) == 0
    assert "image_refs_base_url" not in env.data
    assert any("page_size" in limit for limit in env.limitations)


@pytest.mark.parametrize(("server", "page", "page_size"), _WITHHELD_SHAPES)
def test_the_shed_window_reports_the_same_total_as_a_window_that_fits(
    conn: sqlite3.Connection, server: str, page: int, page_size: int
) -> None:
    # §V120 (c): the shed touches the payload, never the count. A ``total`` that moved
    # with the shed would read as §V67 CONFIRMED-none.
    shed = _call(conn, server, page, page_size)
    unshed = _call(conn, server, page, 80)
    assert shed.data["page"]["total"] == unshed.data["page"]["total"]
    assert shed.data["page"]["page_size"] == page_size


@pytest.mark.parametrize(("server", "page", "page_size"), _WITHHELD_SHAPES)
def test_the_shed_window_returns_rows_a_smaller_page_size_already_serves(
    conn: sqlite3.Connection, server: str, page: int, page_size: int
) -> None:
    # §V120 (e): §V19 is protected by the bounded window and the absence of a walk, not
    # by an over-cap reply being empty. The shed hands back a prefix of the SAME page, so
    # every row it returns is one this caller may already request at page_size<=80.
    shed = _call(conn, server, page, page_size)
    fitting = _call(conn, server, page, 80)
    ids = [row["game_id"] for row in shed.data["banners"]]
    assert ids[:80] == [row["game_id"] for row in fitting.data["banners"]]


def test_a_window_that_fits_is_left_alone(conn: sqlite3.Connection) -> None:
    # §V96 non-degenerate: at 99.0% of the cap this is the tightest live window that
    # still fits, and it must come back with every ref -- a shed that fired on measure
    # alone, or a cap that drifted a few KB low, fails here.
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
    # driven at all before B167, which is why two of them shipped empty for a day.
    env = _call(conn, server, page, page_size)
    assert wire_size(env) <= MAX_RESPONSE_BYTES
    assert env.status == "ok"
    assert env.data["banners"] or env.data["page"]["total"] < (page - 1) * page_size + 1
