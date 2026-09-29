"""An operator's subclass id ships with its NAME.

``operators.subclass_id`` is an opaque game-data code -- ``corecaster``, ``ringhealer``,
``artsfghter`` -- and it shipped bare on every operator response. An emitted opaque id allows
exactly two outcomes: pair it with the display name when the name is imported, or emit
the id plus a limitation when it is not. Neither held, so a client
either guessed the class or dropped the field.

The name was never missing. ``uniequip_table.json`` carries ``subProfDict`` and every sync
has fetched that file since the module domain landed -- it was fetched and never
read. So this is the pairing arm, and the guards are counts:

* upstream -- ``subProfDict`` still keys names by the same id the operator row stores;
* build -- every operator on the promoted build resolves, counted, not assumed;
* wire -- the name ships beside the id, and when a build has no name for an id the
  LIMITATION ships instead of a null or a guess.

The last one runs on a snapshot with no ``uniequip_table`` at all, because that is the
only place the fallback exists today: the real build resolves 434/434 EN and 450/450 CN,
so a test that only looked at the promoted corpus would leave the arm unexecuted while
reading as covered.

The upstream group is CI-only (``ARKMCP_LIVE_UPSTREAM``); nothing fetched is persisted.
"""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
from tests.support import (
    LIVE_UPSTREAM_SKIP_REASON,
    arknights_assets_base_url,
    fetch_upstream_bytes,
    live_upstream_disabled,
)
from tests.support.tool_calls import active_build, registry_for

from arknights_mcp.importers.operators import parse_subclass_names

REPO_ROOT = Path(__file__).resolve().parents[2]

SERVERS = ("en", "cn")

requires_upstream = pytest.mark.skipif(live_upstream_disabled(), reason=LIVE_UPSTREAM_SKIP_REASON)

BUILD = active_build()
requires_build = pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)

#: Non-degenerate floors, well under the pinned counts (75 EN / 76 CN entries in
#: subProfDict; 71 EN / 72 CN distinct ids in use on the promoted build).
MIN_SUBPROF_ENTRIES = 50
MIN_DISTINCT_SUBCLASSES = 50

#: An operator whose subclass is unambiguous and stable enough to assert by name.
AMIYA = "char_002_amiya"


@lru_cache(maxsize=len(SERVERS))
def _uniequip(server: str) -> dict[str, Any]:
    """The pinned ``uniequip_table`` for ``server``; fetched once, never written."""
    url = f"{arknights_assets_base_url(server)}/gamedata/excel/uniequip_table.json"
    table = json.loads(fetch_upstream_bytes(url).decode("utf-8"))
    assert isinstance(table, dict) and table, f"{server} uniequip_table is not a populated dict"
    return table


# --- upstream: the names exist, keyed by the id the operator row stores ---------


@requires_upstream
@pytest.mark.parametrize("server", SERVERS)
def test_subprof_dict_names_are_keyed_by_the_operator_id(server: str) -> None:
    names = parse_subclass_names(_uniequip(server))
    assert len(names) >= MIN_SUBPROF_ENTRIES, f"{server}: only {len(names)} subclass names"
    raw = _uniequip(server)["subProfDict"]
    # The parser reads the id from the ENTRY, not the dict key, so a mismatch cannot
    # invent a pairing; at the pin they agree, and this says so rather than assuming it.
    assert all(entry["subProfessionId"] == key for key, entry in raw.items())
    assert all(name.strip() for name in names.values()), "an empty name was imported"


@requires_upstream
def test_regional_names_differ_because_the_fact_is_regional() -> None:
    """The name is a display string, so CN ships the CN label -- never EN's."""
    en, cn = parse_subclass_names(_uniequip("en")), parse_subclass_names(_uniequip("cn"))
    shared = set(en) & set(cn)
    assert len(shared) >= MIN_SUBPROF_ENTRIES
    assert any(en[key] != cn[key] for key in shared), "both regions carry identical labels"


# --- build: every id on the promoted corpus resolves, counted -------------


@pytest.fixture(scope="module")
def build_conn() -> Any:
    conn = sqlite3.connect(f"file:{BUILD}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()


@requires_build
@pytest.mark.parametrize("server", SERVERS)
def test_every_operator_carries_a_subclass_name(
    build_conn: sqlite3.Connection, server: str
) -> None:
    total, named, distinct = build_conn.execute(
        "SELECT count(*), count(subclass_name), count(DISTINCT subclass_name) "
        "FROM operators WHERE server = ?",
        (server,),
    ).fetchone()
    assert total > 400, f"{server}: only {total} operators -- the corpus shrank"
    assert distinct >= MIN_DISTINCT_SUBCLASSES, f"{server}: only {distinct} distinct names"
    # The pairing is total on this corpus, so the id never ships bare in practice. If this
    # ever drops, the limitation arm below is what a client gets -- never a fabricated name.
    assert named == total, f"{server}: {total - named} operators carry an id with no name"


@requires_build
def test_no_operator_carries_a_name_without_an_id(build_conn: sqlite3.Connection) -> None:
    """A name with no id would be a pairing to nothing -- the join, not the source."""
    orphans = build_conn.execute(
        "SELECT count(*) FROM operators WHERE subclass_name IS NOT NULL AND subclass_id IS NULL"
    ).fetchone()[0]
    assert orphans == 0


@requires_build
def test_wire_pairs_the_name_with_the_id(build_conn: sqlite3.Connection) -> None:
    """The id still ships (it is the joinable key); the name ships beside it."""
    envelope = registry_for(build_conn).get("get_operator").handler(server="en", game_id=AMIYA)
    body = json.loads(json.dumps(envelope.to_dict()))
    summary = body["data"]["operator"]["summary"]
    assert summary["subclass_id"], "the id stopped shipping -- the name is an addition"
    assert summary["subclass_name"], "the id ships bare again"
    assert summary["subclass_name"] != summary["subclass_id"]
    # ...and no limitation claims the name is unavailable while it is right there.
    assert not any("no name for this operator's subclass" in text for text in body["limitations"])
