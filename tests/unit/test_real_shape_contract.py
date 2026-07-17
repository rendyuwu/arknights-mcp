"""T67: real-shape contract test (§V29, §V30).

Unlike the minimal synthetic fixture (``tests/fixtures/stage_4_4``, which already
matches the parser), this drives the full ``sync`` and ``import`` CLI paths over a
fixture authored from the *real* ``arknights_assets_gamedata`` shapes — id-keyed
``m_value`` enemy DB with ``motion``, Title-case ``levelId``, a grid ``map`` with
``passableMask`` tiles, and ``key``-based wave actions resolved via ``enemyDbRefs``
(§V29). It fails if the schema bridge (T66) regresses: real snapshots must yield
non-empty enemies + tiles + spawns + ``stage_enemies`` (§V30), never a silent
empty combat build. It also asserts the §V30 fail-closed guard fires when a
non-empty combat source produces no combat rows.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from tests.support import dict_fetcher_from_snapshot

from arknights_mcp.cli import main
from arknights_mcp.db.connection import read_only_connection
from arknights_mcp.db.promotion import resolve_active_database

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4_real"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"
BASE_URL = "https://example.test/repo"

#: Prose sentinel seeded into every non-allowlisted key of the real fixture.
REAL_PROSE = "REALPROSE"


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


def _sync_config(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.toml"
    config.write_text(
        "[database]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'current_manifest = "{(data_dir / "current.json").as_posix()}"\n'
        "\n[sync]\nallow_remote_download = true\nretain_versions = 3\n"
        f'\n[sync.arknights_assets_gamedata]\nbase_url = "{BASE_URL}"\nservers = ["en"]\n'
        "\n[source_registry]\n"
        f'machine_registry = "{REGISTRY.as_posix()}"\n',
        encoding="utf-8",
    )
    return config, data_dir


def _combat_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 - fixed names
        for table in ("enemies", "stage_tiles", "stage_spawns", "stage_enemies")
    }


def _assert_full_combat(data_dir: Path) -> None:
    with read_only_connection(resolve_active_database(data_dir, data_dir / "current.json")) as conn:  # type: ignore[arg-type]
        counts = _combat_counts(conn)
        assert counts["enemies"] >= 2, counts
        assert counts["stage_tiles"] > 0, counts
        assert counts["stage_spawns"] > 0, counts
        assert counts["stage_enemies"] > 0, counts
        # Motion is sourced from the enemy DB (§V29 (d)); the drone must read FLY.
        drone_motion = conn.execute(
            "SELECT motion_type FROM enemies WHERE game_id = 'enemy_1105_drone'"
        ).fetchone()[0]
        assert drone_motion == "FLY"
        assert "4-4" in {
            r[0] for r in conn.execute("SELECT stage_code FROM stages WHERE server = 'en'")
        }


# --- import + sync over the real shapes (§V29, §V30) --------------------------


def test_import_real_shape_yields_combat(tmp_path: Path) -> None:
    config, data_dir = _import_config(tmp_path)
    rc = main(
        ["--config", str(config), "import", "--server", "en", "--source-path", str(REAL_FIXTURE)]
    )
    assert rc == 0
    _assert_full_combat(data_dir)


def test_sync_real_shape_yields_combat(tmp_path: Path) -> None:
    config, data_dir = _sync_config(tmp_path)
    fetcher = dict_fetcher_from_snapshot(BASE_URL, REAL_FIXTURE)
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=fetcher)
    assert rc == 0
    _assert_full_combat(data_dir)


def test_real_shape_drops_all_prose(tmp_path: Path) -> None:
    """§V18: the real fixture carries prose in non-allowlisted keys (handbook,
    enemy DB, stage table, wave action); none of it survives into the DB."""
    config, data_dir = _import_config(tmp_path)
    assert (
        main(
            [
                "--config",
                str(config),
                "import",
                "--server",
                "en",
                "--source-path",
                str(REAL_FIXTURE),
            ]
        )
        == 0
    )
    with read_only_connection(resolve_active_database(data_dir, data_dir / "current.json")) as conn:  # type: ignore[arg-type]
        tables = [
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        dump = "\n".join(str(r) for t in tables for r in conn.execute(f"SELECT * FROM {t}"))  # noqa: S608
    assert REAL_PROSE not in dump


# --- §V30 fail-closed: non-empty combat source yielding 0 combat rows ---------


def test_unresolved_level_fails_closed(tmp_path: Path) -> None:
    """§V30: a stage that references a level file which does not resolve must fail
    the build closed (silent empty combat build refused), leaving no active DB."""
    config, data_dir = _import_config(tmp_path)
    broken = tmp_path / "broken"
    shutil.copytree(REAL_FIXTURE, broken)
    # Remove the level file the stage's levelId resolves to: the reference remains
    # but nothing imports -> the guard must fire.
    (broken / "gamedata" / "levels" / "obt" / "main" / "level_main_04-04.json").unlink()

    rc = main(["--config", str(config), "import", "--server", "en", "--source-path", str(broken)])
    assert rc == 1
    assert not (data_dir / "current.json").exists()  # active DB untouched (§V3)
