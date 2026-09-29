"""``get_item_drops`` tool tests.

The reverse of ``get_stage_drops``: the tool is the model -> service -> envelope
bridge for one item's drop-across-stages comparison. These drive it end to end
against the same production read-only path using the pinned 4-4 fixture plus
several directly-seeded stages that all drop one item (each with its own sanity cost
/ drop rate / expiry) so the ranking + a per-stage fresh/stale split are
deterministic (no wall-clock coupling). They assert:

* ``include_efficiency`` ranks the stages ascending by sanity per item over ≥2
  stages, never a best-farm/mandatory verdict, and carries the mandatory
  availability / first-clear / byproduct comparison caveats;
* The item is resolved per region + provenance rides every delivered result;
  an en item's comparison never surfaces a cn stage (en/cn never mixed);
* Each stage carries its OWN penguin provenance chain, and an expired
  stage flips the status to ``data_stale`` + adds a staleness limitation while
  staying in the ranking (downgraded below the recommendation threshold, not dropped);
* The typed envelope shape, incl. fail-closed ``not_found`` /
  ``database_unavailable`` / ``internal_error`` with no path/trace leak;
* The input gate + the wire contract: a read-only spec with a bounded input
  schema, present in the single shared registry both transports dispatch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.support.drops import (
    FUTURE_EXPIRY,
    PAST_EXPIRY,
    StageDropSeed,
    seed_item_across_stages,
    seed_item_without_drops,
)

from arknights_mcp.db.connection import DatabaseUnavailable, open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.envelopes import SCHEMA_VERSION
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.mcp.tools._enum_legend import OPEN_ENUM_LIMITATIONS
from arknights_mcp.mcp.tools._shared import CONFIDENCE_SCALE_NOTE
from arknights_mcp.mcp.tools.drops import build_get_item_drops_spec
from arknights_mcp.models.common import MAX_ID_LEN
from arknights_mcp.services.drops import get_item_drops
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

_PROSCRIBED = ("best farm", "best-farm", "mandatory", "must ", "should ", "always farm")


def _candidate(tmp_path: Path) -> Path:
    """Build the 4-4 fixture candidate (the penguin source is already registered)."""
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    return path


def _handler(conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    return build_get_item_drops_spec(lambda: conn).handler


# --- drop facts + region + penguin provenance ---------------------------------


def test_ok_returns_per_stage_facts_with_penguin_provenance(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4"), StageDropSeed("a-1")])
    env = _handler(open_read_only(path))(server="en", game_id="sugar")
    assert env.status == "ok"
    assert env.schema_version == SCHEMA_VERSION
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    # The stages section + its bounded page + the hoisted shared
    # drop_provenance block; no efficiency block without the flag.
    assert set(data) == {"item", "drop_provenance", "stages", "stages_page", "enum_legend"}
    assert data["item"]["game_id"] == "sugar"  # type: ignore[index]
    # The penguin provenance shared by both stages is hoisted once.
    prov = data["drop_provenance"]
    assert prov == {  # type: ignore[comparison-overlap]
        "snapshot_id": "pg:en",
        "fetched_at": "2026-07-19T00:00:00+00:00",
        "expires_at": FUTURE_EXPIRY,
        "imported_at": "2026-07-19T00:00:00+00:00",
    }
    # Region stated ONCE on the parent item, never per stage row.
    assert data["item"]["server"] == "en"  # type: ignore[index]
    stages = data["stages"]
    assert isinstance(stages, list) and len(stages) == 2
    # A two-stage item fits page 1 with no further page.
    assert data["stages_page"] == {"page": 1, "page_size": 50, "total": 2, "has_more": False}  # type: ignore[index]
    for stage in stages:
        assert "region" not in stage
        assert stage["sanity_cost"] is not None
        # The shared provenance is NOT repeated per stage; a fresh stage
        # omits ``expired``.
        for hoisted in ("snapshot_id", "fetched_at", "expires_at", "imported_at"):
            assert hoisted not in stage
        assert "expired" not in stage


def test_drop_rate_rounded_to_4dp_on_wire(tmp_path: Path) -> None:
    # The per-stage penguin drop_rate (here 1/3) is emitted rounded to 4dp on the
    # reverse item->stage rows as well, never the raw 17-digit ``repr`` float.
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4", drop_rate=1 / 3, times=3000)])
    data = _handler(open_read_only(path))(server="en", game_id="sugar").to_dict()["data"]
    stages = data["stages"]  # type: ignore[index]
    assert stages[0]["drop_rate"] == 0.3333
    assert stages[0]["drop_rate"] != 1 / 3


def test_ok_carries_region_and_provenance(tmp_path: Path) -> None:
    # Every delivered fact carries region + (penguin) provenance.
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4"), StageDropSeed("a-1")])
    prov = _handler(open_read_only(path))(server="en", game_id="sugar").to_dict()["provenance"]
    assert isinstance(prov, list) and len(prov) == 1  # one penguin snapshot for en
    assert prov[0]["server"] == "en"
    assert prov[0]["snapshot_id"] == "pg:en" and prov[0]["imported_at"]


def test_wrong_region_is_not_found(tmp_path: Path) -> None:
    # En item drops are not surfaced under a cn query -- en/cn never mixed.
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4"), StageDropSeed("a-1")])
    assert _handler(open_read_only(path))(server="cn", game_id="sugar").status == "not_found"


# --- ranked ascending over ≥2 stages ------------------------------------------


def test_include_efficiency_ranks_ascending_by_sanity_per_item(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("4-4", sanity_cost=18, drop_rate=0.25),  # 72
            StageDropSeed("a-1", sanity_cost=6, drop_rate=0.5),  # 12
            StageDropSeed("b-2", sanity_cost=30, drop_rate=0.25),  # 120
        ],
    )
    env = _handler(open_read_only(path))(server="en", game_id="sugar", include_efficiency=True)
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert "efficiency" in data
    # ONE ranked observation over the stages, not a list of per-stage observations.
    ob = data["efficiency"]["observation"]  # type: ignore[index]
    assert isinstance(ob, dict)
    assert set(ob) >= {"rule_id", "ranking", "confidence", "limitations", "analyzer_version"}
    assert ob["rule_id"] == "farming.sanity_per_item"
    ranking = ob["ranking"]
    assert isinstance(ranking, list) and len(ranking) == 3
    # Ranked ascending by sanity per item -> stage a-1 (12) first, b-2 (120) last.
    # The row id is the unambiguous stage_game_id and the code
    # rides in ``stage_code``. It used to ride in a generic ``name`` -- but "a-1" is a
    # CODE, not a display name, and the sibling get_stage_drops put an item display name
    # under that same key, so one shape carried two referents.
    assert [row["stage_code"] for row in ranking] == ["a-1", "4-4", "b-2"]
    assert [row["sanity_per_item"] for row in ranking] == [12.0, 72.0, 120.0]
    # The ranking SUBSUMES the stage rows -- no separate stages list is
    # emitted, and each ranking row folds the raw drop facts (evidence:
    # sanity_cost / drop_rate / times / quantity) alongside its derived sanity_per_item.
    assert "stages" not in data and "stages_page" not in data
    for row in ranking:
        assert {"sanity_cost", "drop_rate", "times", "quantity"} <= set(row)
    # The row id is the unambiguous stage_game_id, distinct from the shared code.
    # Neither generic key survives -- a client cannot be handed ``id``/``name``
    # whose referent depends on which of the two sibling tools it called.
    assert all(row["stage_game_id"] != row["stage_code"] for row in ranking)
    assert all("id" not in row and "name" not in row for row in ranking)
    # Ascending -> the cheapest stage (a-1, sanity_cost 6) is first, with its facts.
    assert ranking[0]["sanity_cost"] == 6 and ranking[0]["drop_rate"] == 0.5
    # The mandatory comparison caveats ride the observation-level limitations.
    blob = " ".join(ob["limitations"]).lower()
    assert "availability" in blob and "byproduct" in blob
    # The analyzer version rides the envelope too.
    assert env.analyzer_version is not None
    # An ordering + evidence, never a prescriptive verdict.
    text = str(data["efficiency"]).lower()
    assert not any(word in text for word in _PROSCRIBED)


def test_efficiency_omitted_without_the_flag(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4"), StageDropSeed("a-1")])
    env = _handler(open_read_only(path))(server="en", game_id="sugar")
    assert "efficiency" not in env.to_dict()["data"]
    assert env.analyzer_version is None


def test_include_efficiency_omits_stages_ranking_subsumes(tmp_path: Path) -> None:
    # With include_efficiency the ranked observation is the SINGLE
    # per-stage list -- each row folds the raw drop facts + its sanity_per_item, so no
    # separate stages[]/stages_page is emitted and the same stages are never listed twice.
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("4-4", sanity_cost=18, drop_rate=0.25, times=5000),
            StageDropSeed("a-1", sanity_cost=6, drop_rate=0.5, times=5000),
        ],
    )
    env = _handler(open_read_only(path))(server="en", game_id="sugar", include_efficiency=True)
    assert env.status == "ok"
    data = env.to_dict()["data"]
    # No duplicate per-stage list: stages/stages_page are subsumed by the ranking.
    assert "stages" not in data and "stages_page" not in data
    assert set(data) == {"item", "drop_provenance", "efficiency", "enum_legend"}
    ranking = data["efficiency"]["observation"]["ranking"]  # type: ignore[index]
    assert len(ranking) == 2
    # Evidence rides each ranking row (facts folded in) + the derived figure.
    cheapest = ranking[0]
    assert cheapest["stage_code"] == "a-1"
    assert cheapest["sanity_cost"] == 6
    assert cheapest["times"] == 5000
    assert cheapest["drop_rate"] == 0.5
    assert cheapest["sanity_per_item"] == 12.0
    # The shared penguin provenance is still hoisted once; a fresh row omits it.
    assert data["drop_provenance"]["snapshot_id"] == "pg:en"  # type: ignore[index]
    for hoisted in ("snapshot_id", "fetched_at", "expires_at", "imported_at"):
        assert hoisted not in cheapest
    # A fresh row omits expired.
    assert "expired" not in cheapest


# --- normal + tough share a stage_code -> DISTINCT joinable refs ---------------


def test_v68_normal_and_tough_same_code_get_distinct_joinable_refs(tmp_path: Path) -> None:
    # Two stages sharing stage_code "14-18" (a normal + tough pair) get
    # DISTINCT evidence refs -- each the unambiguous stage_game_id -- with the shared
    # code shown alongside as ``name``, so the refs join 1:1 to the sibling stages facts
    # list (which keys on stage_game_id) instead of colliding on one undecidable "14-18".
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("14-18", stage_game_id="main_10-09", sanity_cost=18, drop_rate=0.25),
            StageDropSeed("14-18", stage_game_id="tough_10-09", sanity_cost=36, drop_rate=0.25),
        ],
    )
    env = _handler(open_read_only(path))(server="en", game_id="sugar", include_efficiency=True)
    assert env.status == "ok"
    data = env.to_dict()["data"]
    ranking = data["efficiency"]["observation"]["ranking"]  # type: ignore[index]
    refs = [row["stage_game_id"] for row in ranking]
    # main_10-09 = 18/0.25 = 72, tough_10-09 = 36/0.25 = 144 -> ascending main then tough.
    assert refs == ["main_10-09", "tough_10-09"]
    assert len(set(refs)) == 2  # two DISTINCT refs, not one ambiguous "14-18"
    # The shared stage_code rides alongside in its own ``*_code`` key, never
    # as the ref and never mislabelled as a name.
    assert all(row["stage_code"] == "14-18" for row in ranking)
    assert "14-18" not in refs
    # The ranking subsumes the stage rows -- the distinct refs live on the
    # single ranking list (no separate stages list to join to).
    assert "stages" not in data
    assert set(refs) == {"main_10-09", "tough_10-09"}


# --- region-scoped -- an en item never surfaces a cn stage ---------------------


def test_comparison_is_region_scoped(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("4-4", region="en", sanity_cost=18, drop_rate=0.25),
            StageDropSeed("cn-1", region="cn", sanity_cost=6, drop_rate=0.5),
        ],
    )
    env = _handler(open_read_only(path))(server="en", game_id="sugar", include_efficiency=True)
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert data["item"]["server"] == "en"  # type: ignore[index]
    # Efficiency mode -> the ranking subsumes the stages (no separate list).
    assert "stages" not in data
    ranking = data["efficiency"]["observation"]["ranking"]  # type: ignore[index]
    # Region stated ONCE on the parent item, never per ranking row; only
    # the en stage is ranked -- the cn stage never leaks in.
    assert [row["stage_code"] for row in ranking] == ["4-4"]
    assert all("region" not in row for row in ranking)
    # Provenance is en-only.
    prov = env.to_dict()["provenance"]
    assert {p["server"] for p in prov} == {"en"}  # type: ignore[index]


# --- expired stage -> data_stale, downgraded but still ranked ------------------


def test_expired_stage_is_data_stale_but_still_ranked(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("4-4", sanity_cost=18, drop_rate=0.25, expires_at=FUTURE_EXPIRY),
            StageDropSeed("a-1", sanity_cost=6, drop_rate=0.5, expires_at=PAST_EXPIRY),
        ],
    )
    env = _handler(open_read_only(path))(server="en", game_id="sugar", include_efficiency=True)
    assert env.status == "data_stale"
    data = env.to_dict()["data"]
    # The ranking subsumes the stages -- no separate stages list.
    assert "stages" not in data
    obs = data["efficiency"]["observation"]  # type: ignore[index]
    ranking = obs["ranking"]
    names = [row["stage_code"] for row in ranking]
    assert "a-1" in names
    # The expired stage is flagged on its ranking row, not withheld -- still ranked.
    expired_row = next(row for row in ranking if row["stage_code"] == "a-1")
    assert expired_row["expired"] is True
    # The expired row is downgraded below the recommendation threshold.
    assert expired_row["confidence"] < 0.5
    # The expiry sentence is hoisted ONCE onto the observation-level
    # limitations; the row carries only the typed marker + its confidence.
    assert "limitations" not in expired_row
    assert any("expired" in lim.lower() for lim in obs["limitations"])
    # A staleness limitation names the refresh action; never presented as fresh.
    assert any("expiry" in lim or "stale" in lim for lim in env.limitations)


# --- the thin-sample sentence is hoisted once; rows carry a flag --------------


def test_v85_thin_sample_sentence_hoisted_once_not_per_row(tmp_path: Path) -> None:
    # Every stage here has a thin sample (< 100 runs). The identical
    # "below the floor" sentence must appear EXACTLY ONCE at the observation level
    # (in the live eval it repeated verbatim on ~20 ranked rows); each row keeps only
    # the typed flag + its own reduced confidence.
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("4-4", sanity_cost=18, drop_rate=0.25, times=40),
            StageDropSeed("a-1", sanity_cost=6, drop_rate=0.5, times=55),
            StageDropSeed("b-2", sanity_cost=30, drop_rate=0.25, times=10),
        ],
    )
    env = _handler(open_read_only(path))(server="en", game_id="sugar", include_efficiency=True)
    assert env.status == "ok"
    ob = env.to_dict()["data"]["efficiency"]["observation"]  # type: ignore[index]
    thin_sentences = [lim for lim in ob["limitations"] if "floor" in lim.lower()]
    assert len(thin_sentences) == 1
    for row in ob["ranking"]:
        assert row["flags"] == ["thin_sample"]
        assert row["confidence"] < 0.5  # per-row confidence stays
        assert "limitations" not in row  # no per-row sentence repeats


# --- provenance hoist -- shared block + only the deviant row carries its own ---


def test_provenance_hoist_surfaces_only_the_deviant_stage(tmp_path: Path) -> None:
    # The penguin provenance shared by the stages is hoisted to one block; a
    # stage repeats a field ONLY where it deviates (here a different expiry), and a
    # fresh stage omits ``expired`` -- so the stale/deviant stage stays visible.
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("4-4", expires_at=FUTURE_EXPIRY),
            StageDropSeed("a-1", expires_at=PAST_EXPIRY),
        ],
    )
    env = _handler(open_read_only(path))(server="en", game_id="sugar")
    assert env.status == "data_stale"
    data = env.to_dict()["data"]
    # The shared block is the common (first-seen fresh) provenance.
    assert data["drop_provenance"]["expires_at"] == FUTURE_EXPIRY  # type: ignore[index]
    stages = {s["stage_code"]: s for s in data["stages"]}  # type: ignore[index]
    fresh, stale = stages["4-4"], stages["a-1"]
    # The fresh stage matches the shared block: no per-row provenance, no expired flag.
    for hoisted in ("snapshot_id", "fetched_at", "expires_at", "imported_at"):
        assert hoisted not in fresh
    assert "expired" not in fresh
    # The stale stage deviates: it carries its OWN (past) expiry + expired:true.
    assert stale["expires_at"] == PAST_EXPIRY
    assert stale["expired"] is True


# --- both growable lists are paged --------------------------------------------


def test_stages_are_paged(tmp_path: Path) -> None:
    # The per-stage facts page through their own bounded cursor so a common
    # item (dropping across many stages) never overflows the response cap.
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path, [StageDropSeed("a-1"), StageDropSeed("b-2"), StageDropSeed("c-3")]
    )
    handler = _handler(open_read_only(path))
    env1 = handler(server="en", game_id="sugar", stages_page={"page": 1, "page_size": 2})
    data1 = env1.to_dict()["data"]
    # Ordered by stage_code; page 1 holds the first two + signals another bounded page.
    assert [s["stage_code"] for s in data1["stages"]] == ["a-1", "b-2"]  # type: ignore[index]
    assert data1["stages_page"] == {"page": 1, "page_size": 2, "total": 3, "has_more": True}  # type: ignore[index]
    env2 = handler(server="en", game_id="sugar", stages_page={"page": 2, "page_size": 2})
    data2 = env2.to_dict()["data"]
    assert [s["stage_code"] for s in data2["stages"]] == ["c-3"]  # type: ignore[index]
    assert data2["stages_page"]["has_more"] is False  # type: ignore[index]


def test_efficiency_observations_are_paged_over_global_ranking(tmp_path: Path) -> None:
    # The ranking is computed over the FULL set, THEN sliced -- page 1 is
    # the most-efficient N in GLOBAL order, never a per-page re-rank.
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("e1", sanity_cost=6, drop_rate=0.5),  # 12
            StageDropSeed("e2", sanity_cost=18, drop_rate=0.25),  # 72
            StageDropSeed("e3", sanity_cost=30, drop_rate=0.25),  # 120
            StageDropSeed("e4", sanity_cost=10, drop_rate=0.5),  # 20
            StageDropSeed("e5", sanity_cost=40, drop_rate=0.25),  # 160
        ],
    )
    handler = _handler(open_read_only(path))
    eff1 = handler(
        server="en",
        game_id="sugar",
        include_efficiency=True,
        efficiency_page={"page": 1, "page_size": 2},
    ).to_dict()["data"]["efficiency"]  # type: ignore[index]
    # ONE observation; its ``ranking`` rows are this page of the global ranking.
    # Global ascending: e1(12), e4(20), e2(72), e3(120), e5(160) -> page 1 = the two lowest.
    # The row id is the stage_game_id; assert order by the stage_code display name.
    assert [row["stage_code"] for row in eff1["observation"]["ranking"]] == ["e1", "e4"]
    # Each ranking row folds the raw drop facts (evidence).
    assert all(
        {"sanity_cost", "drop_rate", "times"} <= set(r) for r in eff1["observation"]["ranking"]
    )
    assert eff1["page"] == {"page": 1, "page_size": 2, "total": 5, "has_more": True}
    eff2 = handler(
        server="en",
        game_id="sugar",
        include_efficiency=True,
        efficiency_page={"page": 2, "page_size": 2},
    ).to_dict()["data"]["efficiency"]  # type: ignore[index]
    # Page 2 continues the SAME global ranking (not the two lowest of a fresh re-rank).
    assert [row["stage_code"] for row in eff2["observation"]["ranking"]] == ["e2", "e3"]
    assert eff2["page"]["has_more"] is True
    # The mandatory comparison caveats ride the observation on every page.
    assert "availability" in " ".join(eff2["observation"]["limitations"]).lower()


def test_stale_holds_when_expired_stage_off_page(tmp_path: Path) -> None:
    # The stale verdict is computed over the FULL set, so data_stale holds
    # even when the only expired stage falls on a later page -- page 1 is never
    # presented as fresh just because its rows happen to be unexpired.
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("a-1", expires_at=FUTURE_EXPIRY),
            StageDropSeed("z-9", expires_at=PAST_EXPIRY),
        ],
    )
    env = _handler(open_read_only(path))(
        server="en", game_id="sugar", stages_page={"page": 1, "page_size": 1}
    )
    assert env.status == "data_stale"
    data = env.to_dict()["data"]
    # The returned page holds only the fresh stage; the expired one is off-page...
    assert [s["stage_code"] for s in data["stages"]] == ["a-1"]  # type: ignore[index]
    # The fresh page-1 stage omits ``expired`` (absence = not expired).
    assert "expired" not in data["stages"][0]  # type: ignore[operator]
    assert data["stages_page"]["has_more"] is True  # type: ignore[index]
    # ...yet the staleness posture holds (never presented as fresh).
    assert any("expiry" in lim or "stale" in lim for lim in env.limitations)


def test_hoisted_deviation_sentence_rides_every_efficiency_page(tmp_path: Path) -> None:
    # The hoisted deviation sentences are computed over the FULL
    # ranking and ride the observation onto EVERY page -- a page whose rows carry no
    # ``expired`` marker still states the posture (the marked row lives on a later
    # page), and the marked row's own page carries both the marker and the sentence.
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("a-1", drop_rate=0.25, expires_at=FUTURE_EXPIRY),  # ranks first
            StageDropSeed("z-9", drop_rate=0.05, expires_at=PAST_EXPIRY),  # ranks second
        ],
    )
    handler = _handler(open_read_only(path))
    page1 = handler(
        server="en",
        game_id="sugar",
        include_efficiency=True,
        efficiency_page={"page": 1, "page_size": 1},
    )
    assert page1.status == "data_stale"
    ob1 = page1.to_dict()["data"]["efficiency"]["observation"]  # type: ignore[index]
    rows1 = ob1["ranking"]
    assert [r["stage_code"] for r in rows1] == ["a-1"]
    # The fresh page-1 row carries no marker (item view keys expiry per row)...
    assert "expired" not in rows1[0] and "confidence" not in rows1[0]
    # ...but the full-set hoisted sentence still rides this page's observation.
    assert any("expired" in lim for lim in ob1["limitations"])
    page2 = handler(
        server="en",
        game_id="sugar",
        include_efficiency=True,
        efficiency_page={"page": 2, "page_size": 1},
    )
    ob2 = page2.to_dict()["data"]["efficiency"]["observation"]  # type: ignore[index]
    rows2 = ob2["ranking"]
    assert [r["stage_code"] for r in rows2] == ["z-9"]
    assert rows2[0]["expired"] is True and rows2[0]["confidence"] < 0.5
    assert any("expired" in lim for lim in ob2["limitations"])


def test_out_of_range_page_size_rejected(tmp_path: Path) -> None:
    # An out-of-range page_size is rejected at the model gate, never silently
    # widened -- one contract, both places (mirrors get_stage).
    with pytest.raises(ValidationError):
        _handler(open_read_only(_candidate(tmp_path)))(
            server="en", game_id="sugar", stages_page={"page_size": 101}
        )


# --- absent item / no drop cache -> not_found, no fetch fallback --------------


def test_absent_item_is_not_found(tmp_path: Path) -> None:
    env = _handler(open_read_only(_candidate(tmp_path)))(server="en", game_id="nonexistent_item")
    assert env.status == "not_found"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    action = data["suggested_action"]
    # Never a query-time download/scrape fallback.
    assert "download" not in str(action).lower() and "scrape" not in str(action).lower()
    # The pointer is honest -- search_entities now resolves item name -> id,
    # so the not_found action names it (no longer a dead-end pointer).
    assert "search_entities" in str(action)


def test_resolved_item_with_no_drops_is_a_distinct_empty_ok(tmp_path: Path) -> None:
    # An item that RESOLVES but has zero stage-drop cache is a craft/synthesis-only
    # material -- it will NEVER have a penguin drop row, so its empty answer must NOT read
    # like an unknown-item miss (which points at an admin re-sync). It gets a distinct
    # sentence + a freshness self-check pointer (get_data_status), never the re-sync that
    # would add nothing.
    #
    # Only the STATUS moved: the item resolved, so the lookup succeeded and
    # the empty comparison is an ``ok``. The two-way split is what this test guards, and
    # it must survive that move intact -- which is why both arms are asserted here.
    path = _candidate(tmp_path)
    seed_item_without_drops(path, item_game_id="30155", item_display_name="Nucleic Crystal Sinter")
    env = _handler(open_read_only(path))(server="en", game_id="30155")
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    # The empty collection is emitted, and the item it is about is named.
    assert data["stages"] == []
    assert data["item"]["game_id"] == "30155"  # type: ignore[index]
    caveat = next(lim for lim in env.limitations if "no stage that drops it" in lim)
    # The sentence says the item EXISTS (not an unknown-id miss).
    assert "exists" in caveat.lower()
    # It points a freshness self-check, NOT the admin re-sync -- a synthesis-only
    # material has no drop to fetch, so the sync action would mislead as "cache unsynced".
    assert "get_data_status" in caveat
    assert "arknights-mcp" not in caveat  # no admin re-sync command
    assert "synthesis" in caveat.lower() or "workshop" in caveat.lower()
    # Still never a query-time download/scrape fallback.
    assert "download" not in caveat.lower() and "scrape" not in caveat.lower()
    # Distinct from the UNKNOWN-item arm, which stays a lookup miss pointing at
    # search_entities. The two must not converge on one status OR one wording.
    unknown = _handler(open_read_only(path))(server="en", game_id="nosuchitem")
    assert unknown.status == "not_found"
    unknown_action = str(unknown.to_dict()["data"]["suggested_action"])  # type: ignore[index]
    assert "search_entities" in unknown_action
    assert "search_entities" not in caveat


# --- typed failures -----------------------------------------------------------


def test_database_unavailable_envelope() -> None:
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: cand.sqlite")

    env = build_get_item_drops_spec(boom).handler(server="en", game_id="sugar")
    assert env.status == "database_unavailable"
    data = env.to_dict()["data"]
    assert data["message"] == "the active database is unavailable"  # type: ignore[index]
    assert "cand.sqlite" not in str(data)  # no local path / file name leak


def test_unexpected_error_fails_closed_to_internal_error() -> None:
    def boom() -> sqlite3.Connection:
        raise RuntimeError("secret path /home/ubuntu/db.sqlite blew up")

    env = build_get_item_drops_spec(boom).handler(server="en", game_id="sugar")
    assert env.status == "internal_error"
    assert str(env.to_dict()["data"]).find("/home/ubuntu") == -1
    assert "blew up" not in str(env.to_dict()["data"])


# --- input gate ----------------------------------------------------------------


def test_unknown_parameter_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _handler(open_read_only(_candidate(tmp_path)))(
            server="en", game_id="sugar", stage_code="4-4"
        )


def test_bad_region_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _handler(open_read_only(_candidate(tmp_path)))(server="jp", game_id="sugar")


def test_missing_game_id_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _handler(open_read_only(_candidate(tmp_path)))(server="en")


def test_over_length_game_id_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _handler(open_read_only(_candidate(tmp_path)))(server="en", game_id="x" * (MAX_ID_LEN + 1))


# --- read-only / wire contract / shared registry ------------------------------


def test_service_is_read_only(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4"), StageDropSeed("a-1")])
    conn = open_read_only(path)
    before = conn.total_changes
    get_item_drops(conn, server="en", game_id="sugar", include_efficiency=True)
    assert conn.total_changes == before


def test_spec_registers_read_only_with_bounded_schema(tmp_path: Path) -> None:
    conn = open_read_only(_candidate(tmp_path))
    reg = ToolRegistry()
    spec = reg.register(build_get_item_drops_spec(lambda: conn))
    assert reg.names() == ("get_item_drops",)
    assert spec.read_only is True
    tool = spec.to_mcp_tool()
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True
    assert tool.inputSchema["additionalProperties"] is False
    assert "server" in tool.inputSchema["required"]
    props = tool.inputSchema["properties"]
    # The item selector is on the wire; the id cap rides it (required ->
    # a direct string schema with a maxLength).
    assert "game_id" in props
    assert "include_efficiency" in props
    assert props["game_id"]["maxLength"] == MAX_ID_LEN


def test_tool_registered_in_shared_registry(tmp_path: Path) -> None:
    # Both transports dispatch this one registry; the tool must be in it.
    conn = open_read_only(_candidate(tmp_path))
    reg = build_tool_registry(lambda: conn, registry=load_source_registry(REGISTRY), mode="stdio")
    assert "get_item_drops" in reg.names()


def test_item_type_domain_and_openness_ride_the_response(tmp_path: Path) -> None:
    # The item block carries item_type, so its STATIC 9-token
    # domain rides ``enum_legend`` and the source-defined "may grow" caveat rides a
    # limitation -- the two homes, neither of them the description. The field once went
    # undocumented on both drop tools; the fix stayed, the home
    # moved off the description budget it shared with every other mandated fact.
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4")])
    env = _handler(open_read_only(path))(server="en", game_id="sugar")
    data = env.to_dict()["data"]
    assert set(data["enum_legend"]) == {"item_type"}  # type: ignore[arg-type,index]
    assert OPEN_ENUM_LIMITATIONS["item_type"] in env.to_dict()["limitations"]
    assert "may grow" not in build_get_item_drops_spec(lambda: None).description  # type: ignore[arg-type,misc]


def test_confidence_scale_rides_only_the_efficiency_response(tmp_path: Path) -> None:
    # Stated ONCE per response that carries a confidence; a caller who
    # never asks for a ranking no longer pays for the scale in the description either.
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4"), StageDropSeed("a-1")])
    conn = open_read_only(path)
    with_eff = _handler(conn)(server="en", game_id="sugar", include_efficiency=True)
    assert with_eff.to_dict()["limitations"].count(CONFIDENCE_SCALE_NOTE) == 1  # type: ignore[union-attr]
    without = _handler(conn)(server="en", game_id="sugar")
    assert CONFIDENCE_SCALE_NOTE not in without.to_dict()["limitations"]
