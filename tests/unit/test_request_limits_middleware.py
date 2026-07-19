"""§T54/§V11: per-request body-size + timeout limits.

Drives :class:`~arknights_mcp.middleware.request_limits.RequestLimitsMiddleware`
with a raw ASGI ``(scope, receive, send)`` -- no socket -- asserting the two
per-request §V11 controls: an oversized body is refused ``413`` (declared or
streamed), a handler that stalls past the deadline yields ``504``, and the long-lived
``GET`` SSE stream is exempt from the timeout.
"""

from __future__ import annotations

from typing import Any

import anyio

from arknights_mcp.middleware.request_limits import RequestLimitsMiddleware


class _InnerApp:
    """Records that it ran and how many body bytes it drained; sends a 200."""

    def __init__(self, *, drain: bool = False) -> None:
        self.called = False
        self.drain = drain
        self.drained = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.called = True
        if self.drain:
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    break
                self.drained += len(message.get("body", b""))
                if not message.get("more_body", False):
                    break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _drive(
    app: RequestLimitsMiddleware,
    *,
    headers: list[tuple[bytes, bytes]],
    method: str = "POST",
    body_chunks: list[tuple[bytes, bool]] | None = None,
    scope_type: str = "http",
) -> int:
    scope: dict[str, Any] = {"type": scope_type, "method": method, "path": "/mcp"}
    if scope_type == "http":
        scope["headers"] = headers
    chunks = list(body_chunks or [(b"", False)])
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if chunks:
            body, more = chunks.pop(0)
            return {"type": "http.request", "body": body, "more_body": more}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    anyio.run(app.__call__, scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_declared_content_length_over_cap_rejected_413() -> None:
    inner = _InnerApp()
    app = RequestLimitsMiddleware(inner, max_request_bytes=100, timeout_seconds=30)
    status = _drive(app, headers=[(b"content-length", b"101")])
    assert status == 413
    assert inner.called is False  # refused before the handler ran


def test_within_cap_passes() -> None:
    inner = _InnerApp()
    app = RequestLimitsMiddleware(inner, max_request_bytes=100, timeout_seconds=30)
    status = _drive(app, headers=[(b"content-length", b"5")], body_chunks=[(b"hello", False)])
    assert status == 200
    assert inner.called is True


def test_streamed_body_over_cap_without_content_length_rejected_413() -> None:
    # A dishonest/absent Content-Length: the body streams past the cap and the
    # bounded receive fails it closed to 413 as the inner app drains it.
    inner = _InnerApp(drain=True)
    app = RequestLimitsMiddleware(inner, max_request_bytes=8, timeout_seconds=30)
    status = _drive(
        app,
        headers=[],
        body_chunks=[(b"aaaa", True), (b"bbbb", True), (b"cccc", False)],
    )
    assert status == 413


def test_slow_handler_times_out_504() -> None:
    class _SlowInner:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            # Never starts a response within the deadline.
            await anyio.sleep(30)
            await send({"type": "http.response.start", "status": 200, "headers": []})

    app = RequestLimitsMiddleware(_SlowInner(), max_request_bytes=100, timeout_seconds=0.05)
    status = _drive(app, headers=[(b"content-length", b"0")])
    assert status == 504


def test_get_stream_exempt_from_timeout() -> None:
    # The Streamable HTTP SSE stream rides on GET and stays open; the timeout must
    # not fire for it. A GET that would exceed the deadline still completes normally.
    class _SlowGet:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            await anyio.sleep(0.02)  # longer than the tiny timeout below
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"stream"})

    app = RequestLimitsMiddleware(_SlowGet(), max_request_bytes=100, timeout_seconds=0.001)
    status = _drive(app, headers=[], method="GET")
    assert status == 200


def test_non_http_scope_passes_through() -> None:
    inner = _InnerApp()
    app = RequestLimitsMiddleware(inner, max_request_bytes=0, timeout_seconds=0.001)
    _drive(app, headers=[], scope_type="lifespan")
    assert inner.called is True


def test_timeout_zero_disables_timeout() -> None:
    class _SlowInner:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            await anyio.sleep(0.02)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

    app = RequestLimitsMiddleware(_SlowInner(), max_request_bytes=100, timeout_seconds=0)
    status = _drive(app, headers=[(b"content-length", b"0")])
    assert status == 200
