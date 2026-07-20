"""Penguin Statistics drop-rate source adapter (§T87; §V52, §V53, §V1).

A network-touching adapter (``touches_network=True``) used **exclusively** by CLI
``sync``/``import`` jobs, never at query time (§V1/§V52): it fetches observed
drop-rate statistics from the documented Penguin Statistics v2 HTTP API over
HTTPS, under a fixed endpoint allowlist and the shared size / JSON-depth /
node-count / redirect caps (:mod:`arknights_mcp.sources.http_fetch`, §V37). The
importer (§T89) consumes what this returns and stamps expiry + attribution + region
provenance (§V53/§V54); this adapter is transport only.

Penguin Statistics data is licensed CC BY-NC 4.0 — noncommercial use with
attribution (§V53; recorded in the source registry and ``NOTICE``).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from arknights_mcp.sources.base import SourceAdapterError
from arknights_mcp.sources.http_fetch import (
    DEFAULT_LIMITS,
    DownloadBudget,
    DownloadLimits,
    Fetcher,
    HttpsFetcher,
    fetch_json,
)

#: Source id for this adapter (matches the registry entry key).
DEFAULT_SOURCE_ID = "penguin_statistics"

#: Base URL of the documented Penguin Statistics v2 read API (HTTPS only, §V1).
PENGUIN_BASE_URL = "https://penguin-stats.io/PenguinStats/api/v2"

#: Fixed endpoint allowlist (§V18): only these documented v2 read endpoints are
#: ever fetched. An exact-match set — no path segment is built from caller input, so
#: traversal / SSRF into another path is impossible. ``result/matrix`` is the drop
#: matrix; ``stages``/``items`` are the metadata the importer joins against (§T89).
ALLOWED_ENDPOINTS: frozenset[str] = frozenset({"result/matrix", "stages", "items"})

#: Penguin's server codes. The penguin-server → fact-region mapping (US/Global→en,
#: CN→cn, jp/kr dropped) is the importer's concern (§V54/§T89); here the server is
#: validated against this closed set only so it cannot inject arbitrary query text.
ALLOWED_SERVERS: frozenset[str] = frozenset({"CN", "US", "JP", "KR"})


class PenguinStatsAdapter:
    """CLI-only network adapter for Penguin Statistics drop data (§V52/§V1).

    Fetches an allowlisted v2 endpoint and returns the parsed JSON, applying every
    §V1 gate (HTTPS-only, per-file byte cap, JSON depth/node cap, run-level total
    cap, capped same-domain redirects) via the shared :func:`fetch_json`. It never
    touches the network at query time and is only ever constructed by a CLI job.
    """

    #: This adapter performs network I/O; it is only ever run from CLI sync (§V1).
    touches_network: bool = True

    def __init__(
        self,
        base_url: str = PENGUIN_BASE_URL,
        *,
        fetcher: Fetcher | None = None,
        limits: DownloadLimits = DEFAULT_LIMITS,
        source_id: str = DEFAULT_SOURCE_ID,
        budget: DownloadBudget | None = None,
    ) -> None:
        cleaned = base_url.strip()
        if not cleaned.lower().startswith("https://"):
            raise SourceAdapterError(f"penguin base_url must be https://, got {base_url!r}")
        self.base_url: str = cleaned.rstrip("/")
        self.source_id: str = source_id
        self._fetcher: Fetcher = (
            fetcher if fetcher is not None else HttpsFetcher(max_redirects=limits.max_redirects)
        )
        self._limits = limits
        # A per-run budget may be injected so a multi-endpoint fetch shares one cap;
        # standalone use falls back to a per-adapter budget from ``limits``.
        self._budget = budget if budget is not None else DownloadBudget(limits.max_total_bytes)

    def _build_url(self, endpoint: str, server: str | None) -> str:
        """Build the request URL from validated components only (no free-text path)."""
        if endpoint not in ALLOWED_ENDPOINTS:
            raise SourceAdapterError(f"endpoint not in penguin allowlist: {endpoint!r}")
        url = f"{self.base_url}/{endpoint}"
        if server is not None:
            if server not in ALLOWED_SERVERS:
                raise SourceAdapterError(f"unknown penguin server: {server!r}")
            url = f"{url}?{urlencode({'server': server})}"
        return url

    def fetch(self, endpoint: str, *, server: str | None = None) -> Any:
        """Fetch one allowlisted endpoint and return the capped, parsed JSON (§V1).

        ``server`` (when given) must be a known penguin server code; it is passed as
        the ``server`` query parameter. Fails closed on a non-allowlisted endpoint,
        an unknown server, an over-cap/malformed body, or a non-HTTPS URL.
        """
        url = self._build_url(endpoint, server)
        _, parsed = fetch_json(self._fetcher, url, limits=self._limits, budget=self._budget)
        return parsed
