"""T21: the network source adapter/stager (§V1, §V18 allowlist + limits).

Exercises the safety machinery of :class:`ArknightsAssetsAdapter` with an
in-memory fetcher (no live network): HTTPS enforcement, the path allowlist, and
the per-file / total-size / JSON-depth / JSON-node caps. Staging turns a remote
snapshot into an ordinary local read-only adapter the import pipeline consumes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.support import DictFetcher, dict_fetcher_from_snapshot

from arknights_mcp.sources.arknights_assets import (
    ArknightsAssetsAdapter,
    DownloadBudget,
    DownloadLimits,
    HttpsFetcher,
    _BoundedRedirectHandler,
    _validate_relative_path,
)
from arknights_mcp.sources.base import SourceAdapterError, SourceNotFoundError
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
BASE_URL = "https://example.test/repo"


def _fetcher() -> DictFetcher:
    return dict_fetcher_from_snapshot(BASE_URL, FIXTURE_ROOT)


def _fixture_files() -> dict[str, bytes]:
    """The fixture snapshot as a mutable ``{url: bytes}`` map (for B34 skip tests)."""
    return {
        f"{BASE_URL}/{p.relative_to(FIXTURE_ROOT).as_posix()}": p.read_bytes()
        for p in FIXTURE_ROOT.rglob("*")
        if p.is_file()
    }


# --- HTTPS enforcement (§V1) --------------------------------------------------


def test_base_url_must_be_https() -> None:
    with pytest.raises(SourceAdapterError, match="https"):
        ArknightsAssetsAdapter("http://example.test/repo", "en", fetcher=_fetcher())


def test_default_fetcher_refuses_non_https_url() -> None:
    with pytest.raises(SourceAdapterError, match="non-HTTPS"):
        HttpsFetcher().fetch("http://example.test/x.json", max_bytes=1024)


# --- path allowlist / traversal (§V18) ----------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["/etc/passwd", "../secrets.json", "gamedata/../../x", "config/secret.json", "notallowed/x"],
)
def test_paths_outside_allowlist_rejected(bad: str) -> None:
    with pytest.raises(SourceAdapterError):
        _validate_relative_path(bad)


def test_allowlisted_paths_accepted() -> None:
    assert _validate_relative_path("gamedata/excel/stage_table.json")
    assert _validate_relative_path("gamedata/levels/main/level_main_04-04.json")


# --- staging turns a remote snapshot into a local adapter ----------------------


def test_stage_downloads_allowlisted_into_local_adapter(tmp_path: Path) -> None:
    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=_fetcher())
    local = adapter.stage(tmp_path / "staging")

    assert isinstance(local, LocalSnapshotAdapter)
    assert local.server == "en"
    assert local.source_id == "arknights_assets_gamedata"
    # core files + the discovered level file were all staged
    assert local.exists("gamedata/excel/stage_table.json")
    assert local.exists("gamedata/levels/main/level_main_04-04.json")
    stage_table = local.read_json("gamedata/excel/stage_table.json")
    assert "main_04-04" in stage_table["stages"]


def test_stage_skips_pruned_level_files(tmp_path: Path) -> None:
    """B34/§V30: real ``stage_table`` keeps ``levelId`` refs for retired events whose
    level files are pruned from the snapshot; a 404 on a discovered level file is
    skipped, not fatal, so the whole sync still completes."""
    files = _fixture_files()
    # Add a stage referencing a level file that is NOT in the snapshot (a pruned
    # retired event). DictFetcher raises SourceNotFoundError for the unmapped URL.
    stage_table_url = f"{BASE_URL}/gamedata/excel/stage_table.json"
    stage_table = json.loads(files[stage_table_url])
    stage_table["stages"]["act10d5_01"] = {
        "stageId": "act10d5_01",
        "levelId": "Activities/ACT10d5/level_act10d5_01",
    }
    files[stage_table_url] = json.dumps(stage_table).encode("utf-8")

    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=DictFetcher(files))
    local = adapter.stage(tmp_path / "staging")  # must not raise

    # The present level was staged; the pruned one was skipped, not staged.
    assert local.exists("gamedata/levels/main/level_main_04-04.json")
    assert not local.exists("gamedata/levels/activities/act10d5/level_act10d5_01.json")


def test_stage_core_file_404_still_fatal(tmp_path: Path) -> None:
    """B34: a missing *core* file is never tolerated — a bad ``base_url`` (or a
    truly broken snapshot) must fail closed, not silently produce an empty build."""
    files = _fixture_files()
    del files[f"{BASE_URL}/gamedata/excel/zone_table.json"]  # core file absent
    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=DictFetcher(files))
    with pytest.raises(SourceNotFoundError):
        adapter.stage(tmp_path / "staging")


def test_level_discovery_normalizes_real_levelid() -> None:
    """§V29/§V36: a real Title-case levelId is rewritten to its snapshot path and
    enqueued (the old raw-prefix gate collected 0 real level files, B6b)."""
    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=_fetcher())
    real = {"stages": {"x": {"stageId": "x", "levelId": "Obt/Main/level_main_04-04"}}}
    assert adapter._discover_level_paths(real) == ["gamedata/levels/obt/main/level_main_04-04.json"]


def test_level_discovery_rejects_excel_paths() -> None:
    """L8/§V36: a crafted levelId aimed at an excel table is never enqueued; after
    normalization it folds under the levels prefix but the nested ``excel`` segment
    is refused, so the excel table is never fetched."""
    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=_fetcher())
    poisoned = {"stages": {"x": {"stageId": "x", "levelId": "gamedata/excel/character_table.json"}}}
    assert adapter._discover_level_paths(poisoned) == []


def test_level_discovery_rejects_traversal_levelid() -> None:
    """§V36: a levelId carrying a traversal fragment is dropped at discovery, so it
    can never escape the levels tree."""
    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=_fetcher())
    poisoned = {"stages": {"x": {"stageId": "x", "levelId": "../../secret"}}}
    assert adapter._discover_level_paths(poisoned) == []


@pytest.mark.parametrize(
    "encoded",
    [
        "gamedata/levels/..%2f..%2fsecret.json",
        "gamedata/levels/%2e%2e/%2e%2e/secret.json",
    ],
)
def test_percent_encoded_traversal_rejected(encoded: str) -> None:
    """L7: percent-encoded traversal is rejected (the server would decode %2f->/)."""
    with pytest.raises(SourceAdapterError):
        _validate_relative_path(encoded)


# --- resource caps (PRD §11.2) ------------------------------------------------


def test_per_file_size_cap(tmp_path: Path) -> None:
    limits = DownloadLimits(max_file_bytes=8)
    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=_fetcher(), limits=limits)
    with pytest.raises(SourceAdapterError, match="per-file cap"):
        adapter.stage(tmp_path / "staging")


def test_total_size_cap(tmp_path: Path) -> None:
    # Large enough for any single file, small enough to trip on the running total.
    limits = DownloadLimits(max_file_bytes=10_000, max_total_bytes=64)
    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=_fetcher(), limits=limits)
    with pytest.raises(SourceAdapterError, match="total download cap"):
        adapter.stage(tmp_path / "staging")


def test_json_depth_cap(tmp_path: Path) -> None:
    deep: dict[str, object] = {"enemyData": {}}
    node: dict[str, object] = deep
    for _ in range(20):
        child: dict[str, object] = {}
        node["nest"] = child
        node = child
    url = f"{BASE_URL}/gamedata/excel/enemy_handbook_table.json"
    fetcher = DictFetcher({url: json.dumps(deep).encode("utf-8")})
    limits = DownloadLimits(max_json_depth=4)
    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=fetcher, limits=limits)
    with pytest.raises(SourceAdapterError, match="depth cap"):
        adapter.stage(tmp_path / "staging")


def test_json_node_cap(tmp_path: Path) -> None:
    url = f"{BASE_URL}/gamedata/excel/enemy_handbook_table.json"
    payload = {"enemyData": {f"e{i}": {"name": i} for i in range(50)}}
    fetcher = DictFetcher({url: json.dumps(payload).encode("utf-8")})
    limits = DownloadLimits(max_json_nodes=5)
    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=fetcher, limits=limits)
    with pytest.raises(SourceAdapterError, match="node cap"):
        adapter.stage(tmp_path / "staging")


def test_deeply_nested_json_capped_gracefully(tmp_path: Path) -> None:
    """Pathologically deep JSON is rejected as a capped error, not a RecursionError."""
    url = f"{BASE_URL}/gamedata/excel/enemy_handbook_table.json"
    payload = b"[" * 100_000 + b"]" * 100_000  # ~200 KB, under the per-file cap
    fetcher = DictFetcher({url: payload})
    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=fetcher)
    with pytest.raises(SourceAdapterError, match="nesting depth"):
        adapter.stage(tmp_path / "staging")


# --- run-level total-download budget (PRD §11.2; shared across servers) --------


def test_download_budget_accumulates_run_level() -> None:
    budget = DownloadBudget(100)
    budget.charge(60)  # under cap
    with pytest.raises(SourceAdapterError, match="total download cap"):
        budget.charge(50)  # 110 > 100 across the run


# --- redirect same-domain policy (PRD §17.4) ----------------------------------


def test_redirect_refuses_cross_domain() -> None:
    handler = _BoundedRedirectHandler(5, allowed_host="example.test")
    with pytest.raises(SourceAdapterError, match="cross-domain"):
        handler.redirect_request(None, None, 302, "Found", {}, "https://attacker.example/x.json")


def test_redirect_refuses_non_https_target() -> None:
    handler = _BoundedRedirectHandler(5, allowed_host="example.test")
    with pytest.raises(SourceAdapterError, match="non-HTTPS"):
        handler.redirect_request(None, None, 302, "Found", {}, "http://example.test/x.json")
