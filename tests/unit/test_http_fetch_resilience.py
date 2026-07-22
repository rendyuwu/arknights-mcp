"""T125: sync fetch resilience — transient retry + gzip decompressed-cap (§V64/B55).

Exercises the safety + robustness machinery added to :class:`HttpsFetcher`:

* the pure helpers -- :func:`_backoff_delay` (deterministic exponential backoff) and
  :func:`_gunzip_capped` (inflate with a decompressed-size cap, zip-bomb guarded);
* the :data:`_TRANSIENT_ERRORS` classification (what earns a retry vs. a hard fail);
* the retry loop itself, driven through a stub ``_connection`` so a fresh-connection
  timeout is exercised without live network -- the exact B55 path where one stalled
  fetch used to abort the whole ``sync``.
"""

from __future__ import annotations

import gzip
import socket
import ssl

import pytest

from arknights_mcp.sources import http_fetch
from arknights_mcp.sources.base import SourceAdapterError
from arknights_mcp.sources.http_fetch import (
    _TRANSIENT_ERRORS,
    HttpsFetcher,
    _backoff_delay,
    _gunzip_capped,
)

URL = "https://example.test/gamedata/x.json"


# --- pure helpers -------------------------------------------------------------


def test_backoff_delay_is_deterministic_exponential() -> None:
    assert _backoff_delay(1, 0.5) == 0.5
    assert _backoff_delay(2, 0.5) == 1.0
    assert _backoff_delay(3, 0.5) == 2.0
    # strictly increasing so a later retry always waits longer
    delays = [_backoff_delay(n, 0.5) for n in range(1, 6)]
    assert delays == sorted(delays) and len(set(delays)) == len(delays)


def test_transient_errors_cover_stalls_not_hard_faults() -> None:
    # A read/connect stall, a reset, and a dropped-body framing error are retryable.
    assert issubclass(socket.timeout, _TRANSIENT_ERRORS)  # socket.timeout is TimeoutError
    assert issubclass(ConnectionResetError, _TRANSIENT_ERRORS)
    assert issubclass(http_fetch.http.client.BadStatusLine, _TRANSIENT_ERRORS)
    # A genuine hard fault (DNS, TLS verification) is NOT -- retrying cannot help.
    assert not issubclass(socket.gaierror, _TRANSIENT_ERRORS)
    assert not issubclass(ssl.SSLCertVerificationError, _TRANSIENT_ERRORS)


def test_gunzip_capped_roundtrips_a_normal_body() -> None:
    payload = b'{"hello": "world"}' * 100
    out = _gunzip_capped(gzip.compress(payload), max_bytes=1 << 20, url=URL)
    assert out == payload


def test_gunzip_capped_rejects_a_zip_bomb() -> None:
    # ~500 KB of a single byte compresses to a few hundred bytes; the decompressed cap
    # must refuse it rather than inflate past the per-file cap (§V64).
    bomb = gzip.compress(b"A" * 500_000)
    assert len(bomb) < 1000
    with pytest.raises(SourceAdapterError, match="exceeds per-file cap"):
        _gunzip_capped(bomb, max_bytes=1000, url=URL)


def test_gunzip_capped_rejects_a_malformed_stream() -> None:
    with pytest.raises(SourceAdapterError, match="invalid gzip body"):
        _gunzip_capped(b"not actually gzip", max_bytes=1 << 20, url=URL)


# --- stub transport to drive the retry loop without live network --------------


class _Resp:
    """Minimal stand-in for :class:`http.client.HTTPResponse`."""

    def __init__(
        self, *, status: int = 200, body: bytes = b"", headers: dict[str, str] | None = None
    ):
        self.status = status
        self._body = body
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name.lower(), default)

    def read(self, amt: int | None = None) -> bytes:
        return self._body if amt is None else self._body[:amt]


class _Conn:
    """A stub connection that either raises on ``request`` or returns a canned response."""

    def __init__(self, *, resp: _Resp | None = None, raise_exc: BaseException | None = None):
        self._resp = resp
        self._raise = raise_exc
        self.closed = False

    def request(self, method: str, path: str, headers: dict[str, str] | None = None) -> None:
        if self._raise is not None:
            raise self._raise

    def getresponse(self) -> _Resp:
        assert self._resp is not None
        return self._resp

    def close(self) -> None:
        self.closed = True


class _StubFetcher(HttpsFetcher):
    """Hands out pre-seeded connections (always ``fresh``) instead of dialing out."""

    def __init__(self, conns: list[_Conn], **kw: object):
        super().__init__(**kw)  # type: ignore[arg-type]
        self._queue = list(conns)
        self.conn_calls = 0

    def _connection(self, host: str, port: int | None):  # type: ignore[override]
        self.conn_calls += 1
        return self._queue.pop(0), True


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture (and skip) the backoff sleeps so retries run instantly."""
    recorded: list[float] = []
    monkeypatch.setattr(http_fetch.time, "sleep", recorded.append)
    return recorded


def _ok(body: bytes = b"{}", encoding: str | None = None) -> _Conn:
    headers = {"Content-Encoding": encoding} if encoding else {}
    return _Conn(resp=_Resp(status=200, body=body, headers=headers))


# --- retry loop (the B55 fix) -------------------------------------------------


def test_transient_timeout_on_fresh_conn_is_retried_then_succeeds(no_sleep: list[float]) -> None:
    fetcher = _StubFetcher(
        [
            _Conn(raise_exc=TimeoutError("read timed out")),
            _Conn(raise_exc=TimeoutError()),
            _ok(b'{"ok":1}'),
        ],
        max_retries=2,
        backoff_base=0.5,
    )
    assert fetcher.fetch(URL, max_bytes=1024) == b'{"ok":1}'
    assert fetcher.conn_calls == 3  # initial + 2 retries
    assert no_sleep == [0.5, 1.0]  # exponential backoff between retries


def test_transient_timeout_fails_closed_after_budget_exhausted(no_sleep: list[float]) -> None:
    fetcher = _StubFetcher([_Conn(raise_exc=TimeoutError()) for _ in range(3)], max_retries=2)
    with pytest.raises(SourceAdapterError, match="after 2 retries"):
        fetcher.fetch(URL, max_bytes=1024)
    assert fetcher.conn_calls == 3


def test_non_transient_fault_fails_closed_without_retry(no_sleep: list[float]) -> None:
    fetcher = _StubFetcher(
        [_Conn(raise_exc=socket.gaierror("name resolution failed"))], max_retries=2
    )
    with pytest.raises(SourceAdapterError, match="network error fetching") as exc:
        fetcher.fetch(URL, max_bytes=1024)
    assert "retries" not in str(exc.value)
    assert fetcher.conn_calls == 1  # no retry burned on a hard fault
    assert no_sleep == []


# --- gzip decode + caps -------------------------------------------------------


def test_gzip_body_is_decompressed(no_sleep: list[float]) -> None:
    payload = b'{"amiya": true}'
    fetcher = _StubFetcher([_ok(gzip.compress(payload), encoding="gzip")])
    assert fetcher.fetch(URL, max_bytes=1 << 20) == payload


def test_identity_body_passes_through(no_sleep: list[float]) -> None:
    fetcher = _StubFetcher([_ok(b'{"raw": 1}')])
    assert fetcher.fetch(URL, max_bytes=1 << 20) == b'{"raw": 1}'


def test_unsupported_encoding_is_rejected(no_sleep: list[float]) -> None:
    fetcher = _StubFetcher([_ok(b"\x00\x01", encoding="br")])
    with pytest.raises(SourceAdapterError, match="unsupported Content-Encoding"):
        fetcher.fetch(URL, max_bytes=1 << 20)


def test_over_cap_identity_body_is_rejected(no_sleep: list[float]) -> None:
    fetcher = _StubFetcher([_ok(b"x" * 50)])
    with pytest.raises(SourceAdapterError, match="exceeds per-file cap"):
        fetcher.fetch(URL, max_bytes=10)
