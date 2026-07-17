"""T22: the ``import`` CLI command (§V1, §V3, §V4, §V5, I.cmd).

``arknights-mcp import`` builds a candidate from a user-supplied local snapshot
(no network at all, §V1), validates it, and promotes atomically. A missing or
malformed snapshot fails closed, leaving the active database untouched (§V3/§V4).
"""

from __future__ import annotations

from pathlib import Path

from arknights_mcp.cli import main
from arknights_mcp.db.connection import read_only_connection
from arknights_mcp.db.promotion import resolve_active_database

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"


def _write_config(tmp_path: Path) -> tuple[Path, Path]:
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


def _stage_codes(data_dir: Path, server: str) -> set[str]:
    db = resolve_active_database(data_dir, data_dir / "current.json")
    assert db is not None
    with read_only_connection(db) as conn:
        return {
            row[0]
            for row in conn.execute("SELECT stage_code FROM stages WHERE server = ?", (server,))
        }


def test_import_builds_validates_and_promotes(tmp_path: Path) -> None:
    config, data_dir = _write_config(tmp_path)
    rc = main(
        ["--config", str(config), "import", "--server", "en", "--source-path", str(FIXTURE_ROOT)]
    )
    assert rc == 0
    assert (data_dir / "current.json").is_file()
    # region isolation: en imported, cn empty (§V5, en & cn never silently mixed)
    assert "4-4" in _stage_codes(data_dir, "en")
    assert _stage_codes(data_dir, "cn") == set()


def test_import_missing_path_fails_closed(tmp_path: Path) -> None:
    config, data_dir = _write_config(tmp_path)
    rc = main(
        [
            "--config",
            str(config),
            "import",
            "--server",
            "en",
            "--source-path",
            str(tmp_path / "nope"),
        ]
    )
    assert rc == 1
    assert not (data_dir / "current.json").exists()


def test_import_malformed_snapshot_fails_closed(tmp_path: Path) -> None:
    config, data_dir = _write_config(tmp_path)
    empty = tmp_path / "empty_snapshot"
    (empty / "gamedata" / "excel").mkdir(parents=True)
    rc = main(["--config", str(config), "import", "--server", "en", "--source-path", str(empty)])
    assert rc == 1
    assert not (data_dir / "current.json").exists()
