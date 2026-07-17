"""T26: the ``source`` CLI command group (§V20, §V28, §I.cmd).

``arknights-mcp source list|enable|disable|purge`` are admin-only, CLI-only ops
(§V28 -- never MCP tools). ``enable``/``disable`` flip the registry kill switch and
journal the action but never rebuild or mutate the active database, so current
data keeps being served (§V20). ``purge --rebuild`` removes only the rows
attributable to one source and promotes the rebuilt candidate only after it
validates; the current build stays active until then and on failure (§V20).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from arknights_mcp.cli import main
from arknights_mcp.db.connection import read_only_connection
from arknights_mcp.db.policy_events import read_events
from arknights_mcp.db.promotion import resolve_active_database
from arknights_mcp.db.validate import CheckResult, ValidationReport
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

_LOCAL = "local_snapshot"
_PRIMARY = "arknights_assets_gamedata"


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Config + isolated (writable) registry copy so tests never touch the repo file."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    registry = tmp_path / "data_sources.toml"
    shutil.copyfile(REGISTRY, registry)
    config = tmp_path / "config.toml"
    config.write_text(
        "[database]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'current_manifest = "{(data_dir / "current.json").as_posix()}"\n'
        "\n[source_registry]\n"
        f'machine_registry = "{registry.as_posix()}"\n',
        encoding="utf-8",
    )
    return config, data_dir, registry


def _import_active_db(config: Path) -> None:
    rc = main(
        ["--config", str(config), "import", "--server", "en", "--source-path", str(FIXTURE_ROOT)]
    )
    assert rc == 0


def _active_conn_query(data_dir: Path, sql: str) -> list[tuple[object, ...]]:
    db = resolve_active_database(data_dir, data_dir / "current.json")
    assert db is not None
    with read_only_connection(db) as conn:
        return list(conn.execute(sql))


# --- list ---------------------------------------------------------------------


def test_source_list_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, _, _ = _setup(tmp_path)
    rc = main(["--config", str(config), "source", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert _PRIMARY in out
    assert "[enabled " in out  # primary is enabled by default


def test_source_list_json_is_public_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, _, _ = _setup(tmp_path)
    rc = main(["--config", str(config), "source", "list", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list) and payload
    for entry in payload:
        # §V27: public view omits internal-only fields.
        assert "policy_notes" not in entry
        assert "private_hosting_status" not in entry
        assert entry["source_id"]


# --- enable / disable ---------------------------------------------------------


def test_source_disable_keeps_data_and_journals(tmp_path: Path) -> None:
    config, data_dir, registry = _setup(tmp_path)
    _import_active_db(config)
    before = (data_dir / "current.json").read_bytes()

    rc = main(["--config", str(config), "source", "disable", _PRIMARY, "--reason", "takedown"])
    assert rc == 0

    # Registry kill switch flipped off (§V20).
    entry = load_source_registry(registry, validate=False).get(_PRIMARY)
    assert entry is not None and entry.enabled is False
    # Journaled as a disable event.
    events = read_events(data_dir)
    assert [(e.event_type, e.source_id) for e in events] == [("disable", _PRIMARY)]
    assert events[0].reason == "takedown"
    # §V20: no rebuild, active database untouched.
    assert (data_dir / "current.json").read_bytes() == before


def test_source_enable_after_disable(tmp_path: Path) -> None:
    config, data_dir, registry = _setup(tmp_path)
    assert main(["--config", str(config), "source", "disable", _LOCAL]) == 0
    assert main(["--config", str(config), "source", "enable", _LOCAL]) == 0

    entry = load_source_registry(registry, validate=False).get(_LOCAL)
    assert entry is not None and entry.enabled is True
    assert [(e.event_type, e.source_id) for e in read_events(data_dir)] == [
        ("disable", _LOCAL),
        ("enable", _LOCAL),
    ]


def test_source_enable_noop_when_already_enabled(tmp_path: Path) -> None:
    config, data_dir, _ = _setup(tmp_path)
    rc = main(["--config", str(config), "source", "enable", _PRIMARY])
    assert rc == 0
    # No state change -> no journal event.
    assert read_events(data_dir) == []


def test_source_toggle_unknown_source_fails(tmp_path: Path) -> None:
    config, _, _ = _setup(tmp_path)
    assert main(["--config", str(config), "source", "enable", "nope"]) == 1
    assert main(["--config", str(config), "source", "disable", "nope"]) == 1


# --- purge --rebuild ----------------------------------------------------------


def test_source_purge_rebuild_removes_source_rows(tmp_path: Path) -> None:
    config, data_dir, _ = _setup(tmp_path)
    _import_active_db(config)
    assert _active_conn_query(data_dir, "SELECT COUNT(*) FROM stages")[0][0] > 0

    rc = main(["--config", str(config), "source", "purge", _LOCAL, "--rebuild"])
    assert rc == 0

    # Rebuilt build has no rows attributable to the purged source (§V20).
    assert _active_conn_query(data_dir, "SELECT COUNT(*) FROM stages")[0][0] == 0
    assert _active_conn_query(data_dir, "SELECT COUNT(*) FROM source_snapshots")[0][0] == 0
    # The purge event is materialized into the immutable build.
    events = _active_conn_query(data_dir, "SELECT event_type, source_id FROM source_policy_events")
    assert ("purge", _LOCAL) in events


def test_source_purge_requires_rebuild_flag(tmp_path: Path) -> None:
    config, data_dir, _ = _setup(tmp_path)
    _import_active_db(config)
    before = (data_dir / "current.json").read_bytes()

    rc = main(["--config", str(config), "source", "purge", _LOCAL])
    assert rc == 1
    assert (data_dir / "current.json").read_bytes() == before


def test_source_purge_unknown_source_fails(tmp_path: Path) -> None:
    config, _, _ = _setup(tmp_path)
    _import_active_db(config)
    assert main(["--config", str(config), "source", "purge", "nope", "--rebuild"]) == 1


def test_source_purge_without_active_db_fails(tmp_path: Path) -> None:
    config, data_dir, _ = _setup(tmp_path)
    rc = main(["--config", str(config), "source", "purge", _LOCAL, "--rebuild"])
    assert rc == 1
    assert not (data_dir / "current.json").exists()


def test_source_purge_failed_validation_keeps_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, data_dir, _ = _setup(tmp_path)
    _import_active_db(config)
    before = (data_dir / "current.json").read_bytes()

    failing = ValidationReport(
        passed=False,
        schema_version="0001",
        checks=(CheckResult("forced", passed=False, detail="test"),),
    )
    monkeypatch.setattr("arknights_mcp.db.purge.validate_database", lambda *a, **k: failing)

    rc = main(["--config", str(config), "source", "purge", _LOCAL, "--rebuild"])
    assert rc == 1
    # §V20: current database stays active when the rebuild fails validation.
    assert (data_dir / "current.json").read_bytes() == before
