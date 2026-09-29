"""The ``applies_to`` label reads the source field that STATES it.

A module change describes either the operator or the operator's summon/token, and the
source SAYS which: ``battle_equip_table`` ``phases[].parts[].isToken``, a typed bool on
the part that owns the candidate. The server instead inferred the label from a
NEIGHBOURING sentinel -- ``talentIndex == -1`` -- and the two are nearly disjoint::

    (talentIndex == -1, isToken)   en                cn
    (True,  False)                 454               498     labelled "token", WRONGLY
    (True,  True )                  59                64
    (False, True )                  79                88     the real ones, UNLABELLED
    (False, False)                1642              1752

so the shipped label was false on 454 en rows and absent on the 79 the source flags. It
was verifiable the whole time: Kal'tsit's own module (the canonical token case) carries
``talentIndex: 1`` on both POVs, and Sophia's ``uniequip_002_sophia`` carries ``-1`` on a
part flagged ``isToken: false`` for a talent whose text buffs Melee ALLIES.

This module enforces a cross-tab, and it is the check that would have caught it:
a label must be CROSS-TABBED against the source's own flag and pinned non-degenerate on
BOTH axes. A guard that only checked "some row is labelled token" passed for four
milestones -- the sentinel and the flag agree on 59 en rows, which is plenty to keep such
an assertion green while 454 others are wrong. Three groups here:

* upstream -- the cross-tab itself, both axes non-degenerate, at the pinned commit;
* bridge -- the flag survives parsing onto every change bundle, on the real modules;
* wire -- Sophia reads ``operator`` and Kal'tsit's token rows read ``token``, and the two
  POVs of one change stay two rows (the identity, which the fix also corrected).

The upstream group is CI-only (network, ``ARKMCP_LIVE_UPSTREAM``) and this module is
named in ``.github/workflows/ci.yml``; ``tests/unit/test_ci_matrix.py`` fails if it is
not, since an unlisted live-upstream module runs nowhere. Nothing fetched
is persisted.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
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

from arknights_mcp.importers.modules import parse_modules

REPO_ROOT = Path(__file__).resolve().parents[2]

SERVERS = ("en", "cn")

requires_upstream = pytest.mark.skipif(live_upstream_disabled(), reason=LIVE_UPSTREAM_SKIP_REASON)

BUILD = active_build()
requires_build = pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)

#: Non-degenerate floors for each cell of the cross-tab, set well under the pinned
#: counts. The two that matter are the DISAGREEMENT cells: a corpus where the sentinel and
#: the flag always agreed would make the old inference harmless, and every assertion below
#: would pass while proving nothing.
MIN_SENTINEL_WITHOUT_FLAG = 200  # pinned 454 en / 498 cn -- the falsely labelled rows
MIN_FLAG_WITHOUT_SENTINEL = 20  # pinned 79 en / 88 cn -- the ones the label missed
MIN_TOKEN_PARTS = 50  # pinned 125 en / 137 cn

#: A module whose token-POV candidates carry a REAL talent index, so the sentinel could
#: never have found them, and the operator that owns it.
KALTSIT_MODULE = "uniequip_002_kalts"
KALTSIT = "char_003_kalts"

#: A module whose ``-1`` candidates sit on an operator part -- the false-positive class.
SOPHIA_MODULE = "uniequip_002_sophia"
SOPHIA = "char_265_sophia"


@lru_cache(maxsize=len(SERVERS))
def _battle_equip(server: str) -> dict[str, Any]:
    """The pinned ``battle_equip_table`` for ``server``; fetched once, never written."""
    url = f"{arknights_assets_base_url(server)}/gamedata/excel/battle_equip_table.json"
    table = json.loads(fetch_upstream_bytes(url).decode("utf-8"))
    assert isinstance(table, dict) and table, f"{server} battle_equip_table is not a populated dict"
    return table


@lru_cache(maxsize=len(SERVERS))
def _uniequip(server: str) -> dict[str, Any]:
    """The pinned ``uniequip_table`` for ``server`` (needed to parse modules)."""
    url = f"{arknights_assets_base_url(server)}/gamedata/excel/uniequip_table.json"
    table = json.loads(fetch_upstream_bytes(url).decode("utf-8"))
    assert isinstance(table, dict) and table, f"{server} uniequip_table is not a populated dict"
    return table


def _talent_candidates(server: str) -> list[tuple[bool, bool]]:
    """Every talent candidate upstream ships, as ``(talentIndex == -1, part.isToken)``."""
    out: list[tuple[bool, bool]] = []
    for module in _battle_equip(server).values():
        for phase in (module.get("phases") if isinstance(module, dict) else None) or []:
            for part in (phase.get("parts") if isinstance(phase, dict) else None) or []:
                if not isinstance(part, dict):
                    continue
                bundle = part.get("addOrOverrideTalentDataBundle") or {}
                for cand in (bundle.get("candidates") if isinstance(bundle, dict) else None) or []:
                    if isinstance(cand, dict):
                        out.append((cand.get("talentIndex") == -1, bool(part.get("isToken"))))
    return out


# --- upstream: the cross-tab, both axes non-degenerate ----------------


@requires_upstream
@pytest.mark.parametrize("server", SERVERS)
def test_sentinel_and_flag_are_different_facts(server: str) -> None:
    """The four cells, counted. This is the assertion whose absence shipped the defect."""
    cells = Counter(_talent_candidates(server))
    assert sum(cells.values()) > 1000, f"{server}: too few candidates to conclude anything"
    # The disagreement cells: each one is a row the old inference got wrong, in one
    # direction or the other. Both must be populated, or the cross-tab proves nothing.
    assert cells[(True, False)] >= MIN_SENTINEL_WITHOUT_FLAG, (
        f"{server}: the sentinel no longer appears on operator parts -- if upstream really "
        f"changed, re-derive the label rule rather than relaxing this floor (got {cells})"
    )
    assert cells[(False, True)] >= MIN_FLAG_WITHOUT_SENTINEL, (
        f"{server}: no token-flagged candidate carries a real talent index (got {cells})"
    )
    # ...and the agreement cell, which is the trap: it is why a "some row says token"
    # guard stayed green while 454 rows were wrong.
    assert cells[(True, True)] > 0


@requires_upstream
@pytest.mark.parametrize("server", SERVERS)
def test_is_token_is_a_part_level_typed_bool(server: str) -> None:
    """The flag is on the PART, not the candidate -- which is why it had to be carried down."""
    token_parts = 0
    for module in _battle_equip(server).values():
        for phase in (module.get("phases") if isinstance(module, dict) else None) or []:
            for part in (phase.get("parts") if isinstance(phase, dict) else None) or []:
                if not isinstance(part, dict):
                    continue
                assert isinstance(part.get("isToken"), bool), "isToken is not a typed bool"
                token_parts += bool(part.get("isToken"))
                for cand in (part.get("addOrOverrideTalentDataBundle") or {}).get(
                    "candidates"
                ) or []:
                    # Nothing inside a candidate states it: if this ever fails, the flag
                    # has a closer home and the importer should read THAT one.
                    assert isinstance(cand, dict) and "isToken" not in cand
    assert token_parts >= MIN_TOKEN_PARTS, f"{server}: only {token_parts} token parts"


@requires_upstream
@pytest.mark.parametrize("server", SERVERS)
def test_parser_carries_the_flag_onto_every_change(server: str) -> None:
    """Bridge: the parsed bundles keep the source's statement, for both values."""
    parsed = parse_modules(_uniequip(server), _battle_equip(server))
    assert parsed, f"{server}: no modules parsed"
    seen: Counter[bool] = Counter()
    for module in parsed:
        for level in module.levels:
            for change in (level.talent_changes or []) + (level.trait_changes or []):
                assert "isToken" in change, f"{module.game_id}: a change lost the source flag"
                seen[bool(change["isToken"])] += 1
    # Non-degenerate on both values: a parser that hardcoded False would pass a
    # "the key is present" check on its own.
    assert seen[True] > 0 and seen[False] > 0, f"{server}: one-sided flag {seen}"


@requires_upstream
def test_kaltsit_token_rows_carry_a_real_talent_index() -> None:
    """The case that proves the sentinel could not have found them (the canonical example)."""
    parsed = {m.game_id: m for m in parse_modules(_uniequip("en"), _battle_equip("en"))}
    token_changes = [
        change
        for level in parsed[KALTSIT_MODULE].levels
        for change in level.talent_changes or []
        if change.get("isToken")
    ]
    assert token_changes, "Kal'tsit's module carries no token-flagged talent change"
    assert all(change.get("talentIndex") != -1 for change in token_changes), (
        "the token rows carry -1 after all -- if upstream reshaped this, the whole "
        "sentinel-vs-flag argument needs recounting, not this assertion loosened"
    )


@requires_upstream
def test_sophia_minus_one_sits_on_an_operator_part() -> None:
    """The false-positive class, at its own id: -1 with the flag saying operator."""
    parsed = {m.game_id: m for m in parse_modules(_uniequip("en"), _battle_equip("en"))}
    minus_one = [
        change
        for level in parsed[SOPHIA_MODULE].levels
        for change in level.talent_changes or []
        if change.get("talentIndex") == -1
    ]
    assert minus_one, "Sophia's module no longer carries the -1 sentinel"
    assert all(change.get("isToken") is False for change in minus_one)


# --- wire: what a client actually reads ---------------------------


@pytest.fixture(scope="module")
def build_conn() -> Any:
    conn = sqlite3.connect(f"file:{BUILD}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()


def _module_changes(conn: sqlite3.Connection, game_id: str) -> list[dict[str, Any]]:
    """Every emitted talent/trait change row for one operator's modules, through the tool."""
    envelope = (
        registry_for(conn)
        .get("compare_operator_modules")
        .handler(server="en", game_id=game_id, mode="facts_only")
    )
    data = json.loads(json.dumps(envelope.to_dict()))["data"]
    rows: list[dict[str, Any]] = []
    for module in data.get("modules") or []:
        for key in ("trait_changes", "talent_changes"):
            rows.extend(c for c in (module.get(key) or []) if isinstance(c, dict))
        for level in module.get("levels") or []:
            for key in ("trait_changes", "talent_changes"):
                rows.extend(c for c in (level.get(key) or []) if isinstance(c, dict))
    return rows


@requires_build
def test_wire_label_follows_the_source_flag(build_conn: sqlite3.Connection) -> None:
    """The defect head-on, at both ids, on the promoted build."""
    sophia = _module_changes(build_conn, SOPHIA)
    assert sophia, "Sophia's modules emitted no change rows"
    sentinel_rows = [row for row in sophia if row.get("talent_index") == -1]
    assert sentinel_rows, "the -1 rows are gone from the wire"
    assert all(row.get("applies_to") == "operator" for row in sentinel_rows), (
        "a -1 change is labelled token again -- the original defect verbatim"
    )

    kaltsit = _module_changes(build_conn, KALTSIT)
    token_rows = [row for row in kaltsit if row.get("applies_to") == "token"]
    assert token_rows, "Kal'tsit's token-flagged rows carry no token label"
    # ...and they are the ones the sentinel would have missed.
    assert all(row.get("talent_index") != -1 for row in token_rows)


@requires_build
def test_raw_source_key_never_reaches_the_wire(build_conn: sqlite3.Connection) -> None:
    """The label ships, the camelCase flag it was renamed from does not."""
    for game_id in (SOPHIA, KALTSIT):
        for row in _module_changes(build_conn, game_id):
            assert "isToken" not in row, f"{game_id}: raw source key leaked to the wire"


@requires_build
def test_both_povs_of_one_change_survive_as_two_rows(build_conn: sqlite3.Connection) -> None:
    """The amended identity: the POV is part of it, so neither copy is merged away."""
    rows = _module_changes(build_conn, KALTSIT)
    by_identity: Counter[tuple[Any, Any]] = Counter()
    for row in rows:
        if row.get("applies_to"):
            by_identity[(row.get("talent_index"), row.get("required_potential_rank"))] += 1
    both = [
        key
        for key in by_identity
        if {
            row.get("applies_to")
            for row in rows
            if (row.get("talent_index"), row.get("required_potential_rank")) == key
        }
        == {"token", "operator"}
    ]
    assert both, "no (talent_index, required_potential_rank) group carries both POVs"


@requires_build
def test_label_is_explained_where_it_is_read(build_conn: sqlite3.Connection) -> None:
    """The value vocabulary rides the response that carries the labels."""
    envelope = (
        registry_for(build_conn)
        .get("compare_operator_modules")
        .handler(server="en", game_id=KALTSIT, mode="facts_only")
    )
    body = json.loads(json.dumps(envelope.to_dict()))
    legend = body["data"]["enum_legend"]["applies_to"]
    assert set(legend) == {"token", "operator"}
    assert all(text for text in legend.values())
