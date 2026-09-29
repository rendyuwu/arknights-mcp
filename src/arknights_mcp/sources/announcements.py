"""Official-announcement source adapter.

A network-touching adapter (``touches_network=True``) used **exclusively** by CLI
``sync``/``import`` jobs, never at query time: it fetches the official
Arknights (Global/CN) announcement *feed* over HTTPS under the shared size /
JSON-depth / node-count / redirect caps (:mod:`arknights_mcp.sources.http_fetch`).
The importer (:mod:`arknights_mcp.importers.announcements`) consumes what
this returns and keeps only the metadata allowlist; this adapter is transport
only.

The scope is METADATA-ONLY (D14): the feed carries announcement metadata, and
the importer's field allowlist keeps only ``announceId``/``title``/``date``/``url``/
``category``. The full announcement BODY / html / prose / image is never fetched into
storage. The source stays **disabled by default** in the registry; enabling it
requires an explicit config change plus a recorded policy review (D14).
"""

from __future__ import annotations

from typing import Any

from arknights_mcp.sources.base import SourceAdapterError
from arknights_mcp.sources.http_fetch import (
    DEFAULT_LIMITS,
    DownloadBudget,
    DownloadLimits,
    Fetcher,
    HttpsFetcher,
    fetch_json,
)

#: Registry source ids for the two announcement feeds (match ``data_sources.toml``).
GLOBAL_SOURCE_ID = "arknights_global_official_news"
CN_SOURCE_ID = "arknights_cn_official_news"

#: Fact region -> registry source id: en is the Global feed, cn the CN feed.
#: A region outside {en,cn} has no announcement source.
SOURCE_ID_FOR_REGION: dict[str, str] = {"en": GLOBAL_SOURCE_ID, "cn": CN_SOURCE_ID}


def source_id_for_region(region: str) -> str | None:
    """Registry source id for an en/cn announcement feed, or ``None`` if none."""
    return SOURCE_ID_FOR_REGION.get(region)


class AnnouncementsAdapter:
    """CLI-only network adapter for the official announcement feed.

    Fetches the configured feed URL over HTTPS and returns the parsed JSON, applying
    every gate (HTTPS-only, per-file byte cap, JSON depth/node cap, run-level
    total cap, capped same-domain redirects) via the shared :func:`fetch_json`. It
    never touches the network at query time and is only ever constructed by a CLI
    job. The feed is a single URL configured per source; no path segment is
    ever built from caller input, so traversal / SSRF into another path is impossible.
    """

    #: This adapter performs network I/O; it is only ever run from CLI sync.
    touches_network: bool = True

    def __init__(
        self,
        feed_url: str,
        region: str,
        *,
        fetcher: Fetcher | None = None,
        limits: DownloadLimits = DEFAULT_LIMITS,
        source_id: str | None = None,
        budget: DownloadBudget | None = None,
    ) -> None:
        cleaned = feed_url.strip()
        if not cleaned.lower().startswith("https://"):
            raise SourceAdapterError(f"announcement feed_url must be https://, got {feed_url!r}")
        resolved_source_id = source_id if source_id is not None else source_id_for_region(region)
        if resolved_source_id is None:
            # An announcement region must be en or cn -- never mislabelled.
            raise SourceAdapterError(f"announcement region must be en|cn, got {region!r}")
        self.feed_url: str = cleaned
        self.server: str = region
        self.source_id: str = resolved_source_id
        self._fetcher: Fetcher = (
            fetcher if fetcher is not None else HttpsFetcher(max_redirects=limits.max_redirects)
        )
        self._limits = limits
        # A per-run budget may be injected so a multi-region sync shares one cap;
        # standalone use falls back to a per-adapter budget from ``limits``.
        self._budget = budget if budget is not None else DownloadBudget(limits.max_total_bytes)

    def fetch(self) -> Any:
        """Fetch the announcement feed and return the capped, parsed JSON.

        Fails closed on a non-HTTPS URL, an over-cap/malformed body, or a
        pathologically deep document (surfaced as a capped ``SourceAdapterError``,
        never an uncaught traceback).
        """
        _, parsed = fetch_json(
            self._fetcher, self.feed_url, limits=self._limits, budget=self._budget
        )
        return parsed
