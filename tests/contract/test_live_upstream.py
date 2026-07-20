"""T68: CI-only real-shape validation vs LIVE upstream (§V16, §V29, §V30, §C).

T67 is fixture-only: it proves the raw→normalized schema bridge (T66) is
*internally consistent*, not that the mappings it infers actually match real
upstream. This test closes that gap. Against a **pinned**
``arknights_assets_gamedata`` commit it fetches the real EN snapshot over HTTPS,
drives the real ``import`` CLI path, and asserts that stage 4-4's combat data is
fully populated: every enemy spawned in 4-4 carries non-null
``hp``/``res``/``attackInterval``/``weight``/``lifePointReduction`` + ``motion``,
and the stage yields non-empty tiles/spawns/``stage_enemies`` (§V29, §V30). A
green run is what promotes the inferred mappings (``massLevel``→``weight``,
``lifePointReduce``→``lifePointReduction``, ``preDelay``→``spawnTime``,
``maxTimeWaitingForNextWave``→``maxTimeWaiting``, positional route/wave index) from
"inferred" to "verified vs live upstream" in §V29.

Nothing fetched is ever persisted in the repo (§V16, code-only distribution): the
snapshot and the built database live only under pytest's ``tmp_path`` (outside the
repo tree) and are discarded when the test ends — fetch → use → discard.

CI-only: this needs network and is gated behind ``ARKMCP_LIVE_UPSTREAM`` (set by
the dedicated CI job), so the default offline ``pytest -q`` skips the whole module.

The CN cross-validator (``kengxxiao_gamedata``) is deferred to §T69: its CN
``enemy_database`` uses a different ``{"enemies": [{Key, Value}]}`` schema that
needs its own normalization bridge before it can be driven through the pipeline.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.cli import main
from arknights_mcp.db.connection import read_only_connection
from arknights_mcp.db.promotion import resolve_active_database
from arknights_mcp.sources.http_fetch import HttpsFetcher

_LIVE_ENV = os.environ.get("ARKMCP_LIVE_UPSTREAM", "")
pytestmark = pytest.mark.skipif(
    _LIVE_ENV in ("", "0", "false", "False"),
    reason="live-upstream test (needs network); set ARKMCP_LIVE_UPSTREAM=1 (CI only)",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: Pinned upstream commit (B6 verified the real schema against this tree). Pinning
#: keeps the assertions deterministic; bump only alongside a re-review of §V29.
ARKNIGHTS_ASSETS_COMMIT = "413a81a3ff3e968089b1d6d302473f7b38c36dda"
BASE_URL = (
    "https://raw.githubusercontent.com/ArknightsAssets/ArknightsGamedata/"
    f"{ARKNIGHTS_ASSETS_COMMIT}/en"
)

#: The minimum real files needed to import + validate stage 4-4. The full ``sync``
#: path would additionally discover every stage's level file (thousands of
#: requests); the four core tables + the 4-4 level file exercise the real shapes
#: without that cost. Absent level files are skipped by the importer (a warning),
#: so the full stage table imports fine with only 4-4's level present.
LIVE_FILES: tuple[str, ...] = (
    "gamedata/excel/enemy_handbook_table.json",
    "gamedata/levels/enemydata/enemy_database.json",
    "gamedata/excel/zone_table.json",
    "gamedata/excel/stage_table.json",
    "gamedata/levels/obt/main/level_main_04-04.json",
)

#: stage_table.json is ~23 MiB at the pinned commit; the per-file cap must clear it.
MAX_FILE_BYTES = 64 * 1024 * 1024


def _stage_live_snapshot(dest: Path) -> None:
    """Fetch the pinned real files into ``dest`` via the production HTTPS fetcher.

    Uses the same :class:`HttpsFetcher` the CLI ``sync`` path uses (HTTPS-only,
    redirect-capped), so this exercises the real network adapter. ``dest`` is a
    tmp directory; nothing lands in the repo (§V16).
    """
    fetcher = HttpsFetcher()
    for relative_path in LIVE_FILES:
        data = fetcher.fetch(f"{BASE_URL}/{relative_path}", max_bytes=MAX_FILE_BYTES)
        target = dest / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _import_config(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.toml"
    config.write_text(
        "[database]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'current_manifest = "{(data_dir / "current.json").as_posix()}"\n'
        "\n[source_registry]\n"
        f'machine_registry = "{REGISTRY.as_posix()}"\n',
        encoding="utf-8",
    )
    return config, data_dir


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


# The 4-4 stage code maps to both `main_04-04` and its `#f#` challenge variant;
# either resolving to a non-empty combat picture satisfies the assertions below.
_TILES_4_4 = (
    "SELECT COUNT(*) FROM stage_tiles t "
    "JOIN stages s ON s.stage_pk = t.stage_pk "
    "WHERE s.server = 'en' AND s.stage_code = '4-4'"
)
_SPAWNS_4_4 = (
    "SELECT COUNT(*) FROM stage_spawns sp "
    "JOIN stage_waves w ON w.wave_pk = sp.wave_pk "
    "JOIN stages s ON s.stage_pk = w.stage_pk "
    "WHERE s.server = 'en' AND s.stage_code = '4-4'"
)
_STAGE_ENEMIES_4_4 = (
    "SELECT COUNT(*) FROM stage_enemies se "
    "JOIN stages s ON s.stage_pk = se.stage_pk "
    "WHERE s.server = 'en' AND s.stage_code = '4-4'"
)
# Every enemy_levels row of every enemy spawned in 4-4: the mapped stat columns
# must all be non-null (0 is a real value, not a gap). SUM(x IS NULL) counts gaps.
_STAT_NULLS_4_4 = (
    "SELECT COUNT(*), "
    "SUM(el.hp IS NULL), SUM(el.res IS NULL), SUM(el.attack_interval IS NULL), "
    "SUM(el.weight IS NULL), SUM(el.life_point_reduction IS NULL) "
    "FROM stage_enemies se "
    "JOIN stages s ON s.stage_pk = se.stage_pk "
    "JOIN enemies e ON e.enemy_pk = se.enemy_pk "
    "JOIN enemy_levels el ON el.enemy_pk = e.enemy_pk "
    "WHERE s.server = 'en' AND s.stage_code = '4-4'"
)
_MOTION_NULLS_4_4 = (
    "SELECT SUM(e.motion_type IS NULL) FROM stage_enemies se "
    "JOIN stages s ON s.stage_pk = se.stage_pk "
    "JOIN enemies e ON e.enemy_pk = se.enemy_pk "
    "WHERE s.server = 'en' AND s.stage_code = '4-4'"
)


def _assert_4_4_combat(conn: sqlite3.Connection) -> None:
    # §V30: real 4-4 must produce a non-empty combat picture, never silent-empty.
    assert _count(conn, _TILES_4_4) > 0, "4-4 produced no tiles"
    assert _count(conn, _SPAWNS_4_4) > 0, "4-4 produced no spawns"
    assert _count(conn, _STAGE_ENEMIES_4_4) > 0, "4-4 produced no stage_enemies"

    # §V29: the inferred stat mappings must pull real values for every 4-4 enemy.
    total, null_hp, null_res, null_ai, null_weight, null_lpr = conn.execute(
        _STAT_NULLS_4_4
    ).fetchone()
    assert total > 0, "no enemy_levels for 4-4 spawned enemies"
    assert (null_hp, null_res, null_ai, null_weight, null_lpr) == (0, 0, 0, 0, 0), (
        "null stat mapping over 4-4 enemies "
        f"(hp={null_hp} res={null_res} attackInterval={null_ai} "
        f"weight={null_weight} lifePointReduction={null_lpr} of {total} rows)"
    )

    # §V29 (d): motion is sourced from the enemy database, not the handbook.
    assert _count(conn, _MOTION_NULLS_4_4) == 0, "an enemy spawned in 4-4 has null motion"


def test_live_upstream_en_4_4_combat(tmp_path: Path) -> None:
    """Real pinned EN snapshot → 4-4 fully-populated combat data (§V29, §V30)."""
    snapshot = tmp_path / "snapshot"
    _stage_live_snapshot(snapshot)

    config, data_dir = _import_config(tmp_path)
    rc = main(["--config", str(config), "import", "--server", "en", "--source-path", str(snapshot)])
    assert rc == 0

    active = resolve_active_database(data_dir, data_dir / "current.json")
    assert active is not None
    # §V16: the fetched raw snapshot and the built DB both live outside the repo
    # tree (pytest tmp), so live game data is never committed — fetch → discard.
    assert REPO_ROOT not in snapshot.resolve().parents
    assert REPO_ROOT not in active.resolve().parents

    with read_only_connection(active) as conn:
        _assert_4_4_combat(conn)
