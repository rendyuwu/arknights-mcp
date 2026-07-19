"""Primary allowlisted source adapter: ``arknights_assets_gamedata`` (§T21; §V1).

This is the only network-touching adapter. It is used **exclusively** by the CLI
``sync`` job (never at query time, §V1): it downloads a fixed allowlist of
gameplay JSON files over HTTPS into an isolated staging directory, enforcing size,
JSON-depth, record-count, and redirect limits (PRD §11.2), and then hands the
import pipeline an ordinary local, read-only :class:`LocalSnapshotAdapter` rooted
at that staging directory. The network concern is therefore fully contained here;
everything downstream sees only local files.

The actual HTTP transport is injected (:class:`Fetcher`) so the safety limits are
unit-testable without live network access; the default :class:`HttpsFetcher`
refuses non-HTTPS URLs and caps redirects and response size.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from arknights_mcp.importers.normalization import is_clean_level_path, normalize_level_id
from arknights_mcp.sources.base import SourceAdapterError, json_within_limits
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

#: Default source id for this adapter (matches the registry entry).
DEFAULT_SOURCE_ID = "arknights_assets_gamedata"

#: Relative paths only under these prefixes may be fetched (allowlist, §V18).
ALLOWED_PREFIXES: tuple[str, ...] = ("gamedata/excel/", "gamedata/levels/")

#: Discovered stage level files may only live under these prefixes -- narrower
#: than ``ALLOWED_PREFIXES`` so a crafted ``stage_table.levelId`` cannot enqueue an
#: arbitrary excel table as a "level" file (L8).
LEVEL_PREFIXES: tuple[str, ...] = ("gamedata/levels/",)


#: The fixed core files fetched every sync (enemy + stage/zone tables).
CORE_FILES: tuple[str, ...] = (
    "gamedata/excel/enemy_handbook_table.json",
    "gamedata/levels/enemydata/enemy_database.json",
    "gamedata/excel/zone_table.json",
    "gamedata/excel/stage_table.json",
)


@dataclass(frozen=True)
class DownloadLimits:
    """Resource caps applied to every sync download (PRD §11.2)."""

    max_file_bytes: int = 32 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    max_json_depth: int = 64
    max_json_nodes: int = 2_000_000
    max_redirects: int = 5


DEFAULT_LIMITS = DownloadLimits()


class DownloadBudget:
    """Mutable run-level total-byte accumulator (PRD §11.2 total-download cap).

    Shared across every adapter in one ``sync`` run so the cap bounds the whole
    run, not each server independently: ``sync --server all`` may not exceed
    ``max_total_bytes`` in aggregate.
    """

    def __init__(self, max_total_bytes: int) -> None:
        self._max = max_total_bytes
        self._used = 0

    def charge(self, nbytes: int) -> None:
        self._used += nbytes
        if self._used > self._max:
            raise SourceAdapterError(f"sync exceeds total download cap ({self._max} bytes)")


class Fetcher(Protocol):
    """Fetches the bytes at an HTTPS URL, honoring a per-file byte cap."""

    def fetch(self, url: str, *, max_bytes: int) -> bytes: ...


class HttpsFetcher:
    """Default :class:`Fetcher`: HTTPS-only, redirect-capped, size-capped (§V1).

    Redirects are same-domain by default (PRD §17.4): a redirect that leaves the
    original request's host is refused unless ``allow_cross_domain`` is explicitly
    set, so the domain allowlist cannot be escaped via a 302.
    """

    def __init__(
        self,
        *,
        max_redirects: int = DEFAULT_LIMITS.max_redirects,
        timeout: float = 30.0,
        allow_cross_domain: bool = False,
    ):
        self._max_redirects = max_redirects
        self._timeout = timeout
        self._allow_cross_domain = allow_cross_domain

    def fetch(self, url: str, *, max_bytes: int) -> bytes:  # pragma: no cover - live network
        if not url.lower().startswith("https://"):
            raise SourceAdapterError(f"refusing non-HTTPS URL: {url!r}")
        handler = _BoundedRedirectHandler(
            self._max_redirects,
            allowed_host=urlsplit(url).hostname,
            allow_cross_domain=self._allow_cross_domain,
        )
        opener = urllib.request.build_opener(handler)
        with opener.open(url, timeout=self._timeout) as response:  # noqa: S310 - https enforced above
            data: bytes = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise SourceAdapterError(f"download exceeds per-file cap ({max_bytes} bytes): {url!r}")
        return data


class _BoundedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Caps redirect depth; refuses non-HTTPS and (by default) cross-domain targets."""

    def __init__(
        self,
        max_redirects: int,
        *,
        allowed_host: str | None = None,
        allow_cross_domain: bool = False,
    ) -> None:
        self._max = max_redirects
        self._count = 0
        self._allowed_host = (allowed_host or "").lower()
        self._allow_cross_domain = allow_cross_domain

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not newurl.lower().startswith("https://"):
            raise SourceAdapterError(f"refusing non-HTTPS redirect: {newurl!r}")
        if not self._allow_cross_domain and self._allowed_host:
            target_host = (urlsplit(newurl).hostname or "").lower()
            if target_host != self._allowed_host:
                raise SourceAdapterError(
                    f"refusing cross-domain redirect to {target_host!r} "
                    f"(allowlisted host is {self._allowed_host!r}); "
                    f"same-domain policy (PRD §17.4)"
                )
        self._count += 1
        if self._count > self._max:
            raise SourceAdapterError(f"too many redirects (> {self._max})")
        return super().redirect_request(  # pragma: no cover - live network
            req, fp, code, msg, headers, newurl
        )


def _validate_relative_path(
    relative_path: str, *, allowed_prefixes: tuple[str, ...] = ALLOWED_PREFIXES
) -> str:
    """Reject absolute paths, traversal, and paths outside the allowlist (§V18).

    Both the literal path and its percent-decoded form are checked: the remote
    server decodes ``%2f`` back to ``/``, so ``a/..%2f..%2fsecret`` would escape the
    allowlist on the wire even though its literal ``.`` parts look clean (L7).
    """
    decoded = unquote(relative_path)
    for candidate in (relative_path, decoded):
        if candidate.startswith("/") or (len(candidate) > 1 and candidate[1] == ":"):
            raise SourceAdapterError(f"absolute paths are not allowed: {relative_path!r}")
        if any(part == ".." for part in PurePosixPath(candidate).parts):
            raise SourceAdapterError(f"path traversal is not allowed: {relative_path!r}")
        normalized_candidate = PurePosixPath(candidate).as_posix()
        if not any(normalized_candidate.startswith(prefix) for prefix in allowed_prefixes):
            raise SourceAdapterError(f"path not in allowlist: {relative_path!r}")
    return PurePosixPath(relative_path).as_posix()


class ArknightsAssetsAdapter:
    """Network stager for the primary source; produces a local snapshot (§V1)."""

    #: This adapter performs network I/O; it is only ever run from CLI sync (§V1).
    touches_network: bool = True

    def __init__(
        self,
        base_url: str,
        server: str,
        *,
        fetcher: Fetcher | None = None,
        limits: DownloadLimits = DEFAULT_LIMITS,
        source_id: str = DEFAULT_SOURCE_ID,
        budget: DownloadBudget | None = None,
    ) -> None:
        cleaned = base_url.strip()
        if not cleaned.lower().startswith("https://"):
            raise SourceAdapterError(f"sync base_url must be https://, got {base_url!r}")
        self.base_url: str = cleaned.rstrip("/")
        self.server: str = server
        self.source_id: str = source_id
        self._fetcher: Fetcher = (
            fetcher if fetcher is not None else HttpsFetcher(max_redirects=limits.max_redirects)
        )
        self._limits = limits
        # A per-run budget may be injected so a multi-server sync shares one cap;
        # standalone use falls back to a per-adapter budget from ``limits``.
        self._budget = budget if budget is not None else DownloadBudget(limits.max_total_bytes)

    def _download(self, relative_path: str, staging_root: Path) -> Any:
        """Fetch one allowlisted file into staging (enforcing caps); return parsed JSON."""
        normalized = _validate_relative_path(relative_path)
        url = f"{self.base_url}/{normalized}"
        data = self._fetcher.fetch(url, max_bytes=self._limits.max_file_bytes)
        self._budget.charge(len(data))
        try:
            parsed = json.loads(data.decode("utf-8"))
        except RecursionError as exc:
            # Pathologically deep JSON blows the parser's stack before the depth
            # cap below can reject it; surface a graceful capped error instead of
            # an uncaught traceback (the file never gets staged/imported).
            raise SourceAdapterError(f"JSON exceeds safe nesting depth from {url!r}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourceAdapterError(f"invalid JSON downloaded from {url!r}: {exc}") from exc
        json_within_limits(
            parsed, max_depth=self._limits.max_json_depth, max_nodes=self._limits.max_json_nodes
        )
        target = staging_root / normalized
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return parsed

    def _discover_level_paths(self, stage_table: Any) -> list[str]:
        """Collect allowlisted level-file paths referenced by the stage table.

        The real ``levelId`` is a Title-case, extension-less reference
        (``Obt/Main/level_main_04-04``); it is rewritten to the actual snapshot
        path before the allowlist check (§V29/§V30) so the level file is actually
        fetched. ``normalize_level_id`` always forces the result under
        ``gamedata/levels/``, so a crafted ``levelId`` still cannot enqueue an excel
        table (L8), and traversal is rejected by ``_validate_relative_path`` at
        download time.
        """
        paths: list[str] = []
        if not isinstance(stage_table, dict):
            return paths
        stages = stage_table.get("stages", {})
        if not isinstance(stages, dict):
            return paths
        seen: set[str] = set()
        for entry in stages.values():
            if not isinstance(entry, dict):
                continue
            level_id = entry.get("levelId")
            if not isinstance(level_id, str):
                continue
            resolved = normalize_level_id(level_id)
            if resolved is None or resolved in seen:
                continue
            seen.add(resolved)
            # Confine discovery to the levels tree post-normalization (§V36): a
            # crafted levelId must not fetch an excel table or escape the tree.
            if is_clean_level_path(resolved):
                paths.append(resolved)
        return sorted(paths)

    def stage(self, staging_root: str | Path) -> LocalSnapshotAdapter:
        """Download the allowlisted snapshot into ``staging_root`` and wrap it.

        Fetches the core files, discovers each stage's level file from the stage
        table, downloads those too, then returns a local adapter over the staging
        directory. All downloads obey the configured :class:`DownloadLimits`.
        """
        root = Path(staging_root)
        root.mkdir(parents=True, exist_ok=True)
        stage_table: Any = None
        for relative_path in CORE_FILES:
            parsed = self._download(relative_path, root)
            if relative_path.endswith("stage_table.json"):
                stage_table = parsed
        for level_path in self._discover_level_paths(stage_table):
            self._download(level_path, root)
        return LocalSnapshotAdapter(root, self.server, source_id=self.source_id)
