"""§T54/§V11/§V12/§V10: the composed remote middleware stack.

:func:`~arknights_mcp.transports.streamable_http.wrap_remote_app` layers redacted
logging + bearer validation + per-principal rate/concurrency + per-request
size/timeout around the session-manager app. These tests prove the composition,
not the individual layers (each has its own unit test): a valid request threads
every layer to the inner app, and a request rejected at an outer layer is still
observed by the access log (logging is outermost).
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from arknights_mcp.auth.oidc import AuthError, OidcSettings
from arknights_mcp.auth.principal import Principal
from arknights_mcp.config import AppConfig, LimitsConfig
from arknights_mcp.transports.streamable_http import wrap_remote_app

_SETTINGS = OidcSettings(
    issuer="https://issuer.example.com/",
    audience="arknights-mcp",
    jwks_url="https://issuer.example.com/jwks",
    required_scopes=("arknights:read",),
)
_PRINCIPAL = Principal(
    issuer="https://issuer.example.com/",
    subject="auth0|alice",
    client_id="client-a",
    scopes=frozenset({"arknights:read"}),
)


class _StubVerifier:
    def __init__(self, *, principal: Principal | None = None, error: AuthError | None = None):
        self._principal = principal
        self._error = error

    def verify(self, token: str) -> Principal:
        if self._error is not None:
            raise self._error
        assert self._principal is not None
        return self._principal


class _InnerApp:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _config(**limits: Any) -> AppConfig:
    return AppConfig(limits=LimitsConfig(**limits))


def _drive(app: Any, *, token: str | None) -> int:
    headers: list[tuple[bytes, bytes]] = [(b"content-length", b"0")]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode("latin-1")))
    scope: dict[str, Any] = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    anyio.run(app.__call__, scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_valid_request_threads_every_layer() -> None:
    inner = _InnerApp()
    app = wrap_remote_app(inner, _config(), _StubVerifier(principal=_PRINCIPAL), _SETTINGS)
    assert _drive(app, token="good.token") == 200
    assert inner.calls == 1


def test_bad_token_rejected_before_inner() -> None:
    inner = _InnerApp()
    verifier = _StubVerifier(error=AuthError("invalid_token", 401, "token invalid"))
    app = wrap_remote_app(inner, _config(), verifier, _SETTINGS)
    assert _drive(app, token="bad.token") == 401
    assert inner.calls == 0


def test_rate_limit_applies_after_auth() -> None:
    # Auth passes; the per-principal rate cap (1/min) then refuses the 2nd request.
    inner = _InnerApp()
    app = wrap_remote_app(
        inner,
        _config(requests_per_minute_per_principal=1),
        _StubVerifier(principal=_PRINCIPAL),
        _SETTINGS,
    )
    assert _drive(app, token="good.token") == 200
    assert _drive(app, token="good.token") == 429
    assert inner.calls == 1  # the rate-limited request never reached the inner app


def test_oversized_request_rejected_after_auth() -> None:
    inner = _InnerApp()
    app = wrap_remote_app(
        inner,
        _config(max_request_bytes=4),
        _StubVerifier(principal=_PRINCIPAL),
        _SETTINGS,
    )
    headers = [
        (b"content-length", b"100"),
        (b"authorization", b"Bearer good.token"),
    ]
    scope: dict[str, Any] = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    anyio.run(app.__call__, scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 413
    assert inner.calls == 0


def test_rejected_request_is_still_logged(caplog: pytest.LogCaptureFixture) -> None:
    # §V12 + composition: logging is the OUTERMOST layer, so a 401 from the bearer
    # challenge is still recorded (as anonymous -- no principal was attached).
    import logging

    inner = _InnerApp()
    verifier = _StubVerifier(error=AuthError("invalid_token", 401, "token invalid"))
    app = wrap_remote_app(inner, _config(), verifier, _SETTINGS)
    with caplog.at_level(logging.INFO, logger="arknights_mcp.access"):
        assert _drive(app, token="bad.token") == 401
    records = [r for r in caplog.records if r.name == "arknights_mcp.access"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "401" in message
    assert "principal=anonymous" in message
    # The presented token never reaches the log (§V12).
    assert "bad.token" not in message
