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
    CORE_FILES,
    SUPPLEMENTARY_FILES,
    ArknightsAssetsAdapter,
    _validate_relative_path,
)
from arknights_mcp.sources.base import SourceAdapterError, SourceNotFoundError
from arknights_mcp.sources.http_fetch import (
    DownloadBudget,
    DownloadLimits,
    HttpsFetcher,
    _validate_redirect_target,
)
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
OPERATOR_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "operator" / "en"
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


# --- operator/module tables are fetched by sync (B36; §V41) --------------------


def test_core_files_cover_every_importer_table() -> None:
    """§V41: the sync staged-file set (CORE_FILES ∪ SUPPLEMENTARY_FILES) must be a
    superset of every ``gamedata/*.json`` source path any pipeline importer reads by
    default, or a real ``sync`` silently omits an in-scope domain. Operator/module
    tables import optional-zero at the pipeline, so their omission from the fetch set
    is NOT caught by the combat-scoped §V30 guard (B36). Introspecting the importer
    signatures keeps this honest: adding a domain without wiring its file fails here.
    """
    import inspect

    from arknights_mcp.importers.enemies import import_enemies
    from arknights_mcp.importers.modules import import_modules
    from arknights_mcp.importers.operators import import_operators
    from arknights_mcp.importers.stages import import_stages

    required: set[str] = set()
    for fn in (import_enemies, import_stages, import_operators, import_modules):
        for param in inspect.signature(fn).parameters.values():
            default = param.default
            if (
                isinstance(default, str)
                and default.startswith("gamedata/")
                and default.endswith(".json")
            ):
                required.add(default)

    staged = set(CORE_FILES) | set(SUPPLEMENTARY_FILES)
    assert required, "no importer source paths discovered (introspection broke)"
    assert required <= staged, f"importer tables not staged by sync: {sorted(required - staged)}"


def test_stage_downloads_operator_and_module_tables(tmp_path: Path) -> None:
    """B36/§V41: a snapshot carrying the operator+module excel tables must have them
    staged, so ``get_operator`` / ``compare_operator_modules`` are non-empty on a
    synced DB (they were never fetched before, only via the local ``import`` path)."""
    fetcher = dict_fetcher_from_snapshot(BASE_URL, OPERATOR_FIXTURE_ROOT)
    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=fetcher)
    local = adapter.stage(tmp_path / "staging")

    for table in SUPPLEMENTARY_FILES:
        assert local.exists(table), f"{table} was not staged"


def test_stage_tolerates_missing_operator_tables(tmp_path: Path) -> None:
    """B36: a combat-only snapshot legitimately lacks the operator/module tables; a
    404 on them is skipped, not fatal, so such a snapshot still syncs (the pipeline
    imports those domains empty)."""
    # The stage_4_4 fixture has no operator/module tables at all.
    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=_fetcher())
    local = adapter.stage(tmp_path / "staging")  # must not raise

    assert local.exists("gamedata/excel/stage_table.json")
    for table in SUPPLEMENTARY_FILES:
        assert not local.exists(table)


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
    files = _fixture_files()
    files[f"{BASE_URL}/gamedata/excel/enemy_handbook_table.json"] = json.dumps(deep).encode("utf-8")
    limits = DownloadLimits(max_json_depth=4)
    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=DictFetcher(files), limits=limits)
    with pytest.raises(SourceAdapterError, match="depth cap"):
        adapter.stage(tmp_path / "staging")


def test_json_node_cap(tmp_path: Path) -> None:
    payload = {"enemyData": {f"e{i}": {"name": i} for i in range(50)}}
    files = _fixture_files()
    files[f"{BASE_URL}/gamedata/excel/enemy_handbook_table.json"] = json.dumps(payload).encode(
        "utf-8"
    )
    limits = DownloadLimits(max_json_nodes=5)
    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=DictFetcher(files), limits=limits)
    with pytest.raises(SourceAdapterError, match="node cap"):
        adapter.stage(tmp_path / "staging")


def test_deeply_nested_json_capped_gracefully(tmp_path: Path) -> None:
    """Pathologically deep JSON is rejected as a capped error, not a RecursionError."""
    files = _fixture_files()
    payload = b"[" * 100_000 + b"]" * 100_000  # ~200 KB, under the per-file cap
    files[f"{BASE_URL}/gamedata/excel/enemy_handbook_table.json"] = payload
    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=DictFetcher(files))
    with pytest.raises(SourceAdapterError, match="nesting depth"):
        adapter.stage(tmp_path / "staging")


# --- run-level total-download budget (PRD §11.2; shared across servers) --------


def test_download_budget_accumulates_run_level() -> None:
    budget = DownloadBudget(100)
    budget.charge(60)  # under cap
    with pytest.raises(SourceAdapterError, match="total download cap"):
        budget.charge(50)  # 110 > 100 across the run


def test_download_budget_check_is_a_no_charge_prefetch_gate() -> None:
    """§V42: ``check`` fails fast once the cap is blown (so no new parallel fetch
    starts) but never charges — a run at or under the cap passes untouched."""
    budget = DownloadBudget(100)
    budget.check()  # nothing charged yet: no-op
    budget.charge(100)  # exactly at cap: not over
    budget.check()  # still fine at the boundary
    with pytest.raises(SourceAdapterError, match="total download cap"):
        budget.charge(1)  # 101 > 100 trips the cap (and leaves _used == 101)
    with pytest.raises(SourceAdapterError, match="total download cap"):
        budget.check()  # a later worker sees the blown cap and bails without fetching


def test_https_fetcher_close_is_safe_with_no_open_connections() -> None:
    """§T79 cleanup: ``close`` releases the (possibly empty) connection registry
    without error, so the CLI can always call it in a ``finally``."""
    fetcher = HttpsFetcher()
    fetcher.close()  # nothing opened yet
    fetcher.close()  # idempotent


# --- redirect same-domain policy (PRD §17.4) ----------------------------------


def test_redirect_refuses_cross_domain() -> None:
    with pytest.raises(SourceAdapterError, match="cross-domain"):
        _validate_redirect_target(
            "https://attacker.example/x.json",
            origin_host="example.test",
            allow_cross_domain=False,
        )


def test_redirect_refuses_non_https_target() -> None:
    with pytest.raises(SourceAdapterError, match="non-HTTPS"):
        _validate_redirect_target(
            "http://example.test/x.json",
            origin_host="example.test",
            allow_cross_domain=False,
        )


def test_redirect_same_domain_allowed() -> None:
    # A same-host HTTPS redirect passes the gate (no exception).
    _validate_redirect_target(
        "https://example.test/other.json",
        origin_host="example.test",
        allow_cross_domain=False,
    )


# --- §V42: bounded parallel sync preserves every per-file gate + exact caps ----


def _snapshot_relpaths(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_parallel_and_serial_stage_identical_output(tmp_path: Path) -> None:
    """§V42/§T79: ``max_parallel=1`` (serial fallback) and a parallel run stage the
    exact same file set — the staged output is deterministic, independent of worker
    completion order."""
    serial = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=_fetcher(), max_parallel=1)
    parallel = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=_fetcher(), max_parallel=8)

    serial_root = tmp_path / "serial"
    parallel_root = tmp_path / "parallel"
    serial.stage(serial_root)
    parallel.stage(parallel_root)

    assert _snapshot_relpaths(serial_root) == _snapshot_relpaths(parallel_root)
    assert "gamedata/excel/stage_table.json" in _snapshot_relpaths(parallel_root)
    assert "gamedata/levels/main/level_main_04-04.json" in _snapshot_relpaths(parallel_root)


def test_total_cap_trips_under_parallel_workers(tmp_path: Path) -> None:
    """§V42: the run-level total-download cap still trips under N workers (a lost
    update must not let the run overshoot the cap)."""
    limits = DownloadLimits(max_file_bytes=10_000, max_total_bytes=64)
    adapter = ArknightsAssetsAdapter(
        BASE_URL, "en", fetcher=_fetcher(), limits=limits, max_parallel=8
    )
    with pytest.raises(SourceAdapterError, match="total download cap"):
        adapter.stage(tmp_path / "staging")


def test_all_gates_apply_per_file_under_parallel(tmp_path: Path) -> None:
    """§V42: the per-file byte cap (a §V1 gate) is enforced per file regardless of
    the worker — a parallel run rejects an oversized file just like the serial one."""
    limits = DownloadLimits(max_file_bytes=8)
    adapter = ArknightsAssetsAdapter(
        BASE_URL, "en", fetcher=_fetcher(), limits=limits, max_parallel=8
    )
    with pytest.raises(SourceAdapterError, match="per-file cap"):
        adapter.stage(tmp_path / "staging")


def test_download_budget_charge_is_thread_safe() -> None:
    """§V42: ``DownloadBudget.charge`` accumulates under a lock ∴ the total is exact
    under concurrent charges — a naive ``+=`` loses updates under threads."""
    import threading

    # Cap high enough that no charge trips it; we only assert the running total.
    budget = DownloadBudget(10_000_000)
    charges = 200
    threads = [threading.Thread(target=budget.charge, args=(1,)) for _ in range(charges)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert budget._used == charges  # every charge counted, none lost


def test_max_parallel_below_one_rejected() -> None:
    with pytest.raises(SourceAdapterError, match="max_parallel"):
        ArknightsAssetsAdapter(BASE_URL, "en", fetcher=_fetcher(), max_parallel=0)


def test_pruned_level_skip_count_exact_under_parallel(tmp_path: Path) -> None:
    """§V42/B34: the 404-skip missing-count aggregation is preserved under parallelism
    — the sync still completes and stages only the present level file."""
    files = _fixture_files()
    stage_table_url = f"{BASE_URL}/gamedata/excel/stage_table.json"
    stage_table = json.loads(files[stage_table_url])
    for i in range(5):  # several pruned refs fanned out across workers
        stage_table["stages"][f"act10d5_{i:02d}"] = {
            "stageId": f"act10d5_{i:02d}",
            "levelId": f"Activities/ACT10d5/level_act10d5_{i:02d}",
        }
    files[stage_table_url] = json.dumps(stage_table).encode("utf-8")

    adapter = ArknightsAssetsAdapter(BASE_URL, "en", fetcher=DictFetcher(files), max_parallel=8)
    local = adapter.stage(tmp_path / "staging")  # must not raise

    assert local.exists("gamedata/levels/main/level_main_04-04.json")
    for i in range(5):
        assert not local.exists(f"gamedata/levels/activities/act10d5/level_act10d5_{i:02d}.json")
