"""Per-request size + timeout limits for the remote transport (§V11).

Two more of the five §V11 remote controls (rate + concurrency live in
:mod:`.rate_limit`; the response cap is the §V22 envelope builder's job -- see below):

* **Request cap** -- a declared ``Content-Length`` over ``max_request_bytes`` is
  refused ``413`` up front, before the inner app runs. A request that omits (or lies
  about) its length is still bounded as its body streams: a wrapped ``receive`` stops
  reading and fails closed the moment the cumulative bytes cross the cap, so an
  oversized chunked body can never be buffered whole -- the *memory* bound holds
  whether or not the client is honest. NB on the *status*: the SDK reads the POST body
  (``await request.body()``) inside its own ``try/except`` and converts a mid-read
  overflow to its generic ``500`` before it can propagate to this middleware, so on the
  current SDK only the declared-length path yields a typed ``413``. The
  ``except _RequestTooLarge`` below is kept as a defensive fallback for a composition
  where the overflow does reach us (and it is what the unit test exercises).
* **Request timeout** -- a non-GET ``http`` request that has not begun its response
  within ``timeout_seconds`` is abandoned and answered ``504``. ``GET`` is exempt:
  the Streamable HTTP protocol serves the long-lived server->client SSE stream over
  ``GET`` (SDK ``handle_request``), which is meant to stay open.

Response cap: NOT re-buffered here. Every tool result is measured against the
200 KB cap at the §V22 envelope builder (worst-case ``ensure_ascii=True`` bytes,
which upper-bound the wire encoding -- the B21 fix), so the bound already holds on
the remote wire. Buffering the response body in this middleware to re-check it would
break the SSE stream and duplicate a guarantee the one home already provides (§V37).

The timeout uses :func:`anyio.move_on_after`; if it fires *after* the inner app has
already started its response (``http.response.start`` sent), we can no longer inject a
504 (headers are on the wire) -- the cancellation still tears the request down. The
504 (and likewise the streamed-overflow 413) is only synthesized when nothing was
sent yet, which is the case these controls target: a handler wedged, or a body
over-running, before any output.
"""

from __future__ import annotations

import anyio
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from arknights_mcp.middleware._shared import send_error


class RequestLimitsMiddleware:
    """ASGI middleware enforcing per-request body-size + timeout limits (§V11).

    :param app: the wrapped ASGI app (the next layer inward).
    :param max_request_bytes: max request body size; larger → ``413``.
    :param timeout_seconds: max time before a response starts; exceeded → ``504``
        (non-GET only). ``0`` or negative disables the timeout.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_request_bytes: int,
        timeout_seconds: float,
    ) -> None:
        self._app = app
        self._max_request_bytes = max_request_bytes
        self._timeout = timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        # Refuse an over-cap declared Content-Length before reading a single byte.
        declared = _declared_content_length(scope)
        if declared is not None and declared > self._max_request_bytes:
            await send_error(send, 413, "request_too_large", "request body exceeds limit")
            return

        bounded = _BoundedReceive(receive, self._max_request_bytes)
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        method = scope.get("method", "").upper()
        timed = self._timeout > 0 and method != "GET"
        try:
            if timed:
                with anyio.move_on_after(self._timeout) as cancel_scope:
                    await self._app(scope, bounded, tracking_send)
                if cancel_scope.cancelled_caught and not started:
                    # Timed out before any bytes went out: synthesize a 504. If the
                    # response had already started, the headers are committed and we
                    # can only let the cancellation tear the request down.
                    await send_error(send, 504, "request_timeout", "request processing timed out")
            else:
                # GET carries the long-lived SSE stream; never time it out.
                await self._app(scope, bounded, tracking_send)
        except _RequestTooLarge:
            # A missing/dishonest Content-Length let an over-cap body stream in. If
            # the inner app has not yet committed a response, fail it closed to 413.
            if not started:
                await send_error(send, 413, "request_too_large", "request body exceeds limit")


def _declared_content_length(scope: Scope) -> int | None:
    """Parse a non-negative ``Content-Length`` header, else ``None``."""
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                length = int(value.decode("latin-1").strip())
            except ValueError:
                return None
            return length if length >= 0 else None
    return None


class _RequestTooLarge(Exception):
    """Raised inside a wrapped ``receive`` when the streamed body crosses the cap.

    Caught at the :class:`RequestLimitsMiddleware` boundary and converted to a
    ``413`` -- the inner app never has to know its body read was truncated.
    """


class _BoundedReceive:
    """Wrap ``receive`` to fail closed once cumulative body bytes exceed the cap.

    Guards against a missing or dishonest ``Content-Length``: each ``http.request``
    chunk is counted, and crossing the cap raises :class:`_RequestTooLarge`. Non-body
    messages (e.g. ``http.disconnect``) pass through untouched.
    """

    def __init__(self, receive: Receive, max_bytes: int) -> None:
        self._receive = receive
        self._max_bytes = max_bytes
        self._seen = 0

    async def __call__(self) -> Message:
        message = await self._receive()
        if message["type"] == "http.request":
            self._seen += len(message.get("body", b""))
            if self._seen > self._max_bytes:
                raise _RequestTooLarge
        return message
