"""Extra-locale alias source adapter (§T99; §V57, §V18, §V16, §V1).

A network-touching adapter (``touches_network=True``) used **exclusively** by CLI
``sync``/``import`` jobs, never at query time (§V1): it fetches the jp/kr gamedata
files that carry an entity's canonical NAME in that locale (``character_table.json``
for operators, ``enemy_handbook_table.json`` for enemies) over HTTPS under the
shared size / JSON-depth / node-count / redirect caps
(:mod:`arknights_mcp.sources.http_fetch`, §V37). The importer
(:mod:`arknights_mcp.importers.extra_locale_aliases`) consumes what this returns and
keeps only the NAME (``LOCALE_NAME_ALLOWLIST``); no other field is read.

The scope is NAME-ONLY (§V57, extends D6/§V18): only the per-locale canonical NAME
is imported, as a locale-tagged alias on the *existing* en/cn entity (matched by
``game_id``). A machine-translated description / prose is never stored (D6 forbids
bulk MT). An extra locale (``jp``/``kr``) is a NAME alias only -- it is NOT a new
fact region: the entity still returns its OWN en/cn region facts (§V57).

The two file paths are fixed constants; no path segment is ever built from caller
input, so traversal / SSRF into another path is impossible (like the announcement
adapter). ``base_url`` points at the jp/kr gamedata tree; the region argument
(``jp``/``kr``) only tags the fetch and is validated against the known extra locales.
"""

from __future__ import annotations

from typing import Any

from arknights_mcp.importers.field_policy import EXTRA_LOCALE_FOR_REGION
from arknights_mcp.sources.base import SourceAdapterError
from arknights_mcp.sources.http_fetch import (
    DEFAULT_LIMITS,
    DownloadBudget,
    DownloadLimits,
    Fetcher,
    HttpsFetcher,
    fetch_json,
)

#: Default registry source id for this adapter (matches ``data_sources.toml`` when
#: the source is later wired into the registry + a sync ride-along).
DEFAULT_SOURCE_ID = "arknights_extra_locale_names"

#: The operator NAME file (``character_table``) and enemy NAME file
#: (``enemy_handbook_table``) fetched per extra locale. Only these two are read: the
#: importer keeps just the canonical NAME from each (§V57 NAME-only). Fixed paths --
#: no caller input is ever concatenated into a URL segment (§V1 no traversal).
CHARACTER_TABLE_PATH = "gamedata/excel/character_table.json"
ENEMY_HANDBOOK_PATH = "gamedata/excel/enemy_handbook_table.json"


class ExtraLocaleAliasAdapter:
    """CLI-only network adapter for a jp/kr NAME snapshot (§V57/§V1).

    Fetches the two configured NAME files over HTTPS and returns the parsed JSON,
    applying every §V1 gate (HTTPS-only, per-file byte cap, JSON depth/node cap,
    run-level total cap, capped same-domain redirects) via the shared
    :func:`fetch_json`. It never touches the network at query time and is only ever
    constructed by a CLI job (§V1). Both files live under a fixed relative path, so
    no path segment is built from caller input (no traversal / SSRF).
    """

    #: This adapter performs network I/O; it is only ever run from CLI sync (§V1).
    touches_network: bool = True

    def __init__(
        self,
        base_url: str,
        region: str,
        *,
        fetcher: Fetcher | None = None,
        limits: DownloadLimits = DEFAULT_LIMITS,
        source_id: str = DEFAULT_SOURCE_ID,
        budget: DownloadBudget | None = None,
    ) -> None:
        cleaned = base_url.strip()
        if not cleaned.lower().startswith("https://"):
            raise SourceAdapterError(f"extra-locale base_url must be https://, got {base_url!r}")
        if region not in EXTRA_LOCALE_FOR_REGION:
            # §V57: an extra-locale alias region must be a known extra locale (jp/kr);
            # en/cn are fact regions imported by the primary adapter, never here.
            allowed = "|".join(sorted(EXTRA_LOCALE_FOR_REGION))
            raise SourceAdapterError(f"extra-locale region must be {allowed}, got {region!r}")
        self.base_url: str = cleaned.rstrip("/")
        self.region: str = region
        self.source_id: str = source_id
        self._fetcher: Fetcher = (
            fetcher if fetcher is not None else HttpsFetcher(max_redirects=limits.max_redirects)
        )
        self._limits = limits
        # A per-run budget may be injected so a multi-region sync shares one cap;
        # standalone use falls back to a per-adapter budget from ``limits``.
        self._budget = budget if budget is not None else DownloadBudget(limits.max_total_bytes)

    def _fetch_one(self, relative_path: str) -> Any:
        url = f"{self.base_url}/{relative_path}"
        _, parsed = fetch_json(self._fetcher, url, limits=self._limits, budget=self._budget)
        return parsed

    def fetch(self) -> dict[str, Any]:
        """Fetch the two NAME tables and return them under stable keys (§V1).

        Returns ``{"character_table": ..., "enemy_handbook": ...}``. Fails closed on
        a non-HTTPS URL, an over-cap/malformed body, or a pathologically deep
        document (surfaced as a capped :class:`SourceAdapterError`, never an uncaught
        traceback).
        """
        return {
            "character_table": self._fetch_one(CHARACTER_TABLE_PATH),
            "enemy_handbook": self._fetch_one(ENEMY_HANDBOOK_PATH),
        }
