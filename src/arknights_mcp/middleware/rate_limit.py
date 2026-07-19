"""Per-principal rate + concurrency limits for the remote transport (§V11).

Two of the five §V11 remote controls live here (the other three -- request cap,
request timeout, response cap -- are in :mod:`.request_limits` and the §V22 envelope
builder):

* **Rate limit** -- a per-principal sliding-window log: at most
  ``requests_per_minute`` served in any trailing 60s. Exceeding it yields ``429``
  with a ``Retry-After`` hint. Only *served* requests count against the window; a
  request rejected here (rate or concurrency) is not logged into it.
* **Concurrency limit** -- at most ``max_concurrent`` requests in flight per
  principal at once; the ``+1`` gets ``429`` until an in-flight request finishes.

Both key on :attr:`~arknights_mcp.auth.principal.Principal.principal_id` (``iss|sub``,
the one home for the namespacing -- §V37), read from the request scope via
:func:`~arknights_mcp.middleware._shared.principal_id_of`. The full stack is wired
only on the auth-requiring remote path (§V40), so every http request reaching this
layer carries a validated principal; a request with none buckets under
``ANONYMOUS_PRINCIPAL`` (fail-closed: shared, never unlimited).

Atomicity: the ASGI app runs on a single event loop, and the check-then-reserve
(purge window, test limits, append timestamp, ``+1`` in-flight) runs with no
intervening ``await`` -- so no two requests interleave between the test and the
reservation. The in-flight count is released in a ``finally`` around the inner app.

Pre-auth flood protection (unauthenticated request storms, which never reach a
per-principal bucket) is the reverse proxy's job (§I.api; the §T55 nginx example),
not this app-layer control.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from math import ceil
from time import monotonic

from starlette.types import ASGIApp, Receive, Scope, Send

from arknights_mcp.middleware._shared import principal_id_of, send_error

#: Trailing window the rate limit is measured over (seconds).
_WINDOW_SECONDS = 60.0


class RateLimitMiddleware:
    """ASGI middleware enforcing per-principal rate + concurrency limits (§V11).

    :param app: the wrapped ASGI app (the next layer inward).
    :param requests_per_minute: max requests served per principal per trailing 60s.
    :param max_concurrent: max in-flight requests per principal at any instant.
    :param clock: monotonic time source (injectable for deterministic tests).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        requests_per_minute: int,
        max_concurrent: int,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._app = app
        self._rpm = requests_per_minute
        self._max_concurrent = max_concurrent
        self._clock = clock
        # Per-principal state; empty entries are garbage-collected so a churn of
        # distinct principals does not grow these unbounded.
        self._window: dict[str, deque[float]] = defaultdict(deque)
        self._inflight: dict[str, int] = defaultdict(int)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # lifespan / websocket: not a rate-limited request unit.
            await self._app(scope, receive, send)
            return

        principal_id = principal_id_of(scope)
        # --- reserve a slot: single synchronous critical section (no await) ---
        now = self._clock()
        window = self._window[principal_id]
        cutoff = now - _WINDOW_SECONDS
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self._rpm:
            retry_after = max(1, ceil(window[0] + _WINDOW_SECONDS - now))
            await send_error(
                send,
                429,
                "rate_limited",
                "per-principal request rate exceeded",
                extra_headers=[(b"retry-after", str(retry_after).encode("ascii"))],
            )
            self._gc(principal_id)
            return
        if self._inflight[principal_id] >= self._max_concurrent:
            await send_error(
                send,
                429,
                "concurrency_limited",
                "per-principal concurrent request limit exceeded",
                extra_headers=[(b"retry-after", b"1")],
            )
            self._gc(principal_id)
            return
        # Both limits satisfied: count this request and mark it in flight.
        window.append(now)
        self._inflight[principal_id] += 1
        # --- end critical section ---

        try:
            await self._app(scope, receive, send)
        finally:
            self._inflight[principal_id] -= 1
            self._gc(principal_id)

    def _gc(self, principal_id: str) -> None:
        """Drop a principal's state once its window is empty and nothing in flight."""
        if not self._window.get(principal_id) and self._inflight.get(principal_id, 0) <= 0:
            self._window.pop(principal_id, None)
            self._inflight.pop(principal_id, None)
