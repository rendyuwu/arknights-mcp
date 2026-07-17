"""T25: the ``status`` + ``doctor`` CLI commands (I.cmd; §V12 no-secret output).

``status`` reports the active snapshot + schema version (reusing the shared
``get_data_status`` service); ``doctor`` reports environment/config/database
health. Neither prints secrets or secret descriptor values (§V12).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arknights_mcp.cli import main

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"
ISSUER = "https://issuer.example.test/should-not-be-printed"


def _write_config(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.toml"
    config.write_text(
        "[database]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'current_manifest = "{(data_dir / "current.json").as_posix()}"\n'
        "\n[source_registry]\n"
        f'machine_registry = "{REGISTRY.as_posix()}"\n'
        "\n[auth]\n"
        'mode = "oidc"\n'
        f'issuer = "{ISSUER}"\n',
        encoding="utf-8",
    )
    return config


def _import(config: Path) -> None:
    assert (
        main(
            [
                "--config",
                str(config),
                "import",
                "--server",
                "en",
                "--source-path",
                str(FIXTURE_ROOT),
            ]
        )
        == 0
    )


# --- status -------------------------------------------------------------------


def test_status_no_active_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _write_config(tmp_path)
    assert main(["--config", str(config), "status"]) == 0
    out = capsys.readouterr().out
    assert "no active database" in out


def test_status_reports_snapshot_and_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    _import(config)
    capsys.readouterr()
    assert main(["--config", str(config), "status"]) == 0
    out = capsys.readouterr().out
    assert "schema=" in out
    assert "local_snapshot" in out
    assert "enemies" in out and "stages" in out


def test_status_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _write_config(tmp_path)
    _import(config)
    capsys.readouterr()
    assert main(["--config", str(config), "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["mode"] == "local"
    assert payload["snapshots"][0]["server"] == "en"


# --- doctor -------------------------------------------------------------------


def test_doctor_reports_health(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _write_config(tmp_path)
    _import(config)
    capsys.readouterr()
    assert main(["--config", str(config), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "python" in out
    assert "mcp SDK" in out
    assert "sqlite" in out
    assert "source registry" in out
    assert "PASS" in out  # active database validates


def test_doctor_no_database_warns_but_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    assert main(["--config", str(config), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "active database" in out


def test_doctor_never_prints_secret_descriptors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    assert main(["--config", str(config), "doctor"]) == 0
    out = capsys.readouterr().out
    # §V12: the OIDC issuer descriptor is never echoed into diagnostics.
    assert ISSUER not in out
