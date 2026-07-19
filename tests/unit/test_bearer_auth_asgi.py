"""§T52/§V10/§V40: wire-level bearer enforcement in the Streamable HTTP transport.

Drives :class:`~arknights_mcp.transports.streamable_http._BearerAuthASGIApp` with a
stub verifier and a raw ASGI ``(scope, receive, send)`` -- no socket, no uvicorn --
asserting that a bad/absent token yields a typed ``401`` challenge, insufficient
scope a ``403`` with a ``scope=`` hint, a valid token reaches the inner app with the
principal attached, and non-``http`` scopes pass straight through.
"""

from __future__ import annotations

from typing import Any

import anyio

from arknights_mcp.auth.oidc import AuthError, OidcSettings
from arknights_mcp.auth.principal import Principal
from arknights_mcp.transports.streamable_http import _BearerAuthASGIApp

_SETTINGS = OidcSettings(
    issuer="https://issuer.example.com/",
    audience="arknights-mcp",
    jwks_url="https://issuer.example.com/jwks",
    required_scopes=("arknights:read",),
)

_PRINCIPAL = Principal(
    issuer="https://issuer.example.com/",
    subject="auth0|user123",
    client_id="client-abc",
    scopes=frozenset({"arknights:read"}),
)


class _StubVerifier:
    """Stand-in for OidcTokenVerifier: returns a principal or raises AuthError."""

    def __init__(self, *, principal: Principal | None = None, error: AuthError | None = None):
        self._principal = principal
        self._error = error

    def verify(self, token: str) -> Principal:
        if self._error is not None:
            raise self._error
        assert self._principal is not None
        return self._principal


class _InnerApp:
    """Records whether it ran and the principal handed to it."""

    def __init__(self) -> None:
        self.called = False
        self.principal: Principal | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.called = True
        self.principal = scope.get("state", {}).get("principal")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _drive(
    app: _BearerAuthASGIApp,
    *,
    headers: list[tuple[bytes, bytes]],
    scope_type: str = "http",
) -> list[dict[str, Any]]:
    scope: dict[str, Any] = {"type": scope_type, "method": "POST", "path": "/mcp"}
    if scope_type == "http":
        scope["headers"] = headers
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    anyio.run(app.__call__, scope, receive, send)
    return sent


def _status(sent: list[dict[str, Any]]) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _www_authenticate(sent: list[dict[str, Any]]) -> str:
    start = next(m for m in sent if m["type"] == "http.response.start")
    for name, value in start["headers"]:
        if name == b"www-authenticate":
            return value.decode("latin-1")
    return ""


def test_missing_bearer_rejected_401() -> None:
    inner = _InnerApp()
    app = _BearerAuthASGIApp(inner, _StubVerifier(principal=_PRINCIPAL), _SETTINGS)  # type: ignore[arg-type]
    sent = _drive(app, headers=[])
    assert _status(sent) == 401
    assert inner.called is False
    assert 'error="invalid_token"' in _www_authenticate(sent)


def test_non_bearer_scheme_rejected_401() -> None:
    inner = _InnerApp()
    app = _BearerAuthASGIApp(inner, _StubVerifier(principal=_PRINCIPAL), _SETTINGS)  # type: ignore[arg-type]
    sent = _drive(app, headers=[(b"authorization", b"Basic abc123")])
    assert _status(sent) == 401
    assert inner.called is False


def test_invalid_token_rejected_401() -> None:
    inner = _InnerApp()
    verifier = _StubVerifier(error=AuthError("invalid_token", 401, "token invalid"))
    app = _BearerAuthASGIApp(inner, verifier, _SETTINGS)  # type: ignore[arg-type]
    sent = _drive(app, headers=[(b"authorization", b"Bearer bad.token")])
    assert _status(sent) == 401
    assert inner.called is False


def test_insufficient_scope_rejected_403_with_scope_hint() -> None:
    inner = _InnerApp()
    verifier = _StubVerifier(
        error=AuthError("insufficient_scope", 403, "required scope not granted")
    )
    app = _BearerAuthASGIApp(inner, verifier, _SETTINGS)  # type: ignore[arg-type]
    sent = _drive(app, headers=[(b"authorization", b"Bearer scoped.out")])
    assert _status(sent) == 403
    challenge = _www_authenticate(sent)
    assert 'error="insufficient_scope"' in challenge
    assert 'scope="arknights:read"' in challenge
    assert inner.called is False


def test_valid_token_reaches_inner_with_principal() -> None:
    inner = _InnerApp()
    app = _BearerAuthASGIApp(inner, _StubVerifier(principal=_PRINCIPAL), _SETTINGS)  # type: ignore[arg-type]
    sent = _drive(app, headers=[(b"authorization", b"Bearer good.token")])
    assert _status(sent) == 200
    assert inner.called is True
    assert inner.principal is _PRINCIPAL


def test_challenge_body_carries_no_token() -> None:
    # §V12: the rejection response never echoes the presented bearer.
    inner = _InnerApp()
    verifier = _StubVerifier(error=AuthError("invalid_token", 401, "token invalid"))
    app = _BearerAuthASGIApp(inner, verifier, _SETTINGS)  # type: ignore[arg-type]
    sent = _drive(app, headers=[(b"authorization", b"Bearer secret.jwt.value")])
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert b"secret.jwt.value" not in body


def test_lifespan_scope_passes_through() -> None:
    # Non-http scopes (lifespan/websocket) must reach the inner app so the session
    # manager's task group still starts.
    inner = _InnerApp()
    app = _BearerAuthASGIApp(inner, _StubVerifier(principal=_PRINCIPAL), _SETTINGS)  # type: ignore[arg-type]
    _drive(app, headers=[], scope_type="lifespan")
    assert inner.called is True
