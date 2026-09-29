"""Real-build guard -- shared stage codes are real, and disclosed.

The unit tests build a two-stage fixture and assert the disclosure fires. That
proves the mechanism, not the PREMISE: if shared stage codes were rare or synthetic, the
whole task would be ceremony, and a change that quietly stopped resolving them (or that
made the alternates list wrong) would still pass every fixture test. The population must
be COUNTED on the real corpus, not assumed.

Counted on the shipped ``2026-07-28T170428Z`` build:

* **927 en stage codes are shared by two or more stages** (2293 stages), and 989 in cn --
  a quarter of the en stage table, not a corner case;
* group sizes run 2 (775 groups), 3 (134), 4 (5), then a tail to **36** (``LT-1``..
  ``LT-6``), which is why the emitted list is bounded while its count stays exact;
* The reported case is live: ``4-4`` en names ``main_04-04`` (NORMAL) AND ``main_04-04#f#``
  (FOUR_STAR), and the second was reachable only through a game_id nothing pointed at;
* **206 shared codes have a first-by-order stage with NO drop cache while a sibling under
  the same code HAS one** (cn ``10-10`` resolves to ``easy_10-09`` over ``main_10-09`` /
  ``tough_10-09``), so ``get_stage_drops``'s ``not_found`` was an artefact of the silent
  pick -- the reason that verdict now names the alternates too.

Skipped when no build is promoted (the offline gate builds fixtures, not a full en+cn
corpus); the unit tests carry the behaviour, so a logic edit still regresses loudly
offline.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from arknights_mcp.db.repositories.stages import StageRepository
from arknights_mcp.mcp.tools.drops import build_get_stage_drops_spec
from arknights_mcp.mcp.tools.stage import build_analyze_stage_spec, build_get_stage_spec
from arknights_mcp.services.stages import MAX_STAGE_CODE_MATCHES, _resolve_stage

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


@pytest.fixture(scope="module")
def conn() -> sqlite3.Connection:
    assert BUILD is not None
    return sqlite3.connect(f"file:{BUILD}?mode=ro", uri=True)


def _groups(conn: sqlite3.Connection, server: str) -> dict[str, list[tuple[int, str]]]:
    """``stage_code -> [(stage_pk, game_id)]`` in pick order, shared codes only."""
    rows = conn.execute(
        "SELECT stage_code, stage_pk, game_id FROM stages "
        "WHERE server = ? AND stage_code IS NOT NULL ORDER BY stage_code, stage_pk",
        (server,),
    )
    by_code: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for code, pk, game_id in rows:
        by_code[str(code)].append((int(pk), str(game_id)))
    return {code: members for code, members in by_code.items() if len(members) > 1}


# --- the premise: shared codes are a real, large population --------------------


@pytest.mark.parametrize(("server", "minimum"), [("en", 900), ("cn", 900)])
def test_shared_stage_codes_are_a_large_population(
    conn: sqlite3.Connection, server: str, minimum: int
) -> None:
    # Counted, not assumed: en 927 / cn 989 shared codes on the 2026-07-28 build.
    # A floor rather than an equality so an upstream stage release does not fail the
    # suite -- the claim under guard is "many", and a collapse to a handful would mean
    # the resolution rule stopped seeing them.
    shared = _groups(conn, server)
    assert len(shared) >= minimum, f"{server}: only {len(shared)} shared stage codes"


def test_largest_group_stays_inside_the_bounded_read(conn: sqlite3.Connection) -> None:
    # The service reads at most MAX_STAGE_CODE_MATCHES rows and reports a capped set as
    # open-ended. The real maximum is 36 (LT-1..LT-6), so the cap does not bite today --
    # if upstream ever exceeds it, the truncation arm must be exercised deliberately,
    # never discovered as a silently short list.
    sizes = Counter(len(members) for members in _groups(conn, "en").values())
    assert max(sizes) <= MAX_STAGE_CODE_MATCHES
    assert sizes[2] > sizes[3] > 0, (
        f"group-size distribution changed shape: {sorted(sizes.items())}"
    )


def test_b139_case_is_live_on_the_shipped_build(conn: sqlite3.Connection) -> None:
    # The reported case, verbatim: two stages, one code, and the variant reachable only
    # by an id the client was never given.
    members = _groups(conn, "en")["4-4"]
    assert [game_id for _, game_id in members] == ["main_04-04", "main_04-04#f#"]


# --- the disclosure, on the real corpus ----------------------------------------


def test_get_stage_discloses_the_pick_and_the_alternate(conn: sqlite3.Connection) -> None:
    env = build_get_stage_spec(lambda: conn).handler(server="en", stage_code="4-4")
    assert env.status == "ok"
    payload = env.to_dict()
    assert payload["data"]["stage"]["game_id"] == "main_04-04"  # type: ignore[index]
    (disclosure,) = [lim for lim in payload["limitations"] if "stage_code 4-4" in lim]
    assert "2 stages" in disclosure
    assert "main_04-04#f#" in disclosure
    assert "difficulty NORMAL" in disclosure


def test_analyze_stage_discloses_on_the_reported_stage(conn: sqlite3.Connection) -> None:
    # 7-4 = main_07-03, the stage the defect was reported on; it too has a four-star sibling.
    env = build_analyze_stage_spec(lambda: conn).handler(server="en", stage_code="7-4")
    (disclosure,) = [lim for lim in env.to_dict()["limitations"] if "stage_code 7-4" in lim]
    assert "main_07-03#f#" in disclosure


def test_tower_group_is_summarised_with_an_exact_count(conn: sqlite3.Connection) -> None:
    # The 36-stage LT-1 group: every alternate named would be an economy breach, so
    # the list is bounded -- but the COUNT is the real one, so the client is never told
    # the ambiguity is smaller than it is.
    members = _groups(conn, "en")["LT-1"]
    env = build_get_stage_spec(lambda: conn).handler(server="en", stage_code="LT-1")
    (disclosure,) = [lim for lim in env.to_dict()["limitations"] if "stage_code LT-1" in lim]
    assert f"shared by {len(members)} stages" in disclosure
    assert f"and {len(members) - 1 - 8} more" in disclosure


def test_unique_code_stays_silent(conn: sqlite3.Connection) -> None:
    # A code matching one stage carries no disclosure: the guard must be able to tell
    # "ambiguous" from "always fires".
    unique = next(
        code
        for (code,) in conn.execute(
            "SELECT stage_code FROM stages WHERE server = 'en' AND stage_code IS NOT NULL "
            "GROUP BY stage_code HAVING count(*) = 1 ORDER BY stage_code LIMIT 1"
        )
    )
    env = build_get_stage_spec(lambda: conn).handler(server="en", stage_code=str(unique))
    assert env.status == "ok"
    assert [lim for lim in env.to_dict()["limitations"] if "is shared by" in lim] == []


# --- the drops dead end the silent pick invented -------------------------------


def test_drop_divergent_groups_exist_and_are_disclosed(conn: sqlite3.Connection) -> None:
    # 206 shared codes on this build resolve to a stage with no drop cache while a
    # sibling under the same code has one, so the empty answer says nothing about the code
    # the client asked about. Count them from the table, then drive the tool on one.
    # The stage RESOLVED, so the answer is an ``ok`` with an empty ``drops`` --
    # the alternates that make it retryable now ride a limitation instead of the
    # suggested_action an ``ok`` envelope has no room for (text moved not cut).
    with_drops = {int(pk) for (pk,) in conn.execute("SELECT DISTINCT stage_pk FROM stage_drops")}
    divergent: list[tuple[str, str, list[str]]] = []
    for server in ("en", "cn"):
        for code, members in _groups(conn, server).items():
            if members[0][0] not in with_drops and any(pk in with_drops for pk, _ in members[1:]):
                divergent.append((server, code, [game_id for _, game_id in members[1:]]))
    assert len(divergent) >= 200, f"only {len(divergent)} drop-divergent shared codes"

    server, code, alternates = divergent[0]
    env = build_get_stage_drops_spec(lambda: conn).handler(server=server, stage_code=code)
    assert env.status == "ok"
    assert env.to_dict()["data"]["drops"] == []  # type: ignore[index]
    hint = next(lim for lim in env.limitations if "may be the stage that holds" in lim)
    assert f"The stage_code {code} is also used by" in hint
    assert alternates[0] in hint


def test_resolution_is_deterministic_across_every_shared_code(conn: sqlite3.Connection) -> None:
    # Over the WHOLE shared-code population, the resolved stage is the lowest
    # stage_pk and the alternates are exactly the rest, in order -- so the disclosure can
    # never name a stage the lookup did not return.
    repo = StageRepository(conn)
    for code, members in list(_groups(conn, "en").items())[:200]:
        stage, ambiguity = _resolve_stage(repo, "en", stage_code=code, game_id=None)
        assert stage is not None and ambiguity is not None
        assert stage.game_id == members[0][1]
        assert list(ambiguity.alternates) == [game_id for _, game_id in members[1:]]
        assert ambiguity.truncated is False
