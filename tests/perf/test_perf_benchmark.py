"""§T50: the M5 performance benchmark gate.

Budgets (the PRD's perf targets for v0.1):

* **lookup** p95 < 200 ms -- a point read / search tool call;
* **stage analysis** p95 < 500 ms -- a full ``analyze_stage`` run;
* **startup** < 2 s -- a cold server process reaching *ready to serve*.

Scope + honesty. The committed fixtures are intentionally tiny (§V16 -- no raw
dump ever ships), so the absolute latencies here sit far under budget. This is
therefore a **regression guard**, not a full-dataset SLA: it fails if a change
turns a point lookup or a stage analysis into a full scan / O(rows) blowup that
would breach the budget on real data, and it pins that the shipped server reaches
ready-to-serve within the startup budget on a genuinely cold process.

What it drives is the real thing, not a stand-in: the read-only domain services
(§V2, parameterized SQL over a strictly read-only connection) over a candidate
built through the production pipeline from local fixtures (§V1, offline), and --
for startup -- the packaged ``serve --transport stdio`` twin of the console
script over a real stdio pipe (the shared core §V14 both transports serve from).
"""

from __future__ import annotations

import math
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.services.enemies import get_enemy
from arknights_mcp.services.operators import get_operator
from arknights_mcp.services.search import search_entities, search_stages
from arknights_mcp.services.stages import analyze_stage, get_stage
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
REGISTRY_PATH = REPO_ROOT / "config" / "data_sources.toml"

#: The v0.1 perf budgets, in seconds.
LOOKUP_BUDGET_S = 0.200
ANALYSIS_BUDGET_S = 0.500
STARTUP_BUDGET_S = 2.0

#: Enough samples for a stable p95 while keeping the gate quick on tiny fixtures.
_WARMUP = 5
_ITERATIONS = 100

#: Cold-process startup is timed best-of-N (min) after one untimed warmup: the
#: minimum is the achievable ready-to-serve time, filtering scheduler noise from
#: a shared/loaded CI runner rather than letting a single stall flake the gate.
_STARTUP_RUNS = 3


def _adapter(root: Path, server: str) -> LocalSnapshotAdapter:
    return LocalSnapshotAdapter(root, server, "local_snapshot")


def _build(tmp: Path, imports: list[ServerImport]) -> sqlite3.Connection:
    """Build a candidate through the production pipeline and open it read-only (§V2)."""
    path = tmp / "cand.sqlite"
    build_candidate(path, imports, registry=load_source_registry(REGISTRY_PATH))
    return open_read_only(path)


def _p95(samples: list[float]) -> float:
    """Nearest-rank p95 of ``samples`` (seconds)."""
    ordered = sorted(samples)
    rank = math.ceil(0.95 * len(ordered))
    return ordered[min(rank, len(ordered)) - 1]


def _latencies(fn: Callable[[], object], *, iterations: int = _ITERATIONS) -> list[float]:
    """Time ``fn`` ``iterations`` times after ``_WARMUP`` untimed calls."""
    for _ in range(_WARMUP):
        fn()
    samples: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return samples


# --- built databases ----------------------------------------------------------


@pytest.fixture(scope="module")
def combat_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    """The multi-region stage/enemy build (en + cn) used for lookup + analysis."""
    conn = _build(
        tmp_path_factory.mktemp("perf_combat"),
        [
            ServerImport("en", _adapter(FIXTURES / "golden" / "en", "en"), "local_snapshot"),
            ServerImport("cn", _adapter(FIXTURES / "golden" / "cn", "cn"), "local_snapshot"),
        ],
    )
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def operator_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    """The multi-region operator build (en + cn) used for the operator lookup."""
    conn = _build(
        tmp_path_factory.mktemp("perf_ops"),
        [
            ServerImport("en", _adapter(FIXTURES / "operator" / "en", "en"), "local_snapshot"),
            ServerImport("cn", _adapter(FIXTURES / "operator" / "cn", "cn"), "local_snapshot"),
        ],
    )
    yield conn
    conn.close()


# --- lookup + analysis budgets ------------------------------------------------


def test_lookup_p95_under_budget(
    combat_db: sqlite3.Connection, operator_db: sqlite3.Connection
) -> None:
    """§T50: a mix of point reads / searches stays under the lookup p95 budget.

    Covers every §I lookup surface -- an enemy point read, a stage point read, a
    full operator read (heavy sections included), an entity search and a stage
    search -- so a missing index that scans on any one of them shows up here.
    """
    calls: list[Callable[[], object]] = [
        lambda: get_enemy(combat_db, server="en", game_id="enemy_drone_a"),
        lambda: get_stage(combat_db, server="en", stage_code="GS-1"),
        lambda: search_entities(combat_db, query="drone", server="en"),
        lambda: search_stages(combat_db, query="GS-1", server="en"),
        lambda: get_operator(
            operator_db,
            server="en",
            game_id="char_002_amiya",
            include_phases=True,
            include_skills=True,
            include_talents=True,
            include_modules=True,
        ),
    ]
    # Guard the benchmark itself: a silently-empty result would time a no-op path.
    for call in calls:
        assert call().status == "ok"  # type: ignore[attr-defined]

    samples: list[float] = []
    for call in calls:
        samples += _latencies(call)
    p95 = _p95(samples)
    assert p95 < LOOKUP_BUDGET_S, (
        f"lookup p95 {p95 * 1000:.1f}ms exceeds budget {LOOKUP_BUDGET_S * 1000:.0f}ms"
    )


def test_stage_analysis_p95_under_budget(combat_db: sqlite3.Connection) -> None:
    """§T50: a full stage analysis stays under the analysis p95 budget.

    GS-3 is the multi-route/tiles scenario, so it exercises the widest set of
    deterministic rules -- the heaviest analyze path in the fixture set.
    """

    def run() -> object:
        return analyze_stage(combat_db, server="en", stage_code="GS-3")

    assert run().status == "ok"  # type: ignore[attr-defined]
    p95 = _p95(_latencies(run))
    assert p95 < ANALYSIS_BUDGET_S, (
        f"stage analysis p95 {p95 * 1000:.1f}ms exceeds budget {ANALYSIS_BUDGET_S * 1000:.0f}ms"
    )


# --- startup budget -----------------------------------------------------------


def _write_config(tmp_path: Path) -> Path:
    """A minimal config pointing at an empty data dir (startup needs no build).

    The server starts ready-to-serve even with nothing promoted (§V23) -- ``list``
    of tools is served before any DB is touched -- so the startup budget is timed
    without an import step muddying the measurement.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.toml"
    config.write_text(
        "[database]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'current_manifest = "{(data_dir / "current.json").as_posix()}"\n'
        "\n[source_registry]\n"
        f'machine_registry = "{REGISTRY_PATH.as_posix()}"\n',
        encoding="utf-8",
    )
    return config


async def _drive_ready(config: Path, cwd: Path) -> float:
    """Spawn the stdio server and return seconds until it is ready to serve tools."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "arknights_mcp", "--config", str(config), "serve", "--transport", "stdio"],
        cwd=str(cwd),
        env=dict(os.environ),
    )
    start = time.perf_counter()
    with anyio.fail_after(30):
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                await session.list_tools()
                # Measured at ready-to-serve; teardown happens after the return value
                # is computed, so process shutdown is excluded from startup timing.
                return time.perf_counter() - start


def _time_startup(config: Path, cwd: Path) -> float:
    return anyio.run(_drive_ready, config, cwd)


def test_startup_under_budget(tmp_path: Path) -> None:
    """§T50: a cold server process reaches ready-to-serve under the startup budget."""
    config = _write_config(tmp_path)
    # Warm the bytecode / OS file cache so we time steady-state cold-process start,
    # not the one-off .pyc-compile penalty of the very first spawn.
    _time_startup(config, tmp_path)
    best = min(_time_startup(config, tmp_path) for _ in range(_STARTUP_RUNS))
    assert best < STARTUP_BUDGET_S, f"startup {best:.3f}s exceeds budget {STARTUP_BUDGET_S:.1f}s"
