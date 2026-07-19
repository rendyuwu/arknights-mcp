"""§T54/§V11: per-principal rate + concurrency limits.

Drives :class:`~arknights_mcp.middleware.rate_limit.RateLimitMiddleware` with a raw
ASGI ``(scope, receive, send)`` and an injected clock -- no socket, no real time --
asserting the two per-principal §V11 controls: a trailing-60s rate window and an
in-flight concurrency cap, each keyed on the principal id (``iss|sub``) and each
independent across principals.
"""

from __future__ import annotations

from typing import Any

import anyio

from arknights_mcp.auth.principal import Principal
from arknights_mcp.middleware.rate_limit import RateLimitMiddleware

_ALICE = Principal(
    issuer="https://issuer.example.com/",
    subject="auth0|alice",
    client_id="client-a",
    scopes=frozenset({"arknights:read"}),
)
_BOB = Principal(
    issuer="https://issuer.example.com/",
    subject="auth0|bob",
    client_id="client-a",
    scopes=frozenset({"arknights:read"}),
)


class _Clock:
    """A hand-advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _InnerApp:
    """Counts how many times it ran; sends a 200."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _scope(principal: Principal | None) -> dict[str, Any]:
    scope: dict[str, Any] = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}
    if principal is not None:
        scope["state"] = {"principal": principal}
    return scope


def _drive(app: RateLimitMiddleware, scope: dict[str, Any]) -> int:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    anyio.run(app.__call__, scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_rate_limit_allows_up_to_rpm_then_429() -> None:
    clock = _Clock()
    inner = _InnerApp()
    app = RateLimitMiddleware(inner, requests_per_minute=3, max_concurrent=10, clock=clock)
    # First 3 within the window pass; the 4th is refused.
    assert [_drive(app, _scope(_ALICE)) for _ in range(3)] == [200, 200, 200]
    assert _drive(app, _scope(_ALICE)) == 429
    assert inner.calls == 3  # the rejected request never reached the inner app


def test_rate_limit_window_slides() -> None:
    clock = _Clock()
    inner = _InnerApp()
    app = RateLimitMiddleware(inner, requests_per_minute=2, max_concurrent=10, clock=clock)
    assert _drive(app, _scope(_ALICE)) == 200
    assert _drive(app, _scope(_ALICE)) == 200
    assert _drive(app, _scope(_ALICE)) == 429
    # Advance past the 60s window: the earlier timestamps age out, budget refreshes.
    clock.now += 61.0
    assert _drive(app, _scope(_ALICE)) == 200


def test_rate_limit_is_per_principal() -> None:
    clock = _Clock()
    inner = _InnerApp()
    app = RateLimitMiddleware(inner, requests_per_minute=1, max_concurrent=10, clock=clock)
    assert _drive(app, _scope(_ALICE)) == 200
    assert _drive(app, _scope(_ALICE)) == 429
    # Bob has his own bucket -- Alice exhausting hers must not spend Bob's.
    assert _drive(app, _scope(_BOB)) == 200


def test_retry_after_header_on_rate_reject() -> None:
    clock = _Clock()
    app = RateLimitMiddleware(_InnerApp(), requests_per_minute=1, max_concurrent=10, clock=clock)
    _drive(app, _scope(_ALICE))
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    anyio.run(app.__call__, _scope(_ALICE), receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 429
    headers = dict(start["headers"])
    assert b"retry-after" in headers
    # Within 60s of the only logged request, the hint is a positive second count.
    assert 1 <= int(headers[b"retry-after"]) <= 60


def test_concurrency_limit_blocks_excess_then_releases() -> None:
    # A slow inner app lets us hold requests in flight concurrently. With a cap of 2,
    # the 3rd concurrent request is refused 429; once the in-flight ones finish, a new
    # request is admitted again (the slot was released in the finally).
    clock = _Clock()
    release = anyio.Event()
    started = 0
    rejected: list[int] = []

    class _SlowInner:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            nonlocal started
            started += 1
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

    app = RateLimitMiddleware(_SlowInner(), requests_per_minute=100, max_concurrent=2, clock=clock)

    async def one(principal: Principal, hold: bool) -> None:
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await app(_scope(principal), receive, send)
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        if status == 429:
            rejected.append(status)

    async def scenario() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(one, _ALICE, True)  # holds slot 1
            tg.start_soon(one, _ALICE, True)  # holds slot 2
            await _yield_until(lambda: started >= 2)
            # Both slots held: a 3rd concurrent request is refused.
            await one(_ALICE, False)
            assert rejected == [429]
            release.set()  # let the two in-flight finish, releasing both slots

        # After release, both slots are free -- a fresh request is admitted.
        await one(_ALICE, False)
        assert rejected == [429]  # no new rejection

    anyio.run(scenario)


async def _yield_until(pred: Any) -> None:
    for _ in range(1000):
        if pred():
            return
        await anyio.sleep(0)
    raise AssertionError("condition not reached")


def test_non_http_scope_passes_through() -> None:
    inner = _InnerApp()
    app = RateLimitMiddleware(inner, requests_per_minute=0, max_concurrent=0)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "lifespan.startup"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    # rpm=0 would refuse any http request, but a lifespan scope is not a request unit
    # and must reach the inner app so the session manager's task group can start.
    anyio.run(app.__call__, {"type": "lifespan"}, receive, send)
    assert inner.calls == 1
