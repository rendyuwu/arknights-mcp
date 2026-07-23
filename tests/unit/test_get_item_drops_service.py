"""§T103 ``get_item_drops`` read-layer / service tests (§V60/§V54/§V55/§V5/§V24/§V37).

The reverse of ``get_stage_drops``: for a fixed item, the service resolves the item
PER region and ranks the stages that drop it ascending by sanity per item. These
drive the shared service directly against the same production read-only path (§V2),
seeding several synthetic stages that all drop one item (each with its own sanity
cost / drop rate / expiry) so a ranking, a per-stage stale verdict, and the region
scope are all deterministic (no wall-clock coupling). They assert:

* §V60: the comparison is ranked ascending by sanity per item, an expired stage's
  figure is downgraded but KEPT (never dropped), and the mandatory availability /
  first-clear / byproduct caveats ride the result;
* §V5: the item is resolved per region and only same-region stages are ranked -- an
  en item's comparison never surfaces a cn stage;
* §V54: every per-stage figure carries its OWN penguin provenance chain;
* §V24: an absent item, or an item with no drop cache, is ``not_found`` (no ranking),
  never a query-time download/scrape fallback;
* §V2: the service performs no writes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.support.drops import (
    FUTURE_EXPIRY,
    PAST_EXPIRY,
    StageDropSeed,
    seed_item_across_stages,
)

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
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


def _ranking(result):  # type: ignore[no-untyped-def]
    """The ranking rows of the single ranked observation (§V66.1)."""
    assert result.observation is not None
    return result.observation.ranking


# --- §V60: ranked ascending over ≥2 stages ------------------------------------


def test_ranked_ascending_by_sanity_per_item(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("4-4", sanity_cost=18, drop_rate=0.25),  # 72
            StageDropSeed("a-1", sanity_cost=6, drop_rate=0.5),  # 12
            StageDropSeed("b-2", sanity_cost=30, drop_rate=0.25),  # 120
        ],
    )
    conn = open_read_only(path)
    result = get_item_drops(conn, server="en", game_id="sugar", include_efficiency=True)
    assert result.status == "ok"
    assert result.item is not None and result.item.game_id == "sugar"
    # §V66.1: ONE ranked observation; its ranking rows are ascending by sanity per item.
    ranking = _ranking(result)
    assert [row.sanity_per_item for row in ranking] == [12.0, 72.0, 120.0]
    # §V68/B57: the row id is the unambiguous stage_game_id, with the stage_code shown
    # alongside as name.
    assert [row.name for row in ranking] == ["a-1", "4-4", "b-2"]
    assert all(row.id != row.name for row in ranking)
    # §T161/B82: in efficiency mode the service realigns result.stages 1:1 with the
    # ranking PAGE (same order) so the shaper folds each fact into its ranking row and
    # drops the separate stages list.
    assert result.stages_page is None
    assert len(result.stages) == len(ranking)
    assert [s.stage_game_id for s in result.stages] == [row.id for row in ranking]
    # §V60/§V66.1: the mandatory comparison caveats ride the observation-level limitations.
    obs = result.observation
    assert obs is not None
    blob = " ".join(obs.limitations).lower()
    assert "availability" in blob and "byproduct" in blob
    # §V7/§V55: an ordering + evidence, never a prescriptive verdict.
    text = f"{obs.title} {obs.summary} {' '.join(obs.limitations)}".lower()
    assert not any(word in text for word in _PROSCRIBED)


def test_facts_present_without_the_efficiency_flag(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4"), StageDropSeed("a-1")])
    conn = open_read_only(path)
    result = get_item_drops(conn, server="en", game_id="sugar")
    assert result.status == "ok"
    assert len(result.stages) == 2
    # Without the flag: raw facts only, no ranked observation.
    assert result.observation is None
    assert result.analyzer_version is None


# --- §V54: per-stage penguin provenance chain ---------------------------------


def test_each_stage_carries_penguin_provenance(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4"), StageDropSeed("a-1")])
    conn = open_read_only(path)
    result = get_item_drops(conn, server="en", game_id="sugar")
    for stage in result.stages:
        assert stage.region == "en"
        assert stage.snapshot_id == "pg:en"
        assert stage.fetched_at and stage.expires_at and stage.imported_at
        assert stage.sanity_cost is not None


# --- §V5: region-scoped -- an en item never surfaces a cn stage ----------------


def test_comparison_is_region_scoped(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("4-4", region="en", sanity_cost=18, drop_rate=0.25),
            StageDropSeed("cn-1", region="cn", sanity_cost=6, drop_rate=0.5),
        ],
    )
    conn = open_read_only(path)
    en = get_item_drops(conn, server="en", game_id="sugar", include_efficiency=True)
    # Only the en stage is ranked; the cn stage never leaks into the en comparison.
    assert en.status == "ok"
    assert {s.region for s in en.stages} == {"en"}
    assert [row.name for row in _ranking(en)] == ["4-4"]
    assert {row.id for row in _ranking(en)} <= {s.stage_game_id for s in en.stages}
    # The item resolves independently per region; the cn item ranks only cn stages.
    cn = get_item_drops(conn, server="cn", game_id="sugar", include_efficiency=True)
    assert cn.status == "ok"
    assert {s.region for s in cn.stages} == {"cn"}
    assert [row.name for row in _ranking(cn)] == ["cn-1"]
    assert {row.id for row in _ranking(cn)} <= {s.stage_game_id for s in cn.stages}


# --- §V53/§V60: expired stage downgraded but KEPT in the ranking ---------------


def test_expired_stage_is_stale_but_still_ranked(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(
        path,
        [
            StageDropSeed("4-4", sanity_cost=18, drop_rate=0.25, expires_at=FUTURE_EXPIRY),
            StageDropSeed("a-1", sanity_cost=6, drop_rate=0.5, expires_at=PAST_EXPIRY),
        ],
    )
    conn = open_read_only(path)
    result = get_item_drops(conn, server="en", game_id="sugar", include_efficiency=True)
    assert result.status == "data_stale"
    assert result.stale is True
    # §V60: the expired stage is flagged, not withheld -- still present in facts + ranking.
    expired_stage = next(s for s in result.stages if s.stage_code == "a-1")
    assert expired_stage.expired is True
    ranking = _ranking(result)
    names = [row.name for row in ranking]
    assert "a-1" in names
    expired_row = next(row for row in ranking if row.name == "a-1")
    assert expired_row.confidence is not None and expired_row.confidence < 0.5
    assert any("expired" in lim for lim in expired_row.limitations)


# --- §V24: absent item / no drop cache -> not_found, no fetch fallback ---------


def test_absent_item_is_not_found(tmp_path: Path) -> None:
    conn = open_read_only(_candidate(tmp_path))
    result = get_item_drops(conn, server="en", game_id="nonexistent_item")
    assert result.status == "not_found"
    assert result.item is None and result.stages == ()


def test_item_with_no_drop_cache_is_not_found(tmp_path: Path) -> None:
    # An item that exists but has no stage_drops rows still reports absent (§V24) --
    # there is no comparison to rank.
    path = _candidate(tmp_path)
    conn0 = sqlite3.connect(str(path))
    try:
        conn0.execute(
            "INSERT INTO source_snapshots (snapshot_id, source_id, server, fetched_at, "
            "imported_at, manifest_hash, status, field_policy_version) VALUES "
            "('pg:en', 'penguin_statistics', 'en', '2026-07-19T00:00:00+00:00', "
            "'2026-07-19T00:00:00+00:00', 'ph', 'imported', '1')"
        )
        prov_id = conn0.execute(
            "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
            "record_hash, transform_version, field_policy_version) VALUES "
            "('pg:en', 'items', 'orphan', 'rh', '1', '1')"
        ).lastrowid
        conn0.execute(
            "INSERT INTO items (server, game_id, display_name, rarity, item_type, provenance_id) "
            "VALUES ('en', 'orphan', 'Orphan', '3', 'MATERIAL', ?)",
            (prov_id,),
        )
        conn0.commit()
    finally:
        conn0.close()
    conn = open_read_only(path)
    result = get_item_drops(conn, server="en", game_id="orphan")
    assert result.status == "not_found"
    assert result.stages == ()


# --- §V2: read-only ------------------------------------------------------------


def test_service_is_read_only(tmp_path: Path) -> None:
    path = _candidate(tmp_path)
    seed_item_across_stages(path, [StageDropSeed("4-4"), StageDropSeed("a-1")])
    conn = open_read_only(path)
    before = conn.total_changes
    get_item_drops(conn, server="en", game_id="sugar", include_efficiency=True)
    assert conn.total_changes == before
