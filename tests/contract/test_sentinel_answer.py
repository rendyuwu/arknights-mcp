"""A stripped sentinel keeps the source's ANSWER.

Upstream writes ``rangeRadius.m_value = -1.0`` for "this enemy has no attack radius".
The placeholder rule keeps it out of ``enemy_levels.attack_range``, a distance column --
correct, and unchanged by this fix. What the strip left behind is that it also erased the
fact that upstream ANSWERED, so one NULL carried two different source statements:

    m_defined:true, m_value:-1.0   the source SAID there is none
    never defined                  the source said NOTHING

and ``threat.ranged_arts`` reported the first class under the limitation "attack_range
missing" -- a true conclusion with a false reason -- while the sibling ``targeting: NONE``
conflict did warn. The non-degeneracy check could not catch it (the column is
non-degenerate: 1757/4343), the placeholder rule could not (the strip is right), and the
null-discipline rule could not (a SCALAR has no ``[]`` arm).

So the guard is a count of the two classes and a behaviour check over the enemies that
sit in the gap between them. Three questions, three groups:

* upstream -- does the sentinel population still exist, and is ``-1.0`` still the only
  value it takes (a domain is counted, never assumed);
* bridge -- do those enemies reach the parser carrying the answer rather than an absence
  indistinguishable from silence;
* rule -- are the ones whose ``applyWay`` DISAGREES with the sentinel warned and omitted,
  by ``game_id``, instead of concluded under a false reason.

The build-side half (what the promoted corpus actually stores) runs separately, with its
own gate, because a column count is only evidence after a build carries it.

The upstream groups are CI-only: they need network, gated behind ``ARKMCP_LIVE_UPSTREAM``
like the other live-upstream guards, and this module is named in
``.github/workflows/ci.yml`` (``tests/unit/test_ci_matrix.py`` fails if it is not -- an
unlisted live-upstream module runs NOWHERE). Nothing fetched is persisted.
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

from arknights_mcp.analyzers.base import EnemyOccurrence, StageThreatContext
from arknights_mcp.analyzers.rules.ranged_arts import RangedArtsRule
from arknights_mcp.importers.enemy_normalization import normalize_enemy_sources
from arknights_mcp.importers.field_policy import ENEMY_KEY_HOMES

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "data" / "current.json"

SERVERS = ("en", "cn")

requires_upstream = pytest.mark.skipif(live_upstream_disabled(), reason=LIVE_UPSTREAM_SKIP_REASON)

#: The value upstream uses for "no attack radius". Pinned, not inferred: if it ever moved,
#: the mask strip would be discarding real distances and this whole task would be reading
#: a mask that no longer exists (the build side pins the same constant).
NO_RADIUS_SENTINEL = -1.0

#: ``applyWay`` tokens that state the enemy strikes past melee -- the ones that CONTRADICT
#: a declared "no attack radius" and so state conflicting source fields.
RANGED_TOKENS = frozenset({"RANGED", "ALL"})

#: Non-degenerate floors for the conflict population, well under the pinned counts
#: (13 en / 16 cn). A corpus that stopped carrying the sentinel would otherwise let every
#: behavioural assertion below pass over an empty list.
MIN_CONFLICT_ENEMIES = 5
#: ...and for the class the flag exists to separate it FROM.
MIN_UNSTATED_ENEMIES = 5


@lru_cache(maxsize=len(SERVERS))
def _enemy_database(server: str) -> dict[str, Any]:
    """The pinned ``enemy_database`` for ``server``; fetched once, never written."""
    url = f"{arknights_assets_base_url(server)}/gamedata/levels/enemydata/enemy_database.json"
    table = json.loads(fetch_upstream_bytes(url).decode("utf-8"))
    assert isinstance(table, dict) and table, f"{server} enemy_database is not a populated dict"
    return table


@lru_cache(maxsize=len(SERVERS))
def _enemy_handbook(server: str) -> dict[str, Any]:
    """The pinned ``enemy_handbook_table`` entries for ``server`` (never written)."""
    url = f"{arknights_assets_base_url(server)}/gamedata/excel/enemy_handbook_table.json"
    table = json.loads(fetch_upstream_bytes(url).decode("utf-8"))
    entries = table.get("enemyData") if isinstance(table, dict) else None
    assert isinstance(entries, dict) and entries, f"{server} handbook carries no enemyData"
    return entries


def _defined(cell: Any) -> tuple[bool, Any]:
    """A ``{m_defined, m_value}`` cell -> ``(defined, value)``."""
    if isinstance(cell, dict):
        return bool(cell.get("m_defined", True)), cell.get("m_value")
    return True, cell


def _base_level(server: str, game_id: str) -> dict[str, Any]:
    """The enemy's level-0 entry (or its first), as upstream ships it."""
    levels = _enemy_database(server).get(game_id) or []
    base = next(
        (lvl for lvl in levels if isinstance(lvl, dict) and lvl.get("level") == 0),
        levels[0] if levels else None,
    )
    data = base.get("enemyData") if isinstance(base, dict) else None
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=len(SERVERS))
def _arts_no_radius(server: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(denied, unstated)`` arts-capable enemy ids that store no attack radius.

    The split this guard is about, read off the SOURCE rather than the built column -- which is
    the point: after the strip both classes look identical downstream, so only the raw
    ``m_defined``/``m_value`` pair can separate them. "Denied" additionally requires an
    ``applyWay`` that contradicts the sentinel, because that contradiction is what the
    rule has to report rather than conclude from.
    """
    denied: list[str] = []
    unstated: list[str] = []
    for game_id, entry in _enemy_handbook(server).items():
        if "MAGIC" not in (entry.get("damageType") or []):
            continue
        data = _base_level(server, game_id)
        range_defined, range_value = _defined(data.get("rangeRadius"))
        targeting_defined, targeting_value = _defined(data.get("applyWay"))
        targeting = targeting_value if targeting_defined else None
        is_sentinel = range_defined and isinstance(range_value, int | float) and range_value < 0
        if is_sentinel and targeting in RANGED_TOKENS:
            denied.append(game_id)
        elif not range_defined and targeting in RANGED_TOKENS:
            unstated.append(game_id)
    return tuple(sorted(denied)), tuple(sorted(unstated))


# --- upstream: the two classes exist and are distinguishable -------------------


@requires_upstream
@pytest.mark.parametrize("server", SERVERS)
def test_the_sentinel_is_still_one_value_and_still_an_answer(server: str) -> None:
    """The mask's VALUE is pinned, because its meaning rests on it.

    A second negative value would mean ``-1.0`` is one code among several and the strip is
    collapsing a domain, not a sentinel -- at which point "the source declares no attack
    radius" stops being what the data says.
    """
    negatives = {
        value
        for game_id in _enemy_handbook(server)
        for defined, value in [_defined(_base_level(server, game_id).get("rangeRadius"))]
        if defined and isinstance(value, int | float) and value < 0
    }
    assert negatives == {NO_RADIUS_SENTINEL}, f"{server}: the no-radius sentinel moved: {negatives}"


@requires_upstream
@pytest.mark.parametrize("server", SERVERS)
def test_both_classes_of_absent_radius_exist_on_the_real_corpus(server: str) -> None:
    """The census this guard rests on: an absent radius is TWO source statements, not one.

    Asserted before any behaviour, so a corpus that lost either class fails here instead of
    quietly making the guards below vacuous.
    """
    denied, unstated = _arts_no_radius(server)
    assert len(denied) >= MIN_CONFLICT_ENEMIES, f"{server}: only {len(denied)} denied-radius arts"
    assert len(unstated) >= MIN_UNSTATED_ENEMIES, f"{server}: only {len(unstated)} unstated"
    # The two classes are disjoint by construction; if they ever overlapped, one enemy would
    # be both denied and unstated and the flag would mean nothing.
    assert not set(denied) & set(unstated)
    assert ENEMY_KEY_HOMES["attackRangeDeclaredNone"].status == "live"


@requires_upstream
@pytest.mark.parametrize("server", SERVERS)
def test_the_bridge_carries_the_answer_for_every_denied_enemy(server: str) -> None:
    """upstream -> bridge, on the REAL records: the strip runs and the answer survives.

    Counting upstream alone would reproduce the earlier blind spot exactly (a fact that exists
    in the source and in no code path between it and the column), so this runs each enemy
    through the real normalization bridge and asserts BOTH halves on every level it emits: no
    negative reached the distance column, and the answer did.
    """
    denied, _ = _arts_no_radius(server)
    picked = {game_id: _enemy_database(server)[game_id] for game_id in denied}
    _, database_norm = normalize_enemy_sources({"enemyData": {}}, picked)
    for game_id in denied:
        levels = database_norm["enemies"][game_id]["levels"]
        assert levels, f"{server} {game_id}: the bridge emitted no levels"
        for level in levels:
            assert "attackRange" not in level, f"{server} {game_id}: the mask reached the column"
            assert level.get("attackRangeDeclaredNone") is True, (
                f"{server} {game_id}: the source's answer was stripped along with the value"
            )


# --- the rule: those enemies are WARNED, not concluded -------------------------


def _evaluate(server: str, game_id: str, targeting: str, *, declared_none: bool) -> Any:
    rule = RangedArtsRule()
    return rule.evaluate(
        StageThreatContext(
            server=server,
            stage_game_id="main_04-04",
            stage_code="4-4",
            occurrences=(
                EnemyOccurrence(
                    game_id=game_id,
                    display_name=None,
                    motion_type="WALK",
                    damage_types=("MAGIC",),
                    total_count=1,
                    attack_range=None,
                    attack_range_declared_none=declared_none,
                    targeting=targeting,
                ),
            ),
        )
    )


@requires_upstream
@pytest.mark.parametrize("server", SERVERS)
def test_every_denied_enemy_is_warned_and_none_is_concluded(server: str) -> None:
    """By ``game_id``, over the real population -- the 13 en / 16 cn.

    Each is evaluated alone so a single misclassification cannot hide inside a stage that
    other enemies made fire anyway. Two source fields disagree here: the record denies a
    base attack radius and states ``applyWay: RANGED|ALL``. That is a typed-field conflict, so it
    is reported and the conclusion is omitted -- never published at 0.8 under a limitation
    that calls the denied cell "missing".
    """
    denied, _ = _arts_no_radius(server)
    for game_id in denied:
        targeting = _defined(_base_level(server, game_id).get("applyWay"))[1]
        result = _evaluate(server, game_id, targeting, declared_none=True)
        assert result.observation is None, (
            f"{server} {game_id}: reported as a ranged-arts threat while its own source "
            "record declares it has no attack radius"
        )
        warning = next((w for w in result.warnings if game_id in w), None)
        assert warning is not None, f"{server} {game_id}: conflicting fields, no warning"
        assert "conflicting source fields" in warning
        # Nothing may call a cell the source FILLED missing.
        assert "missing" not in warning


@requires_upstream
@pytest.mark.parametrize("server", SERVERS)
def test_the_unstated_class_still_concludes_from_targeting(server: str) -> None:
    """The negative control the fix must not break (the targeting arm stays intact).

    These enemies really do carry no radius statement, so ``targeting`` remains the best
    typed answer and the 0.8 conclusion stands -- with the "attack_range missing"
    limitation, which is now TRUE by construction because the denied class can no longer
    reach this arm. Without this half, deleting the whole targeting branch would pass.
    """
    _, unstated = _arts_no_radius(server)
    for game_id in unstated:
        targeting = _defined(_base_level(server, game_id).get("applyWay"))[1]
        result = _evaluate(server, game_id, targeting, declared_none=False)
        assert result.observation is not None, f"{server} {game_id}: lost its targeting arm"
        assert any("attack_range missing" in lim for lim in result.observation.limitations)


# --- build-side: the column carries the split ---------------------------------


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

requires_build = pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)

#: Build-side floor for the column: a count over the BUILT DB is the only
#: witness that a new column is not another NULL by construction.
MIN_DECLARED_NONE_ROWS = 100


@requires_build
def test_the_column_is_not_another_empty_one() -> None:
    """Applied to this module's own column: counted on the build, not assumed."""
    assert BUILD is not None
    conn = sqlite3.connect(f"file:{BUILD}?mode=ro", uri=True)
    declared = conn.execute(
        "SELECT COUNT(*) FROM enemy_levels WHERE attack_range_declared_none = 1"
    ).fetchone()[0]
    assert declared >= MIN_DECLARED_NONE_ROWS, f"only {declared} level rows carry the answer"
    # Unchanged: not one negative radius is stored as a distance, then or now.
    assert (
        conn.execute("SELECT COUNT(*) FROM enemy_levels WHERE attack_range < 0").fetchone()[0] == 0
    )
    # The impossible corner stays impossible: a level cannot both state a radius and deny
    # having one. The pair is the decidable state, so this is what makes it decidable.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM enemy_levels "
            "WHERE attack_range IS NOT NULL AND attack_range_declared_none = 1"
        ).fetchone()[0]
        == 0
    )


@requires_build
@pytest.mark.parametrize("server", SERVERS)
def test_the_build_splits_the_population_b161_could_not(server: str) -> None:
    """The whole point, measured on the promoted corpus.

    Before this column, arts enemies with a NULL radius and a RANGED|ALL token were one
    undifferentiated group and every one of them was reported with the reason
    "attack_range missing". The build must now split that group in two, with BOTH parts
    non-empty -- a split where one side is empty is not a split, it is a rename.
    """
    assert BUILD is not None
    conn = sqlite3.connect(f"file:{BUILD}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT DISTINCT e.game_id, el.attack_range_declared_none "
        "FROM enemies e JOIN enemy_levels el ON el.enemy_pk = e.enemy_pk "
        "WHERE e.server = ? AND el.attack_range IS NULL "
        "AND el.targeting IN ('RANGED', 'ALL') "
        "AND e.damage_types_json LIKE '%MAGIC%'",
        (server,),
    ).fetchall()
    denied = {game_id for game_id, flag in rows if flag}
    unstated = {game_id for game_id, flag in rows if not flag} - denied
    assert denied, f"{server}: no enemy carries the answer, so the column changed nothing"
    assert unstated, f"{server}: every enemy is 'denied' -- the flag is not discriminating"
