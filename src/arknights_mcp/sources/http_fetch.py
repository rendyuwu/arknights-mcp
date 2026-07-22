"""Shared HTTPS fetch machinery for network source adapters (§V1, §V37).

The single home for the network-transport primitives every allowlisted network
adapter reuses -- the HTTPS-only, redirect-capped, size-capped :class:`HttpsFetcher`,
the per-run total-download :class:`DownloadBudget`, the :class:`DownloadLimits`
caps, and the :func:`fetch_json` "download -> decode -> cap depth/nodes" sequence.
Both :class:`~arknights_mcp.sources.arknights_assets.ArknightsAssetsAdapter` and
:class:`~arknights_mcp.sources.penguin_statistics.PenguinStatsAdapter` import from
here so the safety caps are defined once and applied identically regardless of
source (§V37: shared logic exactly one home). These primitives are used only from
CLI ``sync``/``import`` jobs, never at query time (§V1/§V52).

The HTTP transport is injected (:class:`Fetcher`) so the caps are unit-testable
without live network; the default :class:`HttpsFetcher` refuses non-HTTPS URLs and
caps redirects and response size.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from arknights_mcp.sources.base import (
    SourceAdapterError,
    SourceNotFoundError,
    json_within_limits,
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

#: ``Content-Encoding`` values the fetcher can decompress (§V64). Anything else
#: (``br``, ``deflate``, …) is rejected rather than silently mis-decoded -- we only
#: advertise ``gzip`` in the request, so a compliant peer never returns another.
_GZIP_ENCODINGS: frozenset[str] = frozenset({"gzip", "x-gzip"})

#: Transport faults worth a bounded retry on a *fresh* connection (§V64/B55): a
#: read/connect stall (``socket.timeout`` is ``TimeoutError`` on 3.10+), a peer that
#: reset/aborted/refused the connection, or an HTTP framing error from a body the peer
#: dropped mid-response. A genuine hard fault -- TLS verification, DNS ``gaierror`` --
#: is a plain :class:`OSError` outside this set and fails closed with no retry, since
#: retrying it only stalls the run without any chance of success.
_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    http.client.HTTPException,
)


def _backoff_delay(attempt: int, base: float) -> float:
    """Exponential backoff for a 1-based retry ``attempt``: ``base * 2**(attempt-1)`` (§V64).

    Deterministic (no jitter) so the delay is unit-testable and the retry budget stays
    small and predictable: attempt 1 → ``base``, attempt 2 → ``2*base``, and so on.
    """
    return base * 2.0 ** (attempt - 1)


def _gunzip_capped(compressed: bytes, *, max_bytes: int, url: str) -> bytes:
    """Inflate a complete gzip body, capping the DECOMPRESSED size (§V64 zip-bomb guard).

    ``compressed`` is the full gzip stream (the caller has already bounded the
    *compressed* read to the per-file cap, so a body already over-cap compressed is
    rejected before reaching here). Decompression is bounded to ``max_bytes + 1`` output
    bytes: a stream that would inflate past the cap leaves compressed data in
    ``unconsumed_tail`` (or overshoots the one-byte margin), and is refused -- a small
    stream cannot inflate hugely past the §V1 per-file cap. A malformed stream raises a
    typed error, never a raw ``zlib`` traceback.
    """
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)  # 16 → expect a gzip (not raw zlib) header
    try:
        out = dec.decompress(compressed, max_bytes + 1)
        if dec.unconsumed_tail:
            # More decompressed output remains than the cap+1 margin allows.
            raise SourceAdapterError(
                f"decompressed download exceeds per-file cap ({max_bytes} bytes): {url!r}"
            )
        out += dec.flush()
    except zlib.error as exc:
        raise SourceAdapterError(f"invalid gzip body from {url!r}: {exc}") from exc
    if len(out) > max_bytes:
        raise SourceAdapterError(
            f"decompressed download exceeds per-file cap ({max_bytes} bytes): {url!r}"
        )
    return out


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

    Resilience (§V64/B55): a transient transport fault (:data:`_TRANSIENT_ERRORS` --
    read/connect timeout, connection reset, dropped-body framing error) is retried up
    to ``max_retries`` times with exponential backoff instead of aborting the whole
    ``sync``, so one stalled fetch among thousands does not kill the run; a genuine hard
    fault (TLS/DNS) still fails closed with no retry. Bodies are requested ``gzip`` and
    the per-file cap counts the *decompressed* size (zip-bomb guarded).
    """

    def __init__(
        self,
        *,
        max_redirects: int = DEFAULT_LIMITS.max_redirects,
        timeout: float = 30.0,
        allow_cross_domain: bool = False,
        max_retries: int = 2,
        backoff_base: float = 0.5,
    ):
        self._max_redirects = max_redirects
        self._timeout = timeout
        self._allow_cross_domain = allow_cross_domain
        # Bounded transient-retry budget (§V64): a fresh-connection timeout/reset is
        # retried this many times with _backoff_delay before it becomes a hard failure.
        self._max_retries = max_retries
        self._backoff_base = backoff_base
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
        retries = 0
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
                    headers={"Connection": "keep-alive", "Accept-Encoding": "gzip"},
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
                # Read one byte past the cap so an over-cap body is detectable. The body
                # may arrive gzip-compressed (§V64): the cap counts the DECOMPRESSED
                # bytes -- the JSON we parse -- so a small stream that inflates hugely is
                # refused (zip-bomb guard). The compressed read is itself cap-bounded, so
                # a body already over-cap compressed is rejected before any inflate.
                encoding = (response.getheader("Content-Encoding") or "").strip().lower()
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise SourceAdapterError(
                        f"download exceeds per-file cap ({max_bytes} bytes): {url!r}"
                    )
                if encoding in _GZIP_ENCODINGS:
                    return _gunzip_capped(data, max_bytes=max_bytes, url=url)
                if encoding and encoding != "identity":
                    raise SourceAdapterError(
                        f"unsupported Content-Encoding {encoding!r} from {url!r}"
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
            except _TRANSIENT_ERRORS as exc:
                # A transient transport fault (timeout, reset, dropped-body framing).
                self._drop(host, port)
                if not fresh:
                    # A reused keep-alive socket the peer dropped while idle: the
                    # request never reached a live server, so reconnect for free (the
                    # next _connection is fresh) WITHOUT spending the retry budget.
                    continue
                if retries < self._max_retries:
                    # A genuine stall/reset on a live connection -- one blip among the
                    # thousands of files a full sync fetches. Back off and retry rather
                    # than aborting the whole run (B55); the run only fails once the
                    # bounded budget is exhausted.
                    retries += 1
                    time.sleep(_backoff_delay(retries, self._backoff_base))
                    continue
                raise SourceAdapterError(
                    f"network error fetching {url!r} after {self._max_retries} retries: {exc}"
                ) from exc
            except OSError as exc:
                # A non-transient transport fault (TLS verification, DNS resolution):
                # retrying cannot help, so fail closed immediately.
                self._drop(host, port)
                raise SourceAdapterError(f"network error fetching {url!r}: {exc}") from exc


def fetch_json(
    fetcher: Fetcher,
    url: str,
    *,
    limits: DownloadLimits,
    budget: DownloadBudget,
) -> tuple[bytes, Any]:
    """Fetch one HTTPS URL under the run budget + per-file cap, then parse + cap it.

    The one home (§V37) for the "download bytes → decode → cap depth/nodes" sequence
    every network source adapter shares, so the size / depth / node caps are applied
    identically regardless of source. Returns ``(raw_bytes, parsed)`` -- the raw bytes
    let a caller stage the file for hashing/provenance (§V17); the parsed value has
    already passed the depth/node caps. Fails closed on an over-cap or malformed
    document (never an uncaught ``RecursionError`` on pathologically deep JSON).
    """
    # Fail fast if a concurrent worker already tripped the run-level cap so no
    # further download starts once it is blown (bounds parallel overshoot to the
    # in-flight set, itself bounded by the worker count, §V42).
    budget.check()
    data = fetcher.fetch(url, max_bytes=limits.max_file_bytes)
    # ``data`` is the DECOMPRESSED body (§V64): charging its length is conservative --
    # it is >= the gzip bytes actually transferred, so the run-level download cap trips
    # no later than the real network volume would (fail-safe direction).
    budget.charge(len(data))
    try:
        parsed = json.loads(data.decode("utf-8"))
    except RecursionError as exc:
        # Pathologically deep JSON blows the parser's stack before the depth cap
        # below can reject it; surface a graceful capped error, not a traceback.
        raise SourceAdapterError(f"JSON exceeds safe nesting depth from {url!r}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SourceAdapterError(f"invalid JSON downloaded from {url!r}: {exc}") from exc
    json_within_limits(parsed, max_depth=limits.max_json_depth, max_nodes=limits.max_json_nodes)
    return data, parsed
