"""T217 (b): §V120 on the corpus the harm was counted over (B169).

The unit guard beside this one drives synthetic payloads. This one drives the calls a
client actually makes: ``get_stage`` on ``cn/act1football_01`` with routes requested,
which returned ``status: partial`` and ``data: {}`` for every flag combination that asked
for them -- while a smaller ``routes_page.page_size`` returned the same routes fine. The
data was reachable by a legal request and the server withheld it anyway.

Only the build says whether the shed ORDER was counted right. §V120 (b) first illustrated
``get_stage`` with ``map_image`` then ``spawns`` then ``routes``, and that plan would also
pass "under the cap and non-empty" -- by shedding three parts and reporting ``partial``
where trimming routes alone fits at ``ok``. And only the build carries the control that
proves the plan does not fire on a response that fits: ``cn/act2multi_tr02`` sits at 95.5%
of the cap on a 90 KB image and must come back whole.

The sweep at the bottom is the test that never ran. B167 and B169 both shipped because no
guard drove a tool at its WIDEST legal request over the real corpus (§V121 a) -- the cap
tests all used defaults.

Skipped without a promoted build. Not added to ``ci.yml``'s enumerated module list on
purpose: that list names the modules the live-upstream job guards, and this needs no
network (T201/T203/T212 precedent); ``ci.yml`` already runs the whole suite.
"""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from tests.support.tool_calls import active_build, registry_for

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.mcp.envelopes import MAX_RESPONSE_BYTES, ResponseEnvelope, wire_size

BUILD = active_build()

pytestmark = pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)

#: The one stage on the promoted build whose max-detail response exceeds the §V22 frame
#: cap: 302656 bytes, 151.3% of it, and route-heavy (routes 67.6% of the payload, the
#: image 19.7%, the spawns 11.3%).
_OVER_CAP = ("cn", "act1football_01")

#: Its distinct-route total -- the number the trim must NOT rewrite (§V120 c).
_OVER_CAP_ROUTE_TOTAL = 116

#: The near-cap control: an image-dominant board at 95.5% of the cap, so it fits and must
#: come back untouched. A shed that ran on measure alone would strip this one too.
_FITTING = ("cn", "act2multi_tr02")

#: Every flag combination that asks for this stage's routes at the maximum page size. All
#: eight were withheld (208261-302656 bytes); B169 counted each one.
_WITHHELD_COMBOS = tuple(
    {
        "include_map": include_map,
        "include_routes": True,
        "include_spawns": include_spawns,
        "include_map_image": include_map_image,
    }
    for include_map, include_spawns, include_map_image in itertools.product((True, False), repeat=3)
)


@pytest.fixture(scope="module")
def conn() -> Iterator[sqlite3.Connection]:
    assert BUILD is not None
    with open_read_only(BUILD) as connection:
        yield connection


def _call(
    conn: sqlite3.Connection,
    server: str,
    game_id: str,
    *,
    routes_page_size: int = 100,
    spawns_page_size: int = 100,
    **flags: bool,
) -> ResponseEnvelope:
    spec = registry_for(conn).get("get_stage")
    params: dict[str, Any] = {
        "server": server,
        "game_id": game_id,
        "routes_page": {"page": 1, "page_size": routes_page_size},
        "spawns_page": {"page": 1, "page_size": spawns_page_size},
        **flags,
    }
    return spec.handler(**params)


def _max_detail(conn: sqlite3.Connection, server: str, game_id: str, **kw: int) -> ResponseEnvelope:
    return _call(
        conn,
        server,
        game_id,
        include_map=True,
        include_routes=True,
        include_spawns=True,
        include_map_image=True,
        **kw,
    )


@pytest.mark.parametrize("flags", _WITHHELD_COMBOS)
def test_the_withheld_stage_shapes_answer(conn: sqlite3.Connection, flags: dict[str, bool]) -> None:
    # B169 verbatim: each of these returned ``partial`` with ``data: {}``.
    env = _call(conn, *_OVER_CAP, **flags)
    assert env.data != {}
    assert env.data["stage"]["game_id"] == _OVER_CAP[1]
    assert wire_size(env) <= MAX_RESPONSE_BYTES


@pytest.mark.parametrize("flags", _WITHHELD_COMBOS)
def test_the_withheld_stage_shapes_stay_ok(
    conn: sqlite3.Connection, flags: dict[str, bool]
) -> None:
    # §V120 (d): every one of these sheds a PAGED section, so the answer is complete for
    # the window it describes. A plan that reached for the image would report ``partial``.
    env = _call(conn, *_OVER_CAP, **flags)
    assert env.status == "ok"


def test_the_shed_trims_routes_and_keeps_the_image_and_spawns(conn: sqlite3.Connection) -> None:
    # §V120 (b) on the corpus the order was counted over. The order §V120 (b) originally
    # illustrated would have shed the image and the spawns, still been over the cap, and
    # trimmed the routes anyway -- three parts gone instead of one.
    env = _max_detail(conn, *_OVER_CAP)
    assert "map_image" in env.data
    assert len(env.data["spawns"]) == 100
    assert 0 < len(env.data["routes"]) < 100


def test_the_shed_reports_the_same_route_total_as_a_window_that_fits(
    conn: sqlite3.Connection,
) -> None:
    # §V120 (c): the shed touches the payload, never the count.
    shed = _max_detail(conn, *_OVER_CAP)
    fitting = _max_detail(conn, *_OVER_CAP, routes_page_size=40)
    assert shed.data["routes_page"]["total"] == _OVER_CAP_ROUTE_TOTAL
    assert fitting.data["routes_page"]["total"] == _OVER_CAP_ROUTE_TOTAL
    assert shed.data["routes_page"]["page_size"] == 100


def test_the_shed_returns_routes_a_smaller_page_size_already_serves(
    conn: sqlite3.Connection,
) -> None:
    # §V120 (e): §V19 is protected by the bounded window and the absence of a walk, not by
    # an over-cap reply being empty. The trim hands back a prefix of the SAME page, so
    # every route it returns is one this caller may already request.
    shed = _max_detail(conn, *_OVER_CAP)
    kept = len(shed.data["routes"])
    fitting = _max_detail(conn, *_OVER_CAP, routes_page_size=kept)
    assert shed.data["routes"] == fitting.data["routes"]


def test_the_shed_limitation_names_the_knob(conn: sqlite3.Connection) -> None:
    env = _max_detail(conn, *_OVER_CAP)
    kept = len(env.data["routes"])
    assert any("routes_page.page_size" in limit and str(kept) in limit for limit in env.limitations)


def test_a_response_that_fits_is_left_alone(conn: sqlite3.Connection) -> None:
    # §V96 non-degenerate: at 95.5% of the cap this is the tightest live response that
    # still fits, and its 90 KB image must survive. A shed that fired on measure alone, or
    # a cap that drifted a few KB low, fails here.
    env = _max_detail(conn, *_FITTING)
    assert env.status == "ok"
    assert "map_image" in env.data
    assert not any("size limit" in limit for limit in env.limitations)


@pytest.mark.parametrize(
    "game_id",
    [
        "act1football_01",  # the over-cap shape
        "act2multi_tr02",  # image-heavy, 95.5%
        "camp_r_20",  # route-heavy, 79.6%
        "act49side_10",  # image-heavy with routes beside it, 77.4%
        "act42side_ex01",  # route-heavy, 58.1%
        "act1football_s01",  # route-heavy, 62.0%
    ],
)
@pytest.mark.parametrize("server", ["en", "cn"])
def test_the_heaviest_stages_answer_under_the_cap_at_max_detail(
    conn: sqlite3.Connection, server: str, game_id: str
) -> None:
    # The sweep no cap test had ever run: the widest legal request over the fattest real
    # frames. Both regions, because the two corpora do not carry the same stages.
    env = _max_detail(conn, server, game_id)
    if env.status == "not_found":
        pytest.skip(f"{game_id} not in {server}")
    assert wire_size(env) <= MAX_RESPONSE_BYTES
    assert env.data != {}
