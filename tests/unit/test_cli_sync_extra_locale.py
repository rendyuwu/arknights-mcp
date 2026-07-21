"""T109: the extra-locale alias ride-along wired into ``sync`` (§V57, §V58, §V3, §V37).

Drives ``arknights-mcp sync`` with an in-memory fetcher (no live network, §V1/§V57)
that serves both the pinned 4-4 game-data snapshot and the jp/kr extra-locale NAME
trees, and asserts the §T109 ride-along contract end to end:

* enabled extra-locale source (in ``enabled_sources`` + registry-enabled + a configured
  ``base_url``) -> jp/kr NAME aliases land in the PROMOTED db, the FTS index is rebuilt,
  and ``search_entities(locale=ja/ko)`` resolves the entity to its OWN en region facts
  (§V57/I.tool);
* a fetch outage -> the game-data build STILL promotes, aliases empty, no fail
  (fail-open, §V58/§V3);
* the source absent from ``enabled_sources`` -> never fetched (§V58 opt-in), even though
  the shipped registry enables it by default;
* a re-run of ``sync`` -> no duplicate alias rows (0012 UNIQUE + INSERT OR IGNORE, §T109).

The shipped registry ships ``arknights_extra_locale_names`` ENABLED by default, so like
the announcement ride-along these tests point at the real registry directly. The
extra-locale fetcher serves ONLY enemy NAMEs (empty ``character_table``) matched to the
fixture enemies: the 4-4 fixture imports no operators, so an operator NAME would trip the
§V30 per-domain guard -- an enemy-only source keeps the operator domain a legitimate empty
(the same shape the accept test uses).
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import DictFetcher

from arknights_mcp.cli import main
from arknights_mcp.db.connection import read_only_connection
from arknights_mcp.db.promotion import resolve_active_database
from arknights_mcp.services.search import search_entities

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"
BASE_URL = "https://example.test/repo/{server}"
EXTRA_BASE = "https://extra.test/{server}"

_SOURCE_ID = "arknights_extra_locale_names"

#: jp katakana NAME on the drone, kr hangul NAME on the slug -- both tokenize under FTS5
#: ``unicode61`` (matches the accept test).
DRONE_JP = "ドローン"
SLUG_KR = "광석충"

#: Machine-translated prose on a NON-allowlisted key. A correct NAME-only pipeline
#: (§V16/§V18) drops it before storage; ASCII-only so JSON escaping can never mask a leak.
_FORBIDDEN_PROSE = "LOCALEBODYPROSE"


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


def _extra_locale_files() -> dict[str, bytes]:
    """Map the jp/kr NAME-tree endpoints to their payloads (enemy NAMEs only)."""
    files: dict[str, bytes] = {}
    jp = EXTRA_BASE.replace("{server}", "jp").rstrip("/")
    kr = EXTRA_BASE.replace("{server}", "kr").rstrip("/")
    files[f"{jp}/gamedata/excel/character_table.json"] = json.dumps({}).encode()
    files[f"{jp}/gamedata/excel/enemy_handbook_table.json"] = json.dumps(
        {"enemyData": {"enemy_1105_drone": {"name": DRONE_JP, "description": _FORBIDDEN_PROSE}}}
    ).encode()
    files[f"{kr}/gamedata/excel/character_table.json"] = json.dumps({}).encode()
    files[f"{kr}/gamedata/excel/enemy_handbook_table.json"] = json.dumps(
        {"enemyData": {"enemy_1007_slime": {"name": SLUG_KR, "description": _FORBIDDEN_PROSE}}}
    ).encode()
    return files


def _write_config(
    tmp_path: Path,
    *,
    enabled_sources: list[str],
    base_url: bool = True,
) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.toml"
    enabled = ", ".join(f'"{s}"' for s in enabled_sources)
    text = (
        "[database]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'current_manifest = "{(data_dir / "current.json").as_posix()}"\n'
        "\n[sync]\n"
        f"enabled_sources = [{enabled}]\n"
        "allow_remote_download = true\n"
        "retain_versions = 3\n"
        f'\n[sync.arknights_assets_gamedata]\nbase_url = "{BASE_URL}"\nservers = ["en"]\n'
    )
    if base_url:
        text += f'\n[sync.{_SOURCE_ID}]\nbase_url = "{EXTRA_BASE}"\n'
    text += f'\n[source_registry]\nmachine_registry = "{REGISTRY.as_posix()}"\n'
    config.write_text(text, encoding="utf-8")
    return config


def _active_db(data_dir: Path) -> Path:
    db = resolve_active_database(data_dir, data_dir / "current.json")
    assert db is not None
    return db


# --- enabled -> jp/kr aliases in the promoted db + search(locale=) resolves (§V57) -----


def test_sync_enabled_extra_locale_promotes_and_resolves(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        enabled_sources=["arknights_assets_gamedata", _SOURCE_ID],
    )
    fetcher = DictFetcher({**_game_files(("en",)), **_extra_locale_files()})
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=fetcher)
    assert rc == 0

    with read_only_connection(_active_db(tmp_path / "data")) as conn:
        # §V57: the jp NAME resolves the en enemy, filtered to the `ja` locale; the hit
        # carries the entity's OWN region (en), never a jp "region".
        ja = search_entities(conn, query=DRONE_JP, locale="ja")
        assert ja.status == "ok"
        drone = next(h for h in ja.hits if h.game_id == "enemy_1105_drone")
        assert drone.server == "en"
        # The kr NAME resolves the other enemy under the `ko` locale.
        ko = search_entities(conn, query=SLUG_KR, locale="ko")
        assert ko.status == "ok"
        assert any(h.game_id == "enemy_1007_slime" for h in ko.hits)

        # The stored aliases carry locale ja/ko while the fact region stays en (§V57).
        rows = conn.execute(
            "SELECT e.server, a.locale, a.alias FROM enemy_aliases a "
            "JOIN enemies e ON e.enemy_pk = a.enemy_pk "
            "WHERE a.alias_type = 'locale_name' ORDER BY a.locale"
        ).fetchall()
        assert rows == [("en", "ja", DRONE_JP), ("en", "ko", SLUG_KR)]
        # §V16/§V18: the machine-translated prose never rode into storage.
        alias_blob = conn.execute("SELECT GROUP_CONCAT(alias) FROM enemy_aliases").fetchone()[0]
        assert _FORBIDDEN_PROSE not in (alias_blob or "")


# --- outage -> game-data-only build still promotes, aliases empty (§V58/§V3) -----------


def test_sync_extra_locale_outage_still_promotes_game_data(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        enabled_sources=["arknights_assets_gamedata", _SOURCE_ID],
    )
    # Game data is served; the extra-locale trees are NOT mapped -> the adapter's fetch
    # raises SourceNotFoundError, which the ride-along catches per locale (§V58).
    fetcher = DictFetcher(_game_files(("en",)))
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=fetcher)
    assert rc == 0

    with read_only_connection(_active_db(tmp_path / "data")) as conn:
        # Game data promoted (the stage exists) but no extra-locale aliases were written.
        assert conn.execute("SELECT COUNT(*) FROM stages WHERE server='en'").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM enemy_aliases").fetchone()[0] == 0


# --- absent from enabled_sources -> never fetched (§V58 opt-in) ------------------------


def test_sync_source_not_in_enabled_sources_not_fetched(tmp_path: Path) -> None:
    # The extra-locale source is NOT in enabled_sources, so the ride-along returns early
    # (the enabled_sources gate fires before the registry check) -> never fetched, even
    # though the shipped registry enables it by default.
    config = _write_config(
        tmp_path,
        enabled_sources=["arknights_assets_gamedata"],
    )
    fetcher = _RecordingFetcher({**_game_files(("en",)), **_extra_locale_files()})
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=fetcher)
    assert rc == 0

    assert not any("extra.test" in url for url in fetcher.urls)
    with read_only_connection(_active_db(tmp_path / "data")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM enemy_aliases").fetchone()[0] == 0


# --- enabled + in enabled_sources but NO base_url -> skipped, not guessed (§V57/§V1) ---


def test_sync_enabled_without_base_url_skips(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        enabled_sources=["arknights_assets_gamedata", _SOURCE_ID],
        base_url=False,
    )
    fetcher = _RecordingFetcher({**_game_files(("en",)), **_extra_locale_files()})
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=fetcher)
    assert rc == 0

    # No extra-locale endpoint was ever requested (no default URL is guessed, §V57/§V1).
    assert not any("extra.test" in url for url in fetcher.urls)
    with read_only_connection(_active_db(tmp_path / "data")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM enemy_aliases").fetchone()[0] == 0


# --- re-run sync -> no duplicate alias rows (0012 UNIQUE + INSERT OR IGNORE, §T109) ----


def test_sync_rerun_no_duplicate_alias_rows(tmp_path: Path) -> None:
    # Each `sync` builds a FRESH candidate, so cross-run duplication cannot arise from the
    # promote path alone; the OR IGNORE + UNIQUE(entity_pk, alias, locale) (0012) protect
    # the within-db re-import/backfill path (covered at the importer level). This test
    # asserts the sync-level invariant: after two syncs the promoted db holds exactly one
    # row per (entity, alias, locale) and search still resolves cleanly.
    config = _write_config(
        tmp_path,
        enabled_sources=["arknights_assets_gamedata", _SOURCE_ID],
    )
    files = {**_game_files(("en",)), **_extra_locale_files()}
    args = ["--config", str(config), "sync", "--server", "en"]
    assert main(args, fetcher=DictFetcher(files)) == 0
    assert main(args, fetcher=DictFetcher(files)) == 0

    with read_only_connection(_active_db(tmp_path / "data")) as conn:
        dupes = conn.execute(
            "SELECT enemy_pk, alias, locale, COUNT(*) c FROM enemy_aliases "
            "GROUP BY enemy_pk, alias, locale HAVING c > 1"
        ).fetchall()
        assert dupes == []
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM enemy_aliases WHERE alias_type='locale_name'"
            ).fetchone()[0]
            == 2
        )
        assert search_entities(conn, query=DRONE_JP, locale="ja").status == "ok"
