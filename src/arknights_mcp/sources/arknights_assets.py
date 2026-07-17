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

from arknights_mcp.sources.base import SourceAdapterError
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

#: Default source id for this adapter (matches the registry entry).
DEFAULT_SOURCE_ID = "arknights_assets_gamedata"

#: Relative paths only under these prefixes may be fetched (allowlist, §V18).
ALLOWED_PREFIXES: tuple[str, ...] = ("gamedata/excel/", "gamedata/levels/")

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


class Fetcher(Protocol):
    """Fetches the bytes at an HTTPS URL, honoring a per-file byte cap."""

    def fetch(self, url: str, *, max_bytes: int) -> bytes: ...


class HttpsFetcher:
    """Default :class:`Fetcher`: HTTPS-only, redirect-capped, size-capped (§V1)."""

    def __init__(self, *, max_redirects: int = DEFAULT_LIMITS.max_redirects, timeout: float = 30.0):
        self._max_redirects = max_redirects
        self._timeout = timeout

    def fetch(self, url: str, *, max_bytes: int) -> bytes:  # pragma: no cover - live network
        if not url.lower().startswith("https://"):
            raise SourceAdapterError(f"refusing non-HTTPS URL: {url!r}")
        opener = urllib.request.build_opener(_BoundedRedirectHandler(self._max_redirects))
        with opener.open(url, timeout=self._timeout) as response:  # noqa: S310 - https enforced above
            data: bytes = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise SourceAdapterError(f"download exceeds per-file cap ({max_bytes} bytes): {url!r}")
        return data


class _BoundedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Caps redirect depth and refuses non-HTTPS redirect targets."""

    def __init__(self, max_redirects: int) -> None:
        self._max = max_redirects
        self._count = 0

    def redirect_request(  # pragma: no cover - live network
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
        self._count += 1
        if self._count > self._max:
            raise SourceAdapterError(f"too many redirects (> {self._max})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_relative_path(relative_path: str) -> str:
    """Reject absolute paths, traversal, and paths outside the allowlist (§V18)."""
    if relative_path.startswith("/") or (len(relative_path) > 1 and relative_path[1] == ":"):
        raise SourceAdapterError(f"absolute paths are not allowed: {relative_path!r}")
    posix = PurePosixPath(relative_path)
    if any(part == ".." for part in posix.parts):
        raise SourceAdapterError(f"path traversal is not allowed: {relative_path!r}")
    normalized = posix.as_posix()
    if not any(normalized.startswith(prefix) for prefix in ALLOWED_PREFIXES):
        raise SourceAdapterError(f"path not in allowlist: {relative_path!r}")
    return normalized


def _json_within_limits(obj: Any, *, max_depth: int, max_nodes: int) -> None:
    """Bound JSON nesting depth and node count (anti-abuse; §V19-adjacent)."""
    nodes = 0

    def walk(value: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            raise SourceAdapterError(f"JSON exceeds node cap ({max_nodes})")
        if depth > max_depth:
            raise SourceAdapterError(f"JSON exceeds depth cap ({max_depth})")
        if isinstance(value, dict):
            for item in value.values():
                walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

    walk(obj, 1)


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
        self._total_bytes = 0

    def _download(self, relative_path: str, staging_root: Path) -> Any:
        """Fetch one allowlisted file into staging (enforcing caps); return parsed JSON."""
        normalized = _validate_relative_path(relative_path)
        url = f"{self.base_url}/{normalized}"
        data = self._fetcher.fetch(url, max_bytes=self._limits.max_file_bytes)
        self._total_bytes += len(data)
        if self._total_bytes > self._limits.max_total_bytes:
            raise SourceAdapterError(
                f"sync exceeds total download cap ({self._limits.max_total_bytes} bytes)"
            )
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourceAdapterError(f"invalid JSON downloaded from {url!r}: {exc}") from exc
        _json_within_limits(
            parsed, max_depth=self._limits.max_json_depth, max_nodes=self._limits.max_json_nodes
        )
        target = staging_root / normalized
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return parsed

    def _discover_level_paths(self, stage_table: Any) -> list[str]:
        """Collect allowlisted level-file paths referenced by the stage table."""
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
            if not isinstance(level_id, str) or level_id in seen:
                continue
            seen.add(level_id)
            if any(level_id.startswith(prefix) for prefix in ALLOWED_PREFIXES):
                paths.append(level_id)
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


class DictFetcher:
    """In-memory :class:`Fetcher` backed by a ``{url: bytes}`` map (tests only)."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = dict(files)

    def fetch(self, url: str, *, max_bytes: int) -> bytes:
        if url not in self._files:
            raise SourceAdapterError(f"not found: {url!r}")
        data = self._files[url]
        if len(data) > max_bytes:
            raise SourceAdapterError(f"download exceeds per-file cap ({max_bytes} bytes): {url!r}")
        return data


def dict_fetcher_from_snapshot(base_url: str, root: str | Path) -> DictFetcher:
    """Build a :class:`DictFetcher` mapping ``base_url``/<rel> to a local tree's bytes."""
    base = base_url.rstrip("/")
    root_path = Path(root)
    files: dict[str, bytes] = {}
    for path in root_path.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root_path).as_posix()
            files[f"{base}/{rel}"] = path.read_bytes()
    return DictFetcher(files)
