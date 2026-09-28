"""ADR 0020: a full 100-row ``get_my_roster`` page of real operator ids fits the §V22 cap.

The ``FRAME_PRESSURE`` row for ``get_my_roster`` is counted on the two-operator synthetic
fixture, because a real roster is personal and never part of the basis. This guard
builds the widest row the roster can carry for the 100 en operators with the longest
names -- E2 level 90, max potential, skill level 7, every build skill at M3, every build
module unlocked at level 3 -- and asserts the maximum page still answers ``ok`` under the
cap. Skipped without a promoted build, like every other real-corpus contract guard.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.account import FIXTURE_SYNCED_AT
from tests.support.tool_calls import active_build

from arknights_mcp.db.account import AccountStore
from arknights_mcp.db.connection import open_read_only
from arknights_mcp.db.repositories.operators import OperatorRepository
from arknights_mcp.importers.account import parse_sync_data
from arknights_mcp.mcp.envelopes import MAX_RESPONSE_BYTES, wire_size
from arknights_mcp.mcp.tools.account import build_get_my_roster_spec

BUILD = active_build()

pytestmark = pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)


@pytest.fixture(scope="module")
def conn() -> Iterator[sqlite3.Connection]:
    assert BUILD is not None
    with open_read_only(BUILD) as connection:
        yield connection


def _maxed_user(conn: sqlite3.Connection) -> dict[str, object]:
    repo = OperatorRepository(conn)
    ids = [
        row[0]
        for row in conn.execute(
            "select game_id from operators where server = 'en' "
            "order by length(display_name) desc, game_id limit 100"
        )
    ]
    chars: dict[str, object] = {}
    for n, game_id in enumerate(ids):
        build = repo.operator_by_game_id("en", game_id)
        assert build is not None
        modules = [m.game_id for m in repo.modules(build.operator_pk)]
        chars[str(n)] = {
            "charId": game_id,
            "evolvePhase": 2,
            "level": 90,
            "potentialRank": 5,
            "mainSkillLvl": 7,
            "skills": [
                {"skillId": s.game_id, "unlock": 1, "specializeLevel": 3}
                for s in repo.skills(build.operator_pk)
            ],
            "equip": {m: {"locked": 0, "level": 3} for m in modules},
            "currentEquip": modules[-1] if modules else None,
        }
    return {"status": {"gold": 0}, "troop": {"chars": chars}}


def test_a_full_page_of_maxed_real_operators_fits_the_cap(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    store = AccountStore(f"sqlite:///{tmp_path}/cap.sqlite")
    store.write_roster(parse_sync_data(_maxed_user(conn)), synced_at=FIXTURE_SYNCED_AT)
    spec = build_get_my_roster_spec(lambda: conn, account_store=store)
    env = spec.handler(server="en", page={"page": 1, "page_size": 100})
    assert env.status == "ok"
    assert len(env.data["operators"]) == 100
    assert wire_size(env) <= MAX_RESPONSE_BYTES
