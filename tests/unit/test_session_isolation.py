"""§T53/§V14/§V10: principal/session isolation — no cross-user session leak.

The Streamable HTTP transport runs the SDK session manager *stateful*, so it keeps
one persistent MCP session per ``Mcp-Session-Id``. Isolation requires each session
be bound to the principal that created it and refuse reuse by anyone else. That
binding is driven by ``scope["user"]``; these tests prove:

* :func:`_session_user` projects a :class:`Principal` onto an owner identity keyed
  on ``principal_id`` (``iss|sub``) -- never the OAuth client (``azp``) -- and never
  carries the raw bearer (§V12);
* :class:`_BearerAuthASGIApp` attaches that user on a validated request;
* the SDK's stateful handler, given our user, rejects a request that presents a
  session id owned by a *different* principal (404) while letting the owning
  principal reuse it -- the whole isolation surface, since the shared read-only core
  holds no other per-principal state (§V14).
"""

from __future__ import annotations

from typing import Any

import anyio
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, authorization_context
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from arknights_mcp.app import build_application
from arknights_mcp.auth.oidc import OidcSettings
from arknights_mcp.auth.principal import Principal
from arknights_mcp.config import AppConfig
from arknights_mcp.transports._server import build_server
from arknights_mcp.transports.streamable_http import (
    _REDACTED_SESSION_TOKEN,
    _BearerAuthASGIApp,
    _session_user,
)

_SETTINGS = OidcSettings(
    issuer="https://issuer.example.com/",
    audience="arknights-mcp",
    jwks_url="https://issuer.example.com/jwks",
    required_scopes=("arknights:read",),
)


def _principal(*, issuer: str, subject: str, client_id: str, scopes: frozenset[str]) -> Principal:
    return Principal(issuer=issuer, subject=subject, client_id=client_id, scopes=scopes)


_ALICE = _principal(
    issuer="https://issuer.example.com/",
    subject="auth0|alice",
    client_id="client-a",
    scopes=frozenset({"arknights:read"}),
)
# Same iss|sub as Alice but a *different* OAuth client (azp) + scope set: still the
# same principal (§V10 keys identity on iss|sub, not the client).
_ALICE_OTHER_CLIENT = _principal(
    issuer="https://issuer.example.com/",
    subject="auth0|alice",
    client_id="client-b",
    scopes=frozenset({"arknights:read", "arknights:extra"}),
)
_BOB = _principal(
    issuer="https://issuer.example.com/",
    subject="auth0|bob",
    client_id="client-a",
    scopes=frozenset({"arknights:read"}),
)
# Same subject string as Alice but a *different* issuer -- ``sub`` is unique only
# per issuer, so this is a distinct principal (§V10).
_ALICE_OTHER_ISSUER = _principal(
    issuer="https://other-issuer.example.com/",
    subject="auth0|alice",
    client_id="client-a",
    scopes=frozenset({"arknights:read"}),
)


def _owner(principal: Principal) -> Any:
    return authorization_context(_session_user(principal))


def test_session_user_keys_on_principal_id_not_client() -> None:
    # §V10/§T53: two clients (azp) acting for the same iss|sub are the same
    # principal, so their session-owner keys are equal -- client_id must not split
    # the identity.
    assert _owner(_ALICE) == _owner(_ALICE_OTHER_CLIENT)
    assert _session_user(_ALICE).username == _ALICE.principal_id
    assert _ALICE.principal_id == _ALICE_OTHER_CLIENT.principal_id


def test_session_user_separates_distinct_principals() -> None:
    # §T53: a different subject or a different issuer is a different principal ->
    # different owner key -> no cross-user session reuse.
    assert _owner(_ALICE) != _owner(_BOB)
    assert _owner(_ALICE) != _owner(_ALICE_OTHER_ISSUER)


def test_session_user_never_carries_raw_bearer() -> None:
    # §V12: the owner AccessToken holds a redaction placeholder, never a credential.
    user = _session_user(_ALICE)
    assert user.access_token.token == _REDACTED_SESSION_TOKEN
    assert user.access_token.token != "the.real.jwt"


class _StubVerifier:
    def __init__(self, principal: Principal) -> None:
        self._principal = principal

    def verify(self, token: str) -> Principal:
        return self._principal


class _InnerApp:
    def __init__(self) -> None:
        self.user: Any = None
        self.principal: Principal | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.user = scope.get("user")
        self.principal = scope.get("state", {}).get("principal")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def test_bearer_auth_attaches_session_user_keyed_on_principal() -> None:
    # §T53: a validated request reaches the inner app carrying an AuthenticatedUser
    # whose owner key equals the principal's -- and still the Principal on state.
    inner = _InnerApp()
    app = _BearerAuthASGIApp(inner, _StubVerifier(_ALICE), _SETTINGS)  # type: ignore[arg-type]
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"authorization", b"Bearer good.token")],
    }
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    anyio.run(app.__call__, scope, receive, send)

    assert isinstance(inner.user, AuthenticatedUser)
    assert authorization_context(inner.user) == _owner(_ALICE)
    assert inner.principal is _ALICE


class _DummyTransport:
    """Stands in for a live session transport; records whether it was dispatched."""

    def __init__(self) -> None:
        self.idle_scope = None
        self.handled = False

    async def handle_request(self, scope: Any, receive: Any, send: Any) -> None:
        self.handled = True


def _build_manager() -> StreamableHTTPSessionManager:
    core = build_application(AppConfig())
    return StreamableHTTPSessionManager(app=build_server(core))


def _drive_session_request(
    manager: StreamableHTTPSessionManager,
    *,
    session_id: str,
    user: AuthenticatedUser,
) -> tuple[int, bytes]:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"mcp-session-id", session_id.encode())],
        "user": user,
    }
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    anyio.run(manager._handle_stateful_request, scope, receive, send)

    status = next(
        (m["status"] for m in sent if m["type"] == "http.response.start"),
        0,
    )
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


def test_stateful_session_rejects_cross_principal_and_allows_owner() -> None:
    # §T53/§V14: a session created by Alice cannot be resumed by Bob -- the SDK
    # owner-binding, fed our principal-keyed user, returns 404 "Session not found"
    # without touching the session transport; Alice (any client of hers) may reuse
    # it. This is the concrete no-cross-user-cache-leak proof.
    manager = _build_manager()
    session_id = "alice-session-0001"
    transport = _DummyTransport()
    manager._server_instances[session_id] = transport  # type: ignore[assignment]
    manager._session_owners[session_id] = _owner(_ALICE)

    # Bob presents Alice's session id: rejected, transport never dispatched.
    status, body = _drive_session_request(manager, session_id=session_id, user=_session_user(_BOB))
    assert status == 404
    assert b"Session not found" in body
    assert transport.handled is False

    # Alice -- even from a different OAuth client -- owns the session and may reuse it.
    status, _ = _drive_session_request(
        manager, session_id=session_id, user=_session_user(_ALICE_OTHER_CLIENT)
    )
    assert status == 0  # dummy transport sends nothing; no 404 was emitted
    assert transport.handled is True
