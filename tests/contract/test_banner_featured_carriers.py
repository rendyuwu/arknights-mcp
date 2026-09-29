"""The featured-op carriers, RECOUNTED against real data.

Six declared ``gachaRuleType`` values were carriers of
``dynMeta.attainRare6CharList`` and put a number beside them ("array, 83 EN"). Both halves
were wrong, and no test could see it: every banner fixture in the suite hand-writes the
array onto the pool it wants featured, so the parser looked right on exactly the rule types
the real data leaves empty. The wire then said ``featured_ops: []`` for a FESCLASSIC
celebration banner -- a CONFIRMED-none asserting a real rate-up banner has no rate-up
operator (the uncounted-claim class, inside the declared list's own verified-2026-07-21
numbers).

So this guard counts, on both sides of the pipeline:

* over the PROMOTED build (the rows the tools actually answer from): the per-rule-type
  cross-tab, pinned; the non-degeneracy of the partition the emit side now draws (a
  real pool on EVERY side, else the classifier ships as a filter that never fires); and a
  round trip through the real ``get_banners`` service asserting a carrier pool with no
  array omits the key and names its rule type in a limitation;
* over the PINNED upstream JSON (live, CI-only): that an EMPTY ``attainRare6CharList``
  occurs on ZERO pools in either region. That is the fact licensing the whole emit rule --
  it is what makes "zero featured rows" mean "not in source" (key ABSENT) rather than
  "confirmed none" (``[]``). The day upstream ships an empty array, the two stop being the
  same thing and this fails, which is the point. The build cannot answer it: it stores
  featured-op ROWS, so absent and empty arrive there identically.

Nothing fetched is persisted (code-only distribution).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.support import (
    LIVE_UPSTREAM_SKIP_REASON,
    arknights_assets_base_url,
    fetch_upstream_bytes,
    live_upstream_disabled,
)

from arknights_mcp.importers.banners import _CLASSIC_FAMILY_RULE_TYPES, _LIMITED_RULE_TYPE
from arknights_mcp.services.banners import (
    EXPECTED_FEATURED_OP_RULE_TYPES,
    NO_TYPED_FEATURED_OP_RULE_TYPES,
    absent_featured_op_array_limitation,
    get_banners,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "data" / "current.json"

GACHA_TABLE_PATH = "gamedata/excel/gacha_table.json"

#: The RECOUNT: ``(region, rule_type) -> (pools, pools carrying >=1 featured op, rows)``,
#: measured over the promoted build and matching the pinned upstream pool-for-pool. This
#: table IS the correction -- four of the six declared carriers
#: (CLASSIC/CLASSIC_DOUBLE/FESCLASSIC/SPECIAL) carry nothing at all, and ``BACKFLOW`` is a
#: twelfth cn rule type the declared list never named.
_CROSS_TAB: dict[tuple[str, str], tuple[int, int, int]] = {
    ("en", "ATTAIN"): (5, 5, 162),
    ("en", "CLASSIC_ATTAIN"): (3, 3, 100),
    ("en", "LIMITED"): (24, 24, 24),
    ("en", "CLASSIC"): (39, 0, 0),
    ("en", "CLASSIC_DOUBLE"): (19, 0, 0),
    ("en", "FESCLASSIC"): (12, 0, 0),
    ("en", "SPECIAL"): (5, 0, 0),
    ("en", "NORMAL"): (222, 0, 0),
    ("en", "SINGLE"): (30, 0, 0),
    ("en", "DOUBLE"): (24, 0, 0),
    ("en", "LINKAGE"): (6, 0, 0),
    ("cn", "ATTAIN"): (5, 5, 167),
    ("cn", "CLASSIC_ATTAIN"): (3, 3, 95),
    ("cn", "LIMITED"): (25, 25, 25),
    ("cn", "CLASSIC"): (41, 0, 0),
    ("cn", "CLASSIC_DOUBLE"): (30, 0, 0),
    ("cn", "FESCLASSIC"): (13, 0, 0),
    ("cn", "SPECIAL"): (7, 0, 0),
    ("cn", "BACKFLOW"): (1, 0, 0),
    ("cn", "NORMAL"): (232, 0, 0),
    ("cn", "SINGLE"): (35, 0, 0),
    ("cn", "DOUBLE"): (37, 0, 0),
    ("cn", "LINKAGE"): (8, 0, 0),
}


def _active_build() -> Path | None:
    """The promoted build's path, or ``None`` when nothing is promoted."""
    if not MANIFEST.is_file():
        return None
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    filename = manifest.get("database_filename")
    if not filename:
        return None
    path = REPO_ROOT / "data" / "builds" / str(filename)
    return path if path.is_file() else None


BUILD = _active_build()

needs_build = pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)
needs_upstream = pytest.mark.skipif(live_upstream_disabled(), reason=LIVE_UPSTREAM_SKIP_REASON)


@pytest.fixture(scope="module")
def conn() -> sqlite3.Connection:
    assert BUILD is not None
    return sqlite3.connect(f"file:{BUILD}?mode=ro", uri=True)


def _cross_tab(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[int, int, int]]:
    """``(region, rule_type) -> (pools, pools with >=1 featured op, featured rows)``."""
    rows = conn.execute(
        "SELECT b.region, b.rule_type, COUNT(*), "
        "SUM(CASE WHEN f.n > 0 THEN 1 ELSE 0 END), COALESCE(SUM(f.n), 0) "
        "FROM banners b LEFT JOIN "
        "(SELECT banner_pk, COUNT(*) AS n FROM banner_featured_ops GROUP BY banner_pk) f "
        "ON f.banner_pk = b.banner_pk "
        "GROUP BY b.region, b.rule_type"
    ).fetchall()
    return {
        (region, rule_type): (pools, with_ops, ops)
        for region, rule_type, pools, with_ops, ops in rows
    }


# --- the recount over the promoted build --------------------------------------


@needs_build
def test_featured_op_cross_tab_matches_the_recount(conn: sqlite3.Connection) -> None:
    """The pinned per-rule-type cross-tab (corrected against the recount).

    A drift here is either a real upstream change (re-pin the table and re-read the declared
    list) or an importer regression -- both need a human, which is why the numbers are
    pinned rather than merely compared against themselves.
    """
    assert _cross_tab(conn) == _CROSS_TAB


@needs_build
def test_no_expected_carrier_rule_type_is_missing_from_the_recount(
    conn: sqlite3.Connection,
) -> None:
    # Guard the guard: every declared carrier rule type must actually appear in the
    # corpus. A declared carrier with no pools at all would let the cross-tab above pass
    # while saying nothing about it.
    present = {rule_type for _, rule_type in _cross_tab(conn)}
    assert present >= EXPECTED_FEATURED_OP_RULE_TYPES
    assert present >= NO_TYPED_FEATURED_OP_RULE_TYPES


@needs_build
def test_the_emitted_partition_is_non_degenerate(conn: sqlite3.Connection) -> None:
    """A real pool on EVERY side of the split the emit side draws.

    Three sides, and the middle one is the whole reason this guard exists: it held 75 EN pools
    while the wire described them as standard banners.
    """
    tab = _cross_tab(conn)
    carries = [key for key, (_, with_ops, _) in tab.items() if with_ops]
    expected_carrier_without = [
        key
        for key, (pools, with_ops, _) in tab.items()
        if key[1] in EXPECTED_FEATURED_OP_RULE_TYPES and pools > with_ops
    ]
    standard = [key for key in tab if key[1] in NO_TYPED_FEATURED_OP_RULE_TYPES]
    assert carries, "no pool carries a typed featured op"
    assert expected_carrier_without, "no expected-carrier pool lacks its array"
    assert standard, "no standard-banner pool"


@needs_build
def test_the_service_classifier_mirrors_what_the_importer_reads(
    conn: sqlite3.Connection,
) -> None:
    # The emit side calls a pool an expected CARRIER on exactly the rule types the
    # importer extracts featured ops from. If the two drift, a pool the importer never reads
    # would be reported as a source gap (or worse, the reverse).
    assert _CLASSIC_FAMILY_RULE_TYPES | {_LIMITED_RULE_TYPE} == EXPECTED_FEATURED_OP_RULE_TYPES
    # And the two sets partition nothing in common: a rule type cannot both carry and not.
    assert not (EXPECTED_FEATURED_OP_RULE_TYPES & NO_TYPED_FEATURED_OP_RULE_TYPES)


@needs_build
def test_every_build_rule_type_is_classified_or_conservatively_named(
    conn: sqlite3.Connection,
) -> None:
    # An unknown token takes the conservative side + limitation: a rule type in neither set
    # -- ``BACKFLOW`` today -- must not be silently read as a prose-only standard banner. It
    # falls to the named absent-array arm, so it can never inherit a claim nobody checked.
    unclassified = {
        rule_type
        for _, rule_type in _cross_tab(conn)
        if rule_type not in EXPECTED_FEATURED_OP_RULE_TYPES
        and rule_type not in NO_TYPED_FEATURED_OP_RULE_TYPES
    }
    assert unclassified == {"BACKFLOW"}, unclassified


@needs_build
def test_a_carrier_pool_without_its_array_omits_the_key_and_names_its_rule_type(
    conn: sqlite3.Connection,
) -> None:
    """The round trip over the real corpus, not a fixture.

    Reads a real FESCLASSIC pool through the same service the MCP tool calls, and asserts
    the two halves of the fix together: the pool has no featured op (so the wire omits the
    key rather than sending a CONFIRMED-none ``[]``), and the limitation NAMES its rule
    type so the reason resolves per pool through that row's own ``rule_type``.
    """
    # Walk pages until a FESCLASSIC pool shows up; the archive is paged.
    fesclassic = None
    page = 1
    while True:
        result = get_banners(conn, server="en", page=page, page_size=100)
        fesclassic = next((b for b in result.banners if b.rule_type == "FESCLASSIC"), None)
        if fesclassic is not None or not result.page.has_more:
            break
        page += 1
    assert fesclassic is not None, "no FESCLASSIC pool in the en archive"
    assert fesclassic.featured_ops == ()
    named = [
        limitation
        for limitation in result.limitations
        if limitation.startswith("featured_ops is omitted")
    ]
    assert len(named) == 1, result.limitations
    assert "FESCLASSIC" in named[0]
    # The rule types named are exactly the ones on that page that need naming -- a static
    # list would keep claiming a gap after upstream filled it.
    on_page = tuple(
        sorted(
            {
                b.rule_type
                for b in result.banners
                if not b.featured_ops
                and b.rule_type is not None
                and b.rule_type not in NO_TYPED_FEATURED_OP_RULE_TYPES
            }
        )
    )
    assert named[0] == absent_featured_op_array_limitation(on_page)


# --- the fact the emit rule rests on, over PINNED upstream (CI-only) ----------


@needs_upstream
@pytest.mark.parametrize("server", ["en", "cn"])
def test_an_empty_featured_op_array_occurs_on_zero_pools(server: str) -> None:
    """An EMPTY ``attainRare6CharList`` never occurs upstream -- so absent is the only case.

    This is the load-bearing measurement behind emitting an ABSENT key for a pool with zero
    featured ops. If upstream ever ships ``attainRare6CharList: []``, that pool would be a
    genuine CONFIRMED-none and would need to keep ``featured_ops: []``, which the
    build alone can never distinguish (it stores rows, not the array's presence). Counted
    at the pin: every non-carrier pool has the KEY absent, or no ``dynMeta`` at all.
    """
    raw = fetch_upstream_bytes(f"{arknights_assets_base_url(server)}/{GACHA_TABLE_PATH}")
    pools = json.loads(raw)["gachaPoolClient"]
    assert pools, "upstream gachaPoolClient is empty; this guard would pass vacuously"
    empty_arrays = []
    non_list_arrays = []
    populated = 0
    for pool in pools:
        dyn_meta = pool.get("dynMeta")
        if not isinstance(dyn_meta, dict) or "attainRare6CharList" not in dyn_meta:
            continue
        value = dyn_meta["attainRare6CharList"]
        if not isinstance(value, list):
            non_list_arrays.append(pool.get("gachaPoolId"))
        elif value:
            populated += 1
        else:
            empty_arrays.append(pool.get("gachaPoolId"))
    assert empty_arrays == [], (
        "upstream now ships an EMPTY attainRare6CharList: those pools are a CONFIRMED none "
        f"and must keep featured_ops: [], unlike an absent array: {empty_arrays}"
    )
    assert non_list_arrays == [], non_list_arrays
    # Guard the guard: the key must still exist somewhere, or the loop above never ran.
    assert populated, "no pool carries a populated attainRare6CharList"


@needs_upstream
@pytest.mark.parametrize("server", ["en", "cn"])
def test_four_declared_carriers_carry_the_array_on_zero_pools(server: str) -> None:
    """The declared-carrier list vs the pin: four of six carry nothing.

    Asserted upstream as well as over the build, because the two failures differ. Here the
    array is absent from the SOURCE; over the build it could also mean the importer stopped
    reading it. Only both together say which.
    """
    raw = fetch_upstream_bytes(f"{arknights_assets_base_url(server)}/{GACHA_TABLE_PATH}")
    pools = json.loads(raw)["gachaPoolClient"]
    carrying: dict[str, int] = {}
    for pool in pools:
        dyn_meta = pool.get("dynMeta")
        if isinstance(dyn_meta, dict) and dyn_meta.get("attainRare6CharList"):
            rule_type = str(pool.get("gachaRuleType"))
            carrying[rule_type] = carrying.get(rule_type, 0) + 1
    assert carrying == {"ATTAIN": 5, "CLASSIC_ATTAIN": 3}, carrying
    empty_declared_carriers = _CLASSIC_FAMILY_RULE_TYPES - set(carrying)
    assert empty_declared_carriers == {"CLASSIC", "CLASSIC_DOUBLE", "FESCLASSIC", "SPECIAL"}
