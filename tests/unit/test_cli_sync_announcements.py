"""T106: the announcement ride-along wired into ``sync`` (§V56, §V58, §V3, §V5).

Drives ``arknights-mcp sync`` with an in-memory fetcher (no live network, §V1/§V56)
that serves both the pinned 4-4 game-data snapshot and an announcement feed, and
asserts the §T106 ride-along contract end to end:

* enabled announcement source (in ``enabled_sources`` + registry-enabled + a configured
  ``feed_url``) -> metadata rows land in the PROMOTED db and ``get_announcements``
  returns facts (§V56/I.tool);
* a feed outage -> the game-data build STILL promotes, announcements empty, no fail
  (fail-open, §V58/§V3);
* enabled + in ``enabled_sources`` but NO ``feed_url`` -> skipped rather than fetching
  a guessed URL (§V56/§V1); the build still promotes;
* the source absent from ``enabled_sources`` -> never fetched (§V58 opt-in), even
  though the shipped registry enables it by default (§V56 flip).

The shipped registry ships both official-news sources ENABLED by default (§V56/§T106),
so unlike the penguin ride-along these tests point at the real registry directly.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import DictFetcher

from arknights_mcp.cli import main
from arknights_mcp.db.connection import read_only_connection
from arknights_mcp.db.promotion import resolve_active_database
from arknights_mcp.services.announcements import get_announcements

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"
BASE_URL = "https://example.test/repo/{server}"

_GLOBAL_SOURCE_ID = "arknights_global_official_news"
_CN_SOURCE_ID = "arknights_cn_official_news"
_GLOBAL_FEED_URL = "https://feed.test/global/news"
_CN_FEED_URL = "https://feed.test/cn/news"

#: Prose that rides every feed entry on a NON-allowlisted key. A correct metadata-only
#: pipeline (§V16/§V18) drops it before storage; ASCII-only so JSON escaping can never
#: mask a leak in the served result.
_FORBIDDEN_PROSE = "ANNOUNCEBODYPROSE"

#: Two en announcements with distinct ISO dates so ordering is deterministic; each
#: carries a forbidden body/prose field that must never survive (§V16).
_EN_FEED = [
    {
        "announceId": "ann-en-1",
        "title": "Older Event",
        "date": "2026-07-01T00:00:00+00:00",
        "url": "https://www.arknights.global/news/ann-en-1",
        "category": "event",
        "body": _FORBIDDEN_PROSE + " the full article body that must never be stored",
    },
    {
        "announceId": "ann-en-2",
        "title": "Newest Banner",
        "date": "2026-07-20T00:00:00+00:00",
        "url": "https://www.arknights.global/news/ann-en-2",
        "category": "banner",
        "content": _FORBIDDEN_PROSE + " more prose that must never be stored",
    },
]


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


def _feed_files(feeds: dict[str, list[dict[str, object]]]) -> dict[str, bytes]:
    """Map each feed URL to its JSON payload bytes."""
    return {url: json.dumps(entries).encode() for url, entries in feeds.items()}


def _write_config(
    tmp_path: Path,
    *,
    enabled_sources: list[str],
    servers: list[str],
    feed_urls: bool = True,
) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.toml"
    enabled = ", ".join(f'"{s}"' for s in enabled_sources)
    server_list = ", ".join(f'"{s}"' for s in servers)
    text = (
        "[database]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'current_manifest = "{(data_dir / "current.json").as_posix()}"\n'
        "\n[sync]\n"
        f"enabled_sources = [{enabled}]\n"
        "allow_remote_download = true\n"
        "retain_versions = 3\n"
        f'\n[sync.arknights_assets_gamedata]\nbase_url = "{BASE_URL}"\nservers = [{server_list}]\n'
    )
    if feed_urls:
        text += (
            f'\n[sync.{_GLOBAL_SOURCE_ID}]\nfeed_url = "{_GLOBAL_FEED_URL}"\n'
            f'\n[sync.{_CN_SOURCE_ID}]\nfeed_url = "{_CN_FEED_URL}"\n'
        )
    text += f'\n[source_registry]\nmachine_registry = "{REGISTRY.as_posix()}"\n'
    config.write_text(text, encoding="utf-8")
    return config


def _active_db(data_dir: Path) -> Path:
    db = resolve_active_database(data_dir, data_dir / "current.json")
    assert db is not None
    return db


# --- enabled -> announcement rows in the promoted db + get_announcements facts (§V56) ---


def test_sync_enabled_announcements_promotes_rows(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        enabled_sources=["arknights_assets_gamedata", _GLOBAL_SOURCE_ID],
        servers=["en"],
    )
    fetcher = DictFetcher({**_game_files(("en",)), **_feed_files({_GLOBAL_FEED_URL: _EN_FEED})})
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=fetcher)
    assert rc == 0

    with read_only_connection(_active_db(tmp_path / "data")) as conn:
        result = get_announcements(conn, server="en")
    assert result.status == "ok"
    # Newest first (§V26): ann-en-2 (2026-07-20) precedes ann-en-1 (2026-07-01).
    assert [a.announce_id for a in result.announcements] == ["ann-en-2", "ann-en-1"]
    assert all(a.region == "en" for a in result.announcements)
    # §V16/§V18: the article body never rode into the served result.
    served = json.dumps([vars(a) for a in result.announcements])
    assert _FORBIDDEN_PROSE not in served


# --- outage -> game-data-only build still promotes, announcements empty (§V58/§V3) ------


def test_sync_announcement_outage_still_promotes_game_data(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        enabled_sources=["arknights_assets_gamedata", _GLOBAL_SOURCE_ID],
        servers=["en"],
    )
    # Game data is served; the feed URL is NOT mapped -> the adapter's fetch raises
    # SourceNotFoundError, which the ride-along catches (§V58).
    fetcher = DictFetcher(_game_files(("en",)))
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=fetcher)
    assert rc == 0

    with read_only_connection(_active_db(tmp_path / "data")) as conn:
        # Game data promoted (the stage exists) but no announcements were written.
        assert conn.execute("SELECT COUNT(*) FROM stages WHERE server='en'").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 0
        result = get_announcements(conn, server="en")
    # A region with no announcements is a legitimate empty ``ok`` list (§V56), not a
    # not_found -- this is a list tool, not an entity lookup.
    assert result.status == "ok"
    assert len(result.announcements) == 0


# --- enabled + in enabled_sources but NO feed_url -> skipped, not guessed (§V56/§V1) ----


def test_sync_enabled_without_feed_url_skips(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        enabled_sources=["arknights_assets_gamedata", _GLOBAL_SOURCE_ID],
        servers=["en"],
        feed_urls=False,
    )
    fetcher = _RecordingFetcher(_game_files(("en",)))
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=fetcher)
    assert rc == 0

    # No feed endpoint was ever requested (no default URL is guessed, §V56/§V1).
    assert not any("feed.test" in url for url in fetcher.urls)
    with read_only_connection(_active_db(tmp_path / "data")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 0


# --- absent from enabled_sources -> never fetched (§V58 opt-in) -------------------------


def test_sync_source_not_in_enabled_sources_not_fetched(tmp_path: Path) -> None:
    # The announcement source is NOT in enabled_sources, so the ride-along returns early
    # (the enabled_sources gate fires before the registry check) -> never fetched, even
    # though the shipped registry enables it by default (§V56 flip).
    config = _write_config(
        tmp_path,
        enabled_sources=["arknights_assets_gamedata"],
        servers=["en"],
    )
    fetcher = _RecordingFetcher(
        {**_game_files(("en",)), **_feed_files({_GLOBAL_FEED_URL: _EN_FEED})}
    )
    rc = main(["--config", str(config), "sync", "--server", "en"], fetcher=fetcher)
    assert rc == 0

    assert not any("feed.test" in url for url in fetcher.urls)
    with read_only_connection(_active_db(tmp_path / "data")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 0
