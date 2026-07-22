"""T102: the penguin drop ride-along wired into ``sync`` (§V58, §V52, §V3, §V54).

Drives ``arknights-mcp sync`` with an in-memory fetcher (no live network, §V1/§V52)
that serves both the pinned 4-4 game-data snapshot and a penguin drop payload, and
asserts the §V58 ride-along contract end to end:

* enabled penguin (in ``enabled_sources`` + registry-enabled) -> drops land in the
  PROMOTED db and ``get_stage_drops`` returns facts + efficiency (§V58/I.tool);
* a penguin outage -> the game-data build STILL promotes, drops empty, no fail
  (fail-open, §V58/§V3);
* ``--server all`` with one region's penguin failing -> that region's savepoint
  rolls back while the other region's drops survive; both game-data builds promote
  (per-server all-or-nothing, §V58);
* disabled penguin -> never fetched (the fetcher records no penguin URL, §V58 opt-in);
* the region -> penguin-server map is the exact inverse of §V54 (en->US, cn->CN).
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import DictFetcher

from arknights_mcp.cli import main
from arknights_mcp.db.connection import read_only_connection
from arknights_mcp.db.promotion import resolve_active_database
from arknights_mcp.importers.penguin_drops import (
    penguin_server_for_region,
    region_for_penguin_server,
)
from arknights_mcp.services.drops import get_stage_drops
from arknights_mcp.sources.penguin_statistics import PENGUIN_BASE_URL
from arknights_mcp.sources.registry import set_source_enabled

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"
BASE_URL = "https://example.test/repo/{server}"

#: A US (Global -> en) penguin payload keyed to the 4-4 fixture stage ``main_04-04``.
#: The prose blurb rides a non-allowlisted item key so a correct pipeline drops it
#: (§V16/§V18). ``absent_stage`` is skipped fail-closed (§V30) -> drops_inserted=1.
_PENGUIN_US = {
    "items": [
        {
            "itemId": "sugar",
            "name": "Sugar",
            "rarity": 3,
            "itemType": "MATERIAL",
            "description": "PENGUINPROSE that must never ship to a client (§V16).",
        },
    ],
    "result/matrix": {
        "matrix": [
            {"stageId": "main_04-04", "itemId": "sugar", "quantity": 1250, "times": 5000},
            {"stageId": "absent_stage", "itemId": "sugar", "quantity": 1, "times": 10},
        ]
    },
}


class _RecordingFetcher(DictFetcher):
    """A :class:`DictFetcher` that records every URL requested (for the opt-in test)."""

    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__(files)
        self.urls: list[str] = []

    def fetch(self, url: str, *, max_bytes: int) -> bytes:
        self.urls.append(url)
        return super().fetch(url, max_bytes=max_bytes)


def _game_files(servers: tuple[str, ...]) -> dict[str, bytes]:
    """Map ``BASE_URL``/<rel> to the 4-4 fixture bytes for each region tree (§V5)."""
    files: dict[str, bytes] = {}
    for server in servers:
        base = BASE_URL.replace("{server}", server).rstrip("/")
        for path in FIXTURE_ROOT.rglob("*"):
            if path.is_file():
                rel = path.relative_to(FIXTURE_ROOT).as_posix()
                files[f"{base}/{rel}"] = path.read_bytes()
    return files


def _penguin_files(payloads: dict[str, dict[str, object]]) -> dict[str, bytes]:
    """Map the penguin v2 endpoint URLs to their payload bytes for each server."""
    files: dict[str, bytes] = {}
    for server, payload in payloads.items():
        files[f"{PENGUIN_BASE_URL}/items?server={server}"] = json.dumps(payload["items"]).encode()
        files[f"{PENGUIN_BASE_URL}/result/matrix?server={server}"] = json.dumps(
            payload["result/matrix"]
        ).encode()
    return files


def _enabled_registry(tmp_path: Path) -> Path:
    """Copy the machine registry to tmp and enable ``penguin_statistics`` in it."""
    registry = tmp_path / "data_sources.toml"
    registry.write_text(REGISTRY.read_text(encoding="utf-8"), encoding="utf-8")
    set_source_enabled(registry, "penguin_statistics", True)
    return registry


def _write_config(
    tmp_path: Path,
    *,
    registry: Path,
    enabled_sources: list[str],
    servers: list[str],
) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.toml"
    enabled = ", ".join(f'"{s}"' for s in enabled_sources)
    server_list = ", ".join(f'"{s}"' for s in servers)
    config.write_text(
        "[database]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'current_manifest = "{(data_dir / "current.json").as_posix()}"\n'
        "\n[sync]\n"
        f"enabled_sources = [{enabled}]\n"
        "allow_remote_download = true\n"
        "retain_versions = 3\n"
        f'\n[sync.arknights_assets_gamedata]\nbase_url = "{BASE_URL}"\nservers = [{server_list}]\n'
        "\n[source_registry]\n"
        f'machine_registry = "{registry.as_posix()}"\n',
        encoding="utf-8",
    )
    return config


def _active_db(data_dir: Path) -> Path:
    db = resolve_active_database(data_dir, data_dir / "current.json")
    assert db is not None
    return db


# --- the region -> penguin-server map is the exact inverse of §V54 (unit) ----------


def test_region_to_penguin_server_inverts_v54() -> None:
    assert penguin_server_for_region("en") == "US"
    assert penguin_server_for_region("cn") == "CN"
    assert penguin_server_for_region("jp") is None
    # Round-trips against the forward §V54 map for every en/cn region.
    for region in ("en", "cn"):
        server = penguin_server_for_region(region)
        assert server is not None
        assert region_for_penguin_server(server) == region


# --- enabled -> drops in the promoted db + get_stage_drops returns facts (§V58) ----


def test_sync_enabled_penguin_promotes_drops(tmp_path: Path) -> None:
    registry = _enabled_registry(tmp_path)
    config = _write_config(
        tmp_path,
        registry=registry,
        enabled_sources=["arknights_assets_gamedata", "penguin_statistics"],
        servers=["en"],
    )
    fetcher = DictFetcher({**_game_files(("en",)), **_penguin_files({"US": _PENGUIN_US})})
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=fetcher)
    assert rc == 0

    with read_only_connection(_active_db(tmp_path / "data")) as conn:
        result = get_stage_drops(conn, server="en", stage_code="4-4", include_efficiency=True)
    assert result.status == "ok"
    assert result.stale is False
    drops = {d.item_game_id: d for d in result.drops}
    assert "sugar" in drops
    assert drops["sugar"].drop_rate == 0.25
    assert drops["sugar"].region == "en"
    # include_efficiency ran the farming analyzer over the fresh drop (§V55/§V66.1).
    assert result.observation is not None
    # §V16/§V18: the item prose never rode into the served result.
    assert "PENGUINPROSE" not in json.dumps(result, default=lambda o: o.__dict__)


# --- outage -> game-data-only build still promotes, drops empty (§V58/§V3) ----------


def test_sync_penguin_outage_still_promotes_game_data(tmp_path: Path) -> None:
    registry = _enabled_registry(tmp_path)
    config = _write_config(
        tmp_path,
        registry=registry,
        enabled_sources=["arknights_assets_gamedata", "penguin_statistics"],
        servers=["en"],
    )
    # Game data is served; the penguin endpoints are NOT mapped -> the adapter's
    # fetch raises SourceNotFoundError, which the ride-along catches (§V58).
    fetcher = DictFetcher(_game_files(("en",)))
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=fetcher)
    assert rc == 0

    with read_only_connection(_active_db(tmp_path / "data")) as conn:
        # The game data promoted (the stage exists) but no drop cache was written.
        assert conn.execute("SELECT COUNT(*) FROM stages WHERE server='en'").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM stage_drops").fetchone()[0] == 0
        # get_stage_drops reports the stage has no drop cache (not_found), never fresh.
        result = get_stage_drops(conn, server="en", stage_code="4-4")
    assert result.status == "not_found"


# --- a non-adapter/non-importer error in the penguin path still fails open (§V58) ---


def test_sync_penguin_unexpected_error_still_promotes(tmp_path: Path) -> None:
    """A penguin payload that raises outside the (adapter, importer, sqlite) error set
    -- here ``times: 1e999`` parses to ``inf`` and ``as_int(inf)`` raises
    ``OverflowError`` -- must still be caught so the game-data build promotes (§V58/§V3).
    A narrow ``except`` tuple would let it escape ``post_build`` and sink the whole sync.
    """
    registry = _enabled_registry(tmp_path)
    config = _write_config(
        tmp_path,
        registry=registry,
        enabled_sources=["arknights_assets_gamedata", "penguin_statistics"],
        servers=["en"],
    )
    bad = {
        "items": [{"itemId": "sugar", "name": "Sugar", "rarity": 3, "itemType": "MATERIAL"}],
        "result/matrix": {
            "matrix": [{"stageId": "main_04-04", "itemId": "sugar", "quantity": 1, "times": 1e999}]
        },
    }
    fetcher = DictFetcher({**_game_files(("en",)), **_penguin_files({"US": bad})})
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=fetcher)
    assert rc == 0

    with read_only_connection(_active_db(tmp_path / "data")) as conn:
        # Game data promoted; the poisoned penguin region rolled back (no drops, no items).
        assert conn.execute("SELECT COUNT(*) FROM stages WHERE server='en'").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM stage_drops").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM items WHERE server='en'").fetchone()[0] == 0


# --- --server all: one region's penguin fails, the other's drops survive (§V58) ----


def test_sync_all_rolls_back_only_failing_region(tmp_path: Path) -> None:
    registry = _enabled_registry(tmp_path)
    config = _write_config(
        tmp_path,
        registry=registry,
        enabled_sources=["arknights_assets_gamedata", "penguin_statistics"],
        servers=["en", "cn"],
    )
    # US (en) penguin is served; CN (cn) penguin is NOT -> cn's savepoint rolls back
    # while en's drops survive, and both game-data regions still promote.
    fetcher = DictFetcher({**_game_files(("en", "cn")), **_penguin_files({"US": _PENGUIN_US})})
    rc = main(["--config", str(config), "sync", "--server", "all"], fetcher=fetcher)
    assert rc == 0

    with read_only_connection(_active_db(tmp_path / "data")) as conn:
        # Both regions' game data promoted.
        assert conn.execute("SELECT COUNT(*) FROM stages WHERE server='en'").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM stages WHERE server='cn'").fetchone()[0] >= 1
        # Only en drops survived; cn rolled back cleanly (no half-written drop set).
        assert conn.execute("SELECT COUNT(*) FROM stage_drops WHERE region='en'").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM stage_drops WHERE region='cn'").fetchone()[0] == 0
        en = get_stage_drops(conn, server="en", stage_code="4-4")
        cn = get_stage_drops(conn, server="cn", stage_code="4-4")
    assert en.status == "ok"
    assert cn.status == "not_found"


# --- disabled penguin is never fetched (§V58 opt-in) --------------------------------


def test_sync_disabled_penguin_not_fetched(tmp_path: Path) -> None:
    # penguin_statistics is NOT in enabled_sources, so the ride-along returns early
    # (the enabled_sources gate fires before the registry check) -> never fetched,
    # even though the registry now enables penguin.
    config = _write_config(
        tmp_path,
        registry=REGISTRY,
        enabled_sources=["arknights_assets_gamedata"],
        servers=["en"],
    )
    fetcher = _RecordingFetcher({**_game_files(("en",)), **_penguin_files({"US": _PENGUIN_US})})
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=fetcher)
    assert rc == 0

    # No penguin endpoint was ever requested.
    assert not any("penguin-stats.io" in url for url in fetcher.urls)
    with read_only_connection(_active_db(tmp_path / "data")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM stage_drops").fetchone()[0] == 0
