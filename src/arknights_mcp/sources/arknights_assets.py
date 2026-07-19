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

import contextlib
import http.client
import json
import logging
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import unquote, urljoin, urlsplit

from arknights_mcp.importers.normalization import is_clean_level_path, normalize_level_id
from arknights_mcp.sources.base import (
    SourceAdapterError,
    SourceNotFoundError,
    json_within_limits,
)
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

_LOG = logging.getLogger(__name__)

#: Default source id for this adapter (matches the registry entry).
DEFAULT_SOURCE_ID = "arknights_assets_gamedata"

#: Relative paths only under these prefixes may be fetched (allowlist, §V18).
ALLOWED_PREFIXES: tuple[str, ...] = ("gamedata/excel/", "gamedata/levels/")

#: Discovered stage level files may only live under these prefixes -- narrower
#: than ``ALLOWED_PREFIXES`` so a crafted ``stage_table.levelId`` cannot enqueue an
#: arbitrary excel table as a "level" file (L8).
LEVEL_PREFIXES: tuple[str, ...] = ("gamedata/levels/",)


#: The fixed core files fetched every sync (enemy + stage/zone tables). These are
#: mandatory: a missing one fails the whole sync (a bad ``base_url`` must not
#: silently produce an empty build). The §V30 silent-empty guard is scoped to this
#: combat data.
CORE_FILES: tuple[str, ...] = (
    "gamedata/excel/enemy_handbook_table.json",
    "gamedata/levels/enemydata/enemy_database.json",
    "gamedata/excel/zone_table.json",
    "gamedata/excel/stage_table.json",
)

#: The stage table is fetched + parsed serially before level discovery fans out
#: (§V42 ordering): the discovered level paths come from it.
STAGE_TABLE_PATH = "gamedata/excel/stage_table.json"

#: Operator/module excel tables the pipeline importers read (``import_operators`` →
#: ``character_table``/``skill_table``; ``import_modules`` → ``uniequip_table``/
#: ``battle_equip_table``). In-scope for v0.1 (PRD §6.1) but *optional per snapshot*:
#: a combat-only snapshot legitimately lacks them and the pipeline imports the
#: domain empty (``operators.py`` / ``modules.py`` return empty if the table is
#: absent). They are therefore fetched every sync but tolerated-if-absent (404/410
#: skip+warn, B34 precedent) rather than added to the strict :data:`CORE_FILES` set,
#: so a combat-only snapshot still syncs. They MUST be attempted, though — else a
#: real ``sync`` silently builds ``operators=modules=0`` and ``get_operator`` /
#: ``compare_operator_modules`` return empty (B36; §V41). ``uniequip_data`` is read
#: from ``uniequip_table``'s ``equipDict`` and is not a separate file.
SUPPLEMENTARY_FILES: tuple[str, ...] = (
    "gamedata/excel/character_table.json",
    "gamedata/excel/skill_table.json",
    "gamedata/excel/uniequip_table.json",
    "gamedata/excel/battle_equip_table.json",
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
        # ``self._used += n`` is not atomic under threads (read-modify-write); the
        # parallel stager (§T79) charges from several workers, so the accumulate +
        # cap check run under a lock ∴ the total-download cap stays exact and still
        # fails closed (⊥ overshoot via a lost update; ⊥ TOCTOU past the cap, §V42).
        self._lock = threading.Lock()

    def charge(self, nbytes: int) -> None:
        with self._lock:
            self._used += nbytes
            if self._used > self._max:
                raise SourceAdapterError(f"sync exceeds total download cap ({self._max} bytes)")

    def check(self) -> None:
        """Raise if the cap is already exhausted, without charging (pre-fetch gate).

        A parallel stager calls this before starting each download so that once one
        worker trips the cap no further fetch begins; overshoot is bounded to the
        in-flight set (itself bounded by the worker count), not the whole queue (§V42).
        """
        with self._lock:
            if self._used > self._max:
                raise SourceAdapterError(f"sync exceeds total download cap ({self._max} bytes)")


class Fetcher(Protocol):
    """Fetches the bytes at an HTTPS URL, honoring a per-file byte cap."""

    def fetch(self, url: str, *, max_bytes: int) -> bytes: ...


#: HTTP status codes that carry a ``Location`` the fetcher follows (capped).
_REDIRECT_CODES: frozenset[int] = frozenset({301, 302, 303, 307, 308})


def _validate_redirect_target(
    newurl: str, *, origin_host: str | None, allow_cross_domain: bool
) -> None:
    """Enforce the same-domain + HTTPS-only redirect policy (§V1/§V42; PRD §17.4).

    A redirect that downgrades to plaintext or leaves the original request's host is
    refused (raised as ``SourceAdapterError``) unless ``allow_cross_domain`` is set,
    so a hostile ``Location`` header cannot escape the domain allowlist -- regardless
    of which worker thread followed the redirect.
    """
    if not newurl.lower().startswith("https://"):
        raise SourceAdapterError(f"refusing non-HTTPS redirect: {newurl!r}")
    if not allow_cross_domain and origin_host:
        target_host = (urlsplit(newurl).hostname or "").lower()
        if target_host != origin_host.lower():
            raise SourceAdapterError(
                f"refusing cross-domain redirect to {target_host!r} "
                f"(allowlisted host is {origin_host.lower()!r}); "
                f"same-domain policy (PRD §17.4)"
            )


class HttpsFetcher:
    """Default :class:`Fetcher`: HTTPS-only, redirect-capped, size-capped (§V1).

    Reuses one keep-alive :class:`http.client.HTTPSConnection` per worker thread
    (§T79/§V42) so a parallel ``sync`` pays the TLS handshake once per worker
    instead of once per file. The connection cache is thread-local -- an
    ``http.client`` connection is not thread-safe, so a connection is never shared
    across workers. Every §V1 gate (HTTPS-only, per-file byte cap, same-domain +
    depth-capped redirects) is applied per file regardless of the worker.
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
        self._local = threading.local()
        # Every connection ever opened, tracked so ``close`` can release sockets that
        # were opened on worker threads which have since exited: their thread-local
        # cache dies with the thread, but the socket would otherwise linger until GC.
        self._all_conns: list[http.client.HTTPSConnection] = []
        self._reg_lock = threading.Lock()

    def _connection(self, host: str, port: int | None) -> tuple[http.client.HTTPSConnection, bool]:
        """Return ``(connection, is_fresh)``; ``is_fresh`` marks a just-opened socket.

        A reused (non-fresh) connection may be a stale keep-alive the peer has
        silently dropped, so the caller retries a network error once on a fresh one.
        """
        cache: dict[tuple[str, int | None], http.client.HTTPSConnection] | None
        cache = getattr(self._local, "conns", None)
        if cache is None:
            cache = {}
            self._local.conns = cache
        conn = cache.get((host, port))
        if conn is not None:
            return conn, False
        conn = http.client.HTTPSConnection(host, port, timeout=self._timeout)
        cache[(host, port)] = conn
        with self._reg_lock:
            self._all_conns.append(conn)
        return conn, True

    def _drop(self, host: str, port: int | None) -> None:
        """Discard a broken/partly-consumed connection so it is never reused."""
        cache = getattr(self._local, "conns", None)
        if cache is None:
            return
        conn = cache.pop((host, port), None)
        if conn is not None:
            conn.close()

    def close(self) -> None:
        """Close every connection this fetcher opened, across all worker threads.

        The shared registry lets a single ``close`` from any thread release sockets
        opened on pool workers that have already exited, so a long CLI ``sync`` does
        not leak file descriptors until GC.
        """
        with self._reg_lock:
            conns = self._all_conns
            self._all_conns = []
        for conn in conns:
            with contextlib.suppress(OSError):  # defensive: already-closed socket
                conn.close()
        self._local.conns = {}

    def fetch(self, url: str, *, max_bytes: int) -> bytes:  # pragma: no cover - live network
        if not url.lower().startswith("https://"):
            raise SourceAdapterError(f"refusing non-HTTPS URL: {url!r}")
        origin_host = urlsplit(url).hostname
        current = url
        redirects = 0
        while True:
            split = urlsplit(current)
            host = split.hostname or ""
            port = split.port
            path = split.path or "/"
            if split.query:
                path = f"{path}?{split.query}"
            conn, fresh = self._connection(host, port)
            try:
                conn.request(
                    "GET",
                    path,
                    headers={"Connection": "keep-alive", "Accept-Encoding": "identity"},
                )
                response = conn.getresponse()
                status = response.status
                if status in _REDIRECT_CODES:
                    location = response.getheader("Location")
                    response.read()  # drain the body so the connection stays reusable
                    redirects += 1
                    if redirects > self._max_redirects:
                        raise SourceAdapterError(f"too many redirects (> {self._max_redirects})")
                    if not location:
                        raise SourceAdapterError(f"redirect without Location from {current!r}")
                    current = urljoin(current, location)
                    _validate_redirect_target(
                        current,
                        origin_host=origin_host,
                        allow_cross_domain=self._allow_cross_domain,
                    )
                    continue
                if status in (404, 410):
                    # A referenced file that is absent upstream is distinguished from
                    # any other transport failure so the sync stager can skip a pruned
                    # level file (B34) without swallowing a genuine 5xx / auth error.
                    response.read()
                    raise SourceNotFoundError(f"upstream file not found ({status}): {url!r}")
                if status != 200:
                    response.read()
                    raise SourceAdapterError(f"HTTP error {status} fetching {url!r}")
                # Read one byte past the cap so an over-cap body is detectable.
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise SourceAdapterError(
                        f"download exceeds per-file cap ({max_bytes} bytes): {url!r}"
                    )
                return data
            except SourceNotFoundError:
                # 404/410 drained its body above, so the connection is still
                # reusable -- keep it. Pruned level files (B34) are the hot path for
                # 404s; dropping here would rebuild TLS per pruned file and defeat
                # the keep-alive that §T79 exists to add.
                raise
            except SourceAdapterError:
                self._drop(host, port)
                raise
            except (http.client.HTTPException, OSError) as exc:
                self._drop(host, port)
                if not fresh:
                    # A reused keep-alive socket the peer dropped while idle: the
                    # request never reached a live server, so reconnect and retry
                    # once. The next _connection is fresh, so a genuine transport
                    # fault raises on the retry instead of looping.
                    continue
                raise SourceAdapterError(f"network error fetching {url!r}: {exc}") from exc


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
        max_parallel: int = 8,
    ) -> None:
        cleaned = base_url.strip()
        if not cleaned.lower().startswith("https://"):
            raise SourceAdapterError(f"sync base_url must be https://, got {base_url!r}")
        if max_parallel < 1:
            raise SourceAdapterError(f"max_parallel must be >= 1, got {max_parallel}")
        self.base_url: str = cleaned.rstrip("/")
        self.server: str = server
        self.source_id: str = source_id
        self._fetcher: Fetcher = (
            fetcher if fetcher is not None else HttpsFetcher(max_redirects=limits.max_redirects)
        )
        self._limits = limits
        # Bounds the download thread pool (§T79/§V42): 1 forces the serial fallback;
        # never an unbounded fan-out (2257 refs ⊥ 2257 sockets).
        self._max_parallel = max_parallel
        # A per-run budget may be injected so a multi-server sync shares one cap;
        # standalone use falls back to a per-adapter budget from ``limits``.
        self._budget = budget if budget is not None else DownloadBudget(limits.max_total_bytes)

    def _download(self, relative_path: str, staging_root: Path) -> Any:
        """Fetch one allowlisted file into staging (enforcing caps); return parsed JSON."""
        normalized = _validate_relative_path(relative_path)
        url = f"{self.base_url}/{normalized}"
        # Fail fast if a concurrent worker already tripped the run-level cap so no
        # further download starts once it is blown (bounds parallel overshoot to the
        # in-flight set, itself bounded by the worker count, §V42).
        self._budget.check()
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

    def _fetch_one(self, path: str, staging_root: Path, *, strict: bool) -> int:
        """Download one file; return 1 if optional-and-absent upstream, else 0.

        ``strict`` files (core tables) re-raise a :class:`SourceNotFoundError` so a
        bad ``base_url`` fails closed; optional files (supplementary tables, pruned
        level refs) count the 404/410 as a skip. Any other transport failure always
        propagates.
        """
        try:
            self._download(path, staging_root)
            return 0
        except SourceNotFoundError:
            if strict:
                raise
            return 1

    def _download_all(self, paths: Iterable[str], staging_root: Path, *, strict: bool) -> int:
        """Download every path with bounded parallelism; return the missing count.

        Each file is fetched independently: every §V1 gate (allowlist, per-file byte
        cap, JSON depth/node cap) runs per file inside ``_download`` regardless of the
        worker, the shared :class:`DownloadBudget` charge is thread-safe, and each file
        is written to its own normalized path so the staged output does not depend on
        completion order (§V42). ``max_parallel == 1`` runs the serial path (identical
        to the old behavior); the returned missing count is the exact sum across
        workers, and a strict fetch failure re-raises out of the pool (fail-closed).
        """
        items = list(paths)
        if not items:
            return 0
        workers = min(self._max_parallel, len(items))
        if workers <= 1:
            return sum(self._fetch_one(p, staging_root, strict=strict) for p in items)
        missing = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="arkmcp-sync") as pool:
            futures = [pool.submit(self._fetch_one, p, staging_root, strict=strict) for p in items]
            try:
                for future in as_completed(futures):
                    missing += future.result()  # re-raises a strict fetch failure, fail-closed
            except BaseException:
                # A fetch failed (strict 404, cap trip, transport fault): cancel every
                # not-yet-started download so a doomed run stops fanning out work.
                for f in futures:
                    f.cancel()
                raise
        return missing

    def stage(self, staging_root: str | Path) -> LocalSnapshotAdapter:
        """Download the allowlisted snapshot into ``staging_root`` and wrap it.

        Fetches the core files, the operator/module tables, and each stage's level
        file (discovered from the stage table), then returns a local adapter over the
        staging directory. All downloads obey the configured :class:`DownloadLimits`.

        Core files are mandatory: a missing one fails the whole sync (a bad
        ``base_url`` must not silently produce an empty build). Both the
        operator/module tables and the discovered level files are fetched but
        tolerated-if-absent (404/410 skip+warn): a discovered ``levelId`` may point at
        a retired event whose data is pruned (B34), and a combat-only snapshot may
        lack the operator/module tables entirely (B36) — the pipeline imports those
        domains empty (optional per snapshot). Both must still be *attempted*, else a
        real sync silently omits an in-scope domain (§V41). The §V30 gate still fails
        closed if 0 levels import overall.
        """
        root = Path(staging_root)
        root.mkdir(parents=True, exist_ok=True)
        # The stage table is fetched + parsed serially FIRST: level discovery fans
        # out from it, so it must be in hand before the pool starts (§V42 ordering).
        stage_table: Any = self._download(STAGE_TABLE_PATH, root)
        remaining_core = [f for f in CORE_FILES if f != STAGE_TABLE_PATH]
        self._download_all(remaining_core, root, strict=True)
        supp_missing = self._download_all(SUPPLEMENTARY_FILES, root, strict=False)
        if supp_missing:
            _LOG.warning(
                "sync %s: %d of %d operator/module table(s) were absent upstream "
                "(404/410) and were skipped; their domains import empty",
                self.server,
                supp_missing,
                len(SUPPLEMENTARY_FILES),
            )
        level_paths = self._discover_level_paths(stage_table)
        missing = self._download_all(level_paths, root, strict=False)
        if missing:
            _LOG.warning(
                "sync %s: %d of %d referenced level files were absent upstream "
                "(404/410) and were skipped; their stages import with no map/waves",
                self.server,
                missing,
                len(level_paths),
            )
        return LocalSnapshotAdapter(root, self.server, source_id=self.source_id)
