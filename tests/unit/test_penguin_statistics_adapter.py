"""T87: the Penguin Statistics network adapter (§V52/§V1 CLI-only fetch, caps).

Exercises the safety machinery of :class:`PenguinStatsAdapter` with an in-memory
fetcher (no live network): HTTPS enforcement, the endpoint + server allowlists, and
the per-file / total-size / JSON-depth / JSON-node caps shared via
:mod:`arknights_mcp.sources.http_fetch` (§V37). The adapter is transport only — it
never touches the network at query time (§V52); the drop importer (T89) consumes
what it returns.
"""

from __future__ import annotations

import json

import pytest
from tests.support import DictFetcher

from arknights_mcp.sources.base import SourceAdapterError
from arknights_mcp.sources.http_fetch import DownloadBudget, DownloadLimits
from arknights_mcp.sources.penguin_statistics import (
    PENGUIN_BASE_URL,
    PenguinStatsAdapter,
)

MATRIX_URL = f"{PENGUIN_BASE_URL}/result/matrix?server=US"
STAGES_URL = f"{PENGUIN_BASE_URL}/stages"


def _matrix_payload() -> dict[str, object]:
    return {"matrix": [{"stageId": "main_04-04", "itemId": "30012", "quantity": 42, "times": 100}]}


def _fetcher(extra: dict[str, bytes] | None = None) -> DictFetcher:
    files: dict[str, bytes] = {
        MATRIX_URL: json.dumps(_matrix_payload()).encode("utf-8"),
        STAGES_URL: json.dumps([{"stageId": "main_04-04"}]).encode("utf-8"),
    }
    if extra:
        files.update(extra)
    return DictFetcher(files)


# --- CLI-only network posture (§V52/§V1) --------------------------------------


def test_adapter_touches_network_is_true() -> None:
    # §V1/§V52: this is a network adapter — flagged so it is only ever run from CLI.
    assert PenguinStatsAdapter(fetcher=_fetcher()).touches_network is True


# --- HTTPS enforcement (§V1) --------------------------------------------------


def test_base_url_must_be_https() -> None:
    with pytest.raises(SourceAdapterError, match="https"):
        PenguinStatsAdapter("http://penguin-stats.io/api", fetcher=_fetcher())


# --- endpoint allowlist (§V18) ------------------------------------------------


def test_non_allowlisted_endpoint_rejected() -> None:
    adapter = PenguinStatsAdapter(fetcher=_fetcher())
    with pytest.raises(SourceAdapterError, match="endpoint not in penguin allowlist"):
        adapter.fetch("private/users")


def test_endpoint_cannot_smuggle_traversal() -> None:
    # An exact-match allowlist means a traversal-looking endpoint is simply not a
    # member — it is refused before any URL is built (no SSRF into another path).
    adapter = PenguinStatsAdapter(fetcher=_fetcher())
    with pytest.raises(SourceAdapterError, match="allowlist"):
        adapter.fetch("result/../../admin")


# --- server allowlist ---------------------------------------------------------


def test_unknown_server_rejected() -> None:
    adapter = PenguinStatsAdapter(fetcher=_fetcher())
    with pytest.raises(SourceAdapterError, match="unknown penguin server"):
        adapter.fetch("result/matrix", server="XX")


# --- happy path: allowlisted endpoint parses ----------------------------------


def test_fetch_matrix_returns_parsed_json() -> None:
    adapter = PenguinStatsAdapter(fetcher=_fetcher())
    result = adapter.fetch("result/matrix", server="US")
    assert result == _matrix_payload()


def test_fetch_server_less_endpoint() -> None:
    adapter = PenguinStatsAdapter(fetcher=_fetcher())
    assert adapter.fetch("stages") == [{"stageId": "main_04-04"}]


# --- resource caps (PRD §11.2; shared with the primary adapter, §V37) ---------


def test_per_file_size_cap() -> None:
    limits = DownloadLimits(max_file_bytes=8)
    adapter = PenguinStatsAdapter(fetcher=_fetcher(), limits=limits)
    with pytest.raises(SourceAdapterError, match="per-file cap"):
        adapter.fetch("result/matrix", server="US")


def test_total_size_cap() -> None:
    # A shared run budget bounds the whole run: a first fetch under cap, a second
    # over it. Cap is small enough to trip on the running total.
    limits = DownloadLimits(max_file_bytes=10_000, max_total_bytes=8)
    adapter = PenguinStatsAdapter(fetcher=_fetcher(), limits=limits)
    with pytest.raises(SourceAdapterError, match="total download cap"):
        adapter.fetch("result/matrix", server="US")


def test_shared_budget_bounds_multiple_fetches() -> None:
    # §V42/§V37: an injected DownloadBudget spans several fetches so the total cap
    # is a run-level bound, not per-request.
    budget = DownloadBudget(max_total_bytes=40)
    adapter = PenguinStatsAdapter(fetcher=_fetcher(), budget=budget)
    adapter.fetch("stages")  # small payload, under the running total
    with pytest.raises(SourceAdapterError, match="total download cap"):
        for _ in range(10):
            adapter.fetch("result/matrix", server="US")


def test_json_depth_cap() -> None:
    deep: dict[str, object] = {}
    node: dict[str, object] = deep
    for _ in range(20):
        child: dict[str, object] = {}
        node["nest"] = child
        node = child
    adapter = PenguinStatsAdapter(
        fetcher=_fetcher({MATRIX_URL: json.dumps(deep).encode("utf-8")}),
        limits=DownloadLimits(max_json_depth=4),
    )
    with pytest.raises(SourceAdapterError, match="depth cap"):
        adapter.fetch("result/matrix", server="US")


def test_json_node_cap() -> None:
    payload = {"matrix": [{"i": i} for i in range(50)]}
    adapter = PenguinStatsAdapter(
        fetcher=_fetcher({MATRIX_URL: json.dumps(payload).encode("utf-8")}),
        limits=DownloadLimits(max_json_nodes=5),
    )
    with pytest.raises(SourceAdapterError, match="node cap"):
        adapter.fetch("result/matrix", server="US")


def test_deeply_nested_json_capped_gracefully() -> None:
    """Pathologically deep JSON is rejected as a capped error, not a RecursionError."""
    payload = b"[" * 100_000 + b"]" * 100_000  # ~200 KB, under the per-file cap
    adapter = PenguinStatsAdapter(fetcher=_fetcher({MATRIX_URL: payload}))
    with pytest.raises(SourceAdapterError, match="nesting depth"):
        adapter.fetch("result/matrix", server="US")


def test_invalid_json_rejected() -> None:
    adapter = PenguinStatsAdapter(fetcher=_fetcher({MATRIX_URL: b"not json{"}))
    with pytest.raises(SourceAdapterError, match="invalid JSON"):
        adapter.fetch("result/matrix", server="US")
