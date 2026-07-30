"""T200: an emitted `range_id` resolves to a real grid (§V98/§V69/§V96; B132).

``range_id`` ("3-1", "x-4") rides every operator phase and every skill level. It shipped
bare -- no resolver, no limitation -- so "what is this skill's range" was unanswerable
from the response, and the registry made it worse by DECLARING ``range_table.json`` in
``fields_consumed`` while no sync fetched it and no importer read it (§V98).

Three groups, because the defect had three separable halves:

* **upstream** -- the file really exists at the pin, in the shape the parser assumes,
  and ``direction`` really is a single constant (so dropping it is a counted fact, not
  an assumption -- §V96/§V112 (c));
* **build** -- every ``range_id`` the promoted corpus emits resolves against its OWN
  region's table, counted rather than assumed (§V96 non-degenerate);
* **wire** -- the grid arrives beside the id through the real tool.

The counts matter more than the shapes here. A resolver that resolves nothing is the
silent no-op §V96 was written for: it would ship, every synthetic test would pass, and
``range_id`` would still be bare on every real response.

The upstream group is CI-only (``ARKMCP_LIVE_UPSTREAM``); nothing fetched is persisted
(§V16).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

import pytest
from tests.support import (
    LIVE_UPSTREAM_SKIP_REASON,
    arknights_assets_base_url,
    fetch_upstream_bytes,
    live_upstream_disabled,
)
from tests.support.tool_calls import active_build, registry_for

from arknights_mcp.importers.field_policy import RANGE_ALLOWLIST
from arknights_mcp.importers.ranges import RANGE_TABLE_PATH, parse_ranges

SERVERS = ("en", "cn")

requires_upstream = pytest.mark.skipif(live_upstream_disabled(), reason=LIVE_UPSTREAM_SKIP_REASON)

BUILD = active_build()
requires_build = pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)

#: §V96 non-degenerate floors, well under the counts at the pin (68 EN / 73 CN entries;
#: 56 EN / 57 CN distinct ids in use on the promoted build). Floors, not equalities: an
#: upstream that adds a range must not fail this, an upstream that empties one must.
MIN_RANGE_ENTRIES = 50
MIN_DISTINCT_IDS_IN_USE = 40

#: Amiya: her phases name 1-1/1-2 and Chain Cast names x-1, all three real at the pin.
AMIYA = "char_002_amiya"


@lru_cache(maxsize=len(SERVERS))
def _upstream_table(server: str) -> dict[str, Any]:
    """The pinned ``range_table`` for ``server``; fetched once, never written (§V16)."""
    url = f"{arknights_assets_base_url(server)}/{RANGE_TABLE_PATH}"
    table = json.loads(fetch_upstream_bytes(url).decode("utf-8"))
    assert isinstance(table, dict) and table, f"{server} range_table is not a populated dict"
    return table


# --- upstream: the file the registry declared really is there, in this shape ----


@requires_upstream
@pytest.mark.parametrize("server", SERVERS)
def test_upstream_entries_have_the_parsed_shape(server: str) -> None:
    """§V29: ``{id, direction, grids: [{row, col}]}``, keyed by the id a phase stores."""
    raw = _upstream_table(server)
    assert len(raw) >= MIN_RANGE_ENTRIES, f"{server}: only {len(raw)} range entries"
    for key, entry in raw.items():
        assert entry["id"] == key, f"{server}: entry {key} disagrees with its own key"
        assert entry["grids"], f"{server}: {key} carries no grid cell"
        for cell in entry["grids"]:
            assert set(cell) == {"row", "col"}, f"{server}: {key} cell has unexpected keys"
            assert all(isinstance(v, int) for v in cell.values())


@requires_upstream
@pytest.mark.parametrize("server", SERVERS)
def test_direction_is_a_single_constant_so_dropping_it_loses_nothing(server: str) -> None:
    """§V96/§V112 (c): constant by DATA, not by construction -- so PIN it.

    ``direction`` is deliberately not imported (§V94: it would be a column no reader
    consumes, the dead substrate §V113/B160 caught). That is only safe while it carries
    one value. A second value would mean the field distinguishes something this build
    cannot express, and this fails loudly rather than dropping it silently.
    """
    directions = {entry["direction"] for entry in _upstream_table(server).values()}
    assert directions == {1}, f"{server}: range_table.direction is no longer constant: {directions}"
    assert "direction" not in RANGE_ALLOWLIST


@requires_upstream
@pytest.mark.parametrize("server", SERVERS)
def test_parser_keeps_every_upstream_entry(server: str) -> None:
    """No real entry is dropped: the skip paths are for malformed data, not real data."""
    raw = _upstream_table(server)
    parsed = parse_ranges(raw)
    assert {p.range_id for p in parsed} == set(raw)


@requires_upstream
def test_cn_is_a_superset_so_the_lookup_must_be_region_scoped() -> None:
    """§V5: CN carries ranges EN does not, so a region-blind resolve would cross regions.

    This is why the repository keys on ``(server, range_id)`` rather than falling back to
    another region's table when a lookup misses.
    """
    en, cn = set(_upstream_table("en")), set(_upstream_table("cn"))
    assert en < cn, "EN is no longer a strict subset of CN -- re-check the region scoping"


# --- build: every emitted id resolves, counted (§V96) ---------------------------


@pytest.fixture(scope="module")
def build_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{BUILD}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()


def _emitted_ids(conn: sqlite3.Connection, server: str) -> set[str]:
    """Every ``range_id`` the wire can carry for ``server``: phases plus skill LEVELS."""
    phases = conn.execute(
        "SELECT DISTINCT p.range_id FROM operator_phases p "
        "JOIN operators o ON o.operator_pk = p.operator_pk "
        "WHERE o.server = ? AND p.range_id IS NOT NULL",
        (server,),
    ).fetchall()
    levels = conn.execute(
        "SELECT DISTINCT l.range_id FROM skill_levels l "
        "JOIN skills k ON k.skill_pk = l.skill_pk "
        "WHERE k.server = ? AND l.range_id IS NOT NULL",
        (server,),
    ).fetchall()
    return {r[0] for r in (*phases, *levels)}


@requires_build
@pytest.mark.parametrize("server", SERVERS)
def test_every_emitted_range_id_resolves_on_the_build(
    build_conn: sqlite3.Connection, server: str
) -> None:
    """The pairing arm, counted: zero bare ids left on the real corpus (§V69/B132)."""
    emitted = _emitted_ids(build_conn, server)
    assert len(emitted) >= MIN_DISTINCT_IDS_IN_USE, f"{server}: only {len(emitted)} ids in use"
    stored = {
        r[0] for r in build_conn.execute("SELECT range_id FROM ranges WHERE server = ?", (server,))
    }
    assert len(stored) >= MIN_RANGE_ENTRIES, f"{server}: only {len(stored)} range rows imported"
    unresolved = emitted - stored
    assert not unresolved, (
        f"{server}: {len(unresolved)} emitted ids resolve to nothing: {unresolved}"
    )


@requires_build
@pytest.mark.parametrize("server", SERVERS)
def test_stored_grids_are_non_degenerate(build_conn: sqlite3.Connection, server: str) -> None:
    """§V96: a table of one-cell grids would resolve everything and mean nothing."""
    sizes = [
        len(json.loads(r[0]))
        for r in build_conn.execute("SELECT grids_json FROM ranges WHERE server = ?", (server,))
    ]
    assert sizes, f"{server}: no ranges imported"
    assert min(sizes) >= 1
    assert max(sizes) > 10, f"{server}: largest grid is only {max(sizes)} cells"
    assert len(set(sizes)) > 5, f"{server}: only {len(set(sizes))} distinct grid sizes"


@requires_build
def test_ranges_carry_provenance_and_never_cross_regions(build_conn: sqlite3.Connection) -> None:
    """§V17 + §V5: every row is attributable, and no row is region-less."""
    orphans = build_conn.execute(
        "SELECT count(*) FROM ranges r LEFT JOIN record_provenance p "
        "ON p.provenance_id = r.provenance_id WHERE p.provenance_id IS NULL"
    ).fetchone()[0]
    assert orphans == 0
    servers = {r[0] for r in build_conn.execute("SELECT DISTINCT server FROM ranges")}
    assert servers == {"en", "cn"}


# --- wire: the grid arrives beside the id ---------------------------------------


@requires_build
def test_wire_resolves_the_ids_it_emits(build_conn: sqlite3.Connection) -> None:
    """§V69: the id still ships (it is the joinable key) and the grid ships with it."""
    envelope = (
        registry_for(build_conn)
        .get("get_operator")
        .handler(server="en", game_id=AMIYA, include_phases=True, include_skills=True)
    )
    body = json.loads(json.dumps(envelope.to_dict()))
    operator = body["data"]["operator"]

    emitted = {p["range_id"] for p in operator["phases"] if "range_id" in p}
    emitted |= {
        lv["range_id"] for s in operator["skills"] for lv in s["levels"] if "range_id" in lv
    }
    assert emitted, "the operator emitted no range_id at all -- the fixture moved"

    entries = operator["ranges"]["entries"]
    assert emitted <= set(entries), f"unresolved on the wire: {emitted - set(entries)}"
    for range_id in emitted:
        entry = entries[range_id]
        assert entry["grids"], f"{range_id} resolved to an empty grid"
        assert entry["cell_count"] == len(entry["grids"])
        assert entry["rows"], f"{range_id} carries no board"
    # ...and no limitation claims a range is unavailable while its grid is right there.
    assert not any("attack-range grid unavailable" in t for t in body["limitations"])


@requires_build
def test_wire_symbols_decode_the_board(build_conn: sqlite3.Connection) -> None:
    """The board is only readable if the alphabet rides with it (§V66: hoisted once)."""
    envelope = (
        registry_for(build_conn)
        .get("get_operator")
        .handler(server="en", game_id=AMIYA, include_skills=True)
    )
    ranges = json.loads(json.dumps(envelope.to_dict()))["data"]["operator"]["ranges"]
    symbols = set(ranges["symbols"].values())
    assert len(symbols) == 4
    for entry in ranges["entries"].values():
        used = {ch for row in entry["rows"] for ch in row}
        assert used <= symbols, f"board uses characters the symbol map does not decode: {used}"
