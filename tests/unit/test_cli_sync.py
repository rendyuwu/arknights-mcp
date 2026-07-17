"""T21: the ``sync`` CLI command end to end (§V1, §V3, §V4, §V28, I.cmd).

Drives ``arknights-mcp sync`` with an in-memory fetcher (no live network, §V1):
the pinned 4-4 snapshot is "downloaded", staged, imported, validated, and
promoted atomically. Re-running an unchanged sync is a no-op, and a disabled
source or missing endpoint fails closed without touching the active DB (§V3/§V4).
"""

from __future__ import annotations

from pathlib import Path

from arknights_mcp.cli import main
from arknights_mcp.db.connection import read_only_connection
from arknights_mcp.db.promotion import resolve_active_database
from arknights_mcp.sources.arknights_assets import DictFetcher, dict_fetcher_from_snapshot

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"
BASE_URL = "https://example.test/repo"


def _write_config(tmp_path: Path, *, base_url: str = BASE_URL, allow_remote: bool = True) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.toml"
    config.write_text(
        "[database]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'current_manifest = "{(data_dir / "current.json").as_posix()}"\n'
        "\n[sync]\n"
        f"allow_remote_download = {str(allow_remote).lower()}\n"
        "retain_versions = 3\n"
        f'\n[sync.arknights_assets_gamedata]\nbase_url = "{base_url}"\nservers = ["en"]\n'
        "\n[source_registry]\n"
        f'machine_registry = "{REGISTRY.as_posix()}"\n',
        encoding="utf-8",
    )
    return config


def _fetcher() -> DictFetcher:
    return dict_fetcher_from_snapshot(BASE_URL, FIXTURE_ROOT)


def _active_stage_codes(config: Path, data_dir: Path) -> set[str]:
    db = resolve_active_database(data_dir, data_dir / "current.json")
    assert db is not None
    with read_only_connection(db) as conn:
        return {row[0] for row in conn.execute("SELECT stage_code FROM stages WHERE server='en'")}


def test_sync_builds_validates_and_promotes(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=_fetcher())
    assert rc == 0

    data_dir = tmp_path / "data"
    assert (data_dir / "current.json").is_file()
    assert "4-4" in _active_stage_codes(config, data_dir)


def test_sync_unchanged_is_noop(tmp_path: Path, capsys) -> None:
    config = _write_config(tmp_path)
    assert main(["--config", str(config), "sync", "--server", "en"], fetcher=_fetcher()) == 0
    capsys.readouterr()
    assert main(["--config", str(config), "sync", "--server", "en"], fetcher=_fetcher()) == 0
    out = capsys.readouterr().out
    assert "no-op" in out


def test_sync_refuses_disabled_remote_download(tmp_path: Path) -> None:
    config = _write_config(tmp_path, allow_remote=False)
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=_fetcher())
    assert rc == 1
    assert not (tmp_path / "data" / "current.json").exists()


def test_sync_refuses_placeholder_base_url(tmp_path: Path) -> None:
    config = _write_config(tmp_path, base_url="<configured allowlisted repository endpoint>")
    # The adapter never sees a placeholder: the command refuses before staging.
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=_fetcher())
    assert rc == 1
    assert not (tmp_path / "data" / "current.json").exists()
