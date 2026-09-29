"""Real-build guard -- every routing pointer actually leads somewhere.

The unit tests prove a coarse observation CARRIES a routing sentence and that the
flag it names is a real ``get_stage`` input. Neither proves the promise is kept: a
limitation that says "call get_stage with include_routes for this stage" is a worse
answer than silence if that call comes back empty, because the client has now been sent
somewhere and told the data is there. The harm was a client acting on what a
limitation implied; a dangling pointer is the same harm with an extra hop.

So this walks the real corpus and checks the pointer against the tool it names. Counted
on the promoted ``2026-07-30T010030Z`` build:

* ``threat.lane_route`` fires on **2453 en / 2484 cn** stages, and ``include_routes``
  returns a non-empty routes page on **all of them**;
* ``threat.pressure_spike``'s windowed arm fires on **1124 en / 1149 cn**, and
  ``include_spawns`` returns a non-empty spawn page on all of them;
* ``threat.tiles_deploy`` fires on **241 en / 247 cn**, and ``include_map`` returns a
  tile grid on all of them.

Also pinned: the two limitations that are NOT routing cases. ``ranged_arts``'s
"attack_range missing" (13 en / 13 cn enemies) and ``def_res_skew``'s "res missing"
(5 both) name a TRUE source absence -- the column is NULL on every level variant of
those enemies, so ``get_enemy`` would show the same gap and routing there would be a
false promise. That distinction is what stops a future sweep from "fixing" them by
bolting a pointer onto an absence no view can fill.

Deliberately NOT in ``ci.yml``'s enumerated live-upstream module list: it needs no
network, so it is not a ``live_upstream_disabled`` module and the whole-suite run at
``ci.yml:45`` already covers it (the existing precedent). Skipped when no build is
promoted; the unit tests carry the behaviour, so a logic edit still fails offline.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.services.stages import analyze_stage, get_stage

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "data" / "current.json"


def _active_build() -> Path | None:
    if not MANIFEST.is_file():
        return None
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    filename = manifest.get("database_filename")
    if not filename:
        return None
    path = REPO_ROOT / "data" / "builds" / str(filename)
    return path if path.is_file() else None


BUILD = _active_build()

pytestmark = pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)

#: rule_id -> (the include_ flag its limitation names, floor on how often it fires).
#: The floors are ~90% of the counted figures: a real corpus edit may move them, a
#: regression that stops the rule firing (and so stops the guard checking anything)
#: cannot hide under them.
_ROUTED_RULES = {
    "threat.lane_route": ("include_routes", 2200),
    "threat.pressure_spike": ("include_spawns", 1000),
    "threat.tiles_deploy": ("include_map", 200),
}


@pytest.fixture(scope="module")
def conn() -> sqlite3.Connection:
    assert BUILD is not None
    connection = sqlite3.connect(f"file:{BUILD}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _routed_stages(conn: sqlite3.Connection, server: str) -> dict[str, list[str]]:
    """``rule_id -> [stage game_id]`` for every observation carrying a get_stage route."""
    rows = conn.execute("SELECT game_id FROM stages WHERE server = ?", (server,))
    stage_ids = [str(row[0]) for row in rows]
    routed: dict[str, list[str]] = {rule: [] for rule in _ROUTED_RULES}
    for game_id in stage_ids:
        result = analyze_stage(conn, server=server, stage_code=None, game_id=game_id)
        for obs in result.observations:
            if obs.rule_id not in routed:
                continue
            if any("get_stage" in lim for lim in obs.limitations):
                routed[obs.rule_id].append(game_id)
    return routed


@pytest.fixture(scope="module")
def routed_en(conn: sqlite3.Connection) -> dict[str, list[str]]:
    return _routed_stages(conn, "en")


@pytest.fixture(scope="module")
def routed_cn(conn: sqlite3.Connection) -> dict[str, list[str]]:
    return _routed_stages(conn, "cn")


# --- the premise: the routed population is large ------------------------------


@pytest.mark.parametrize("rule_id", sorted(_ROUTED_RULES))
def test_routed_population_is_non_degenerate(
    routed_en: dict[str, list[str]], routed_cn: dict[str, list[str]], rule_id: str
) -> None:
    # A rule that stopped firing would make every reachability assertion below
    # pass over an empty list.
    _, floor = _ROUTED_RULES[rule_id]
    assert len(routed_en[rule_id]) >= floor, f"en {rule_id}: {len(routed_en[rule_id])}"
    assert len(routed_cn[rule_id]) >= floor, f"cn {rule_id}: {len(routed_cn[rule_id])}"


# --- the promise: the named call comes back non-empty -------------------------


def _section_present(conn: sqlite3.Connection, server: str, game_id: str, flag: str) -> bool:
    """Does the routed-to ``get_stage`` call actually return the promised section?"""
    kwargs: dict[str, object] = {flag: True}
    if flag == "include_routes":
        kwargs |= {"routes_page": 1, "routes_page_size": 1}
    if flag == "include_spawns":
        kwargs |= {"spawns_page": 1, "spawns_page_size": 1}
    detail = get_stage(conn, server=server, stage_code=None, game_id=game_id, **kwargs)  # type: ignore[arg-type]
    if flag == "include_routes":
        return detail.routes_page is not None and detail.routes_page.total > 0
    if flag == "include_spawns":
        return detail.spawns_page is not None and detail.spawns_page.total > 0
    return detail.tile_grid is not None and bool(detail.tile_grid.rows)


@pytest.mark.parametrize("server", ["en", "cn"])
@pytest.mark.parametrize("rule_id", sorted(_ROUTED_RULES))
def test_every_routed_stage_reaches_a_non_empty_section(
    conn: sqlite3.Connection,
    routed_en: dict[str, list[str]],
    routed_cn: dict[str, list[str]],
    server: str,
    rule_id: str,
) -> None:
    # The limitation tells the client the fuller view is one call away. On the
    # promoted build that is true for every stage it says it to -- 2453/2453 en and
    # 2484/2484 cn for routes, 1124/1124 and 1149/1149 for spawns, 241/241 and 247/247
    # for the tile grid. A single empty landing is a limitation that lied.
    flag, _ = _ROUTED_RULES[rule_id]
    routed = routed_en if server == "en" else routed_cn
    empty = [
        game_id for game_id in routed[rule_id] if not _section_present(conn, server, game_id, flag)
    ]
    assert empty == [], f"{server} {rule_id} routes to an empty {flag} on: {empty[:8]}"


# --- the boundary: a true source absence is NOT routed -------------------------


@pytest.mark.parametrize("server", ["en", "cn"])
@pytest.mark.parametrize(
    ("game_id", "column"),
    [
        ("enemy_1085_sotiwz", "attack_range"),
        ("enemy_1331_cbsisy", "attack_range"),
        ("enemy_1330_cbrush", "res"),
        ("enemy_3005_lpeopl", "res"),
    ],
)
def test_absent_stat_is_absent_on_every_level_variant(
    conn: sqlite3.Connection, server: str, game_id: str, column: str
) -> None:
    # The counterpart to the routing rule: these enemies drive a "<stat> missing"
    # limitation, and the stat is NULL on EVERY level variant, so no view of this server
    # holds it. Routing them at get_enemy would send the client to the same NULL --
    # the rule asks for a fuller view, not for a pointer where none exists. If a future
    # import fills the column, this fails and the limitation gets re-examined.
    rows = conn.execute(
        f"SELECT el.{column} FROM enemy_levels el JOIN enemies e ON e.enemy_pk = el.enemy_pk "
        "WHERE e.server = ? AND e.game_id = ?",
        (server, game_id),
    ).fetchall()
    assert rows, f"{server} {game_id} not in build"
    assert all(row[0] is None for row in rows), f"{server} {game_id}.{column} is populated"
