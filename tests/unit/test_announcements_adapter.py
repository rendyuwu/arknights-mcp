"""T95: the official-announcement network adapter (§V56/§V1 CLI-only fetch, caps).

Exercises the safety machinery of :class:`AnnouncementsAdapter` with an in-memory
fetcher (no live network): HTTPS enforcement, the en/cn region gate (§V56), the
network flag (§V1), and the per-file / total-size / JSON-depth / JSON-node caps
shared via :mod:`arknights_mcp.sources.http_fetch` (§V37). The adapter is transport
only -- it never touches the network at query time (§V1); the importer (T95)
consumes what it returns.
"""

from __future__ import annotations

import json

import pytest
from tests.support import DictFetcher

from arknights_mcp.sources.announcements import (
    CN_SOURCE_ID,
    GLOBAL_SOURCE_ID,
    AnnouncementsAdapter,
    source_id_for_region,
)
from arknights_mcp.sources.base import SourceAdapterError
from arknights_mcp.sources.http_fetch import DownloadBudget, DownloadLimits

FEED_URL = "https://www.arknights.global/news/feed.json"


def _feed_payload() -> list[dict[str, object]]:
    return [{"announceId": "ann-1001", "title": "Maintenance", "category": "maintenance"}]


def _fetcher(extra: dict[str, bytes] | None = None) -> DictFetcher:
    files: dict[str, bytes] = {FEED_URL: json.dumps(_feed_payload()).encode("utf-8")}
    if extra:
        files.update(extra)
    return DictFetcher(files)


# --- CLI-only network posture (§V1) -------------------------------------------


def test_adapter_touches_network_is_true() -> None:
    # §V1: this is a network adapter -- flagged so it is only ever run from CLI.
    assert AnnouncementsAdapter(FEED_URL, "en", fetcher=_fetcher()).touches_network is True


# --- HTTPS enforcement (§V1) --------------------------------------------------


def test_feed_url_must_be_https() -> None:
    with pytest.raises(SourceAdapterError, match="https"):
        AnnouncementsAdapter("http://www.arknights.global/feed", "en", fetcher=_fetcher())


# --- region gate (§V56/§V5) ---------------------------------------------------


def test_region_must_be_en_or_cn() -> None:
    with pytest.raises(SourceAdapterError, match="region must be en"):
        AnnouncementsAdapter(FEED_URL, "jp", fetcher=_fetcher())


def test_region_selects_source_id() -> None:
    assert source_id_for_region("en") == GLOBAL_SOURCE_ID
    assert source_id_for_region("cn") == CN_SOURCE_ID
    assert source_id_for_region("jp") is None
    assert AnnouncementsAdapter(FEED_URL, "en", fetcher=_fetcher()).source_id == GLOBAL_SOURCE_ID
    assert AnnouncementsAdapter(FEED_URL, "cn", fetcher=_fetcher()).source_id == CN_SOURCE_ID


# --- happy path: feed parses --------------------------------------------------


def test_fetch_returns_parsed_json() -> None:
    adapter = AnnouncementsAdapter(FEED_URL, "en", fetcher=_fetcher())
    assert adapter.fetch() == _feed_payload()


# --- resource caps (PRD §11.2; shared with the other adapters, §V37) ----------


def test_per_file_size_cap() -> None:
    adapter = AnnouncementsAdapter(
        FEED_URL, "en", fetcher=_fetcher(), limits=DownloadLimits(max_file_bytes=8)
    )
    with pytest.raises(SourceAdapterError, match="per-file cap"):
        adapter.fetch()


def test_total_size_cap() -> None:
    budget = DownloadBudget(max_total_bytes=8)
    adapter = AnnouncementsAdapter(FEED_URL, "en", fetcher=_fetcher(), budget=budget)
    with pytest.raises(SourceAdapterError, match="total download cap"):
        adapter.fetch()


def test_json_depth_cap() -> None:
    deep: dict[str, object] = {}
    node: dict[str, object] = deep
    for _ in range(20):
        child: dict[str, object] = {}
        node["nest"] = child
        node = child
    adapter = AnnouncementsAdapter(
        FEED_URL,
        "en",
        fetcher=_fetcher({FEED_URL: json.dumps(deep).encode("utf-8")}),
        limits=DownloadLimits(max_json_depth=4),
    )
    with pytest.raises(SourceAdapterError, match="depth cap"):
        adapter.fetch()


def test_deeply_nested_json_capped_gracefully() -> None:
    """Pathologically deep JSON is a capped error, not a RecursionError."""
    payload = b"[" * 100_000 + b"]" * 100_000  # ~200 KB, under the per-file cap
    adapter = AnnouncementsAdapter(FEED_URL, "en", fetcher=_fetcher({FEED_URL: payload}))
    with pytest.raises(SourceAdapterError, match="nesting depth"):
        adapter.fetch()


def test_invalid_json_rejected() -> None:
    adapter = AnnouncementsAdapter(FEED_URL, "en", fetcher=_fetcher({FEED_URL: b"not json{"}))
    with pytest.raises(SourceAdapterError, match="invalid JSON"):
        adapter.fetch()
