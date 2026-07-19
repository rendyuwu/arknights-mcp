"""Private Streamable HTTP transport (§T51/M6; §V14; §I.api).

Serves the *same* shared core both transports use over the MCP Streamable HTTP
wire: a single ``POST /mcp`` ASGI endpoint (§I.api) driven by the SDK's
:class:`~mcp.server.streamable_http_manager.StreamableHTTPSessionManager`. The
session manager wraps :func:`arknights_mcp.transports._server.build_server`, the
one transport-agnostic server (§V14/§V37) -- so ``tools/list`` / ``tools/call``
dispatch the identical registry + handlers as ``stdio``; there is no second query
path to drift.

Bearer validation lands here in §T52: when the deployment requires auth (a
non-loopback bind, or a loopback bind declared ``behind_proxy`` -- §V40), the ASGI
app is wrapped in :class:`_BearerAuthASGIApp`, which enforces the §V10
resource-server checks on every ``/mcp`` request and issues typed ``401``/``403``
``WWW-Authenticate`` challenges. A genuine loopback dev bind (not behind a proxy)
stays authless -- the explicit §V9 exception.

Principal/session isolation lands in §T53: the shared session manager runs
*stateful* (``stateless=False``), so it keeps one persistent MCP session per
``Mcp-Session-Id`` and -- crucially -- binds each session to the credential that
created it, rejecting any request that presents a session id owned by a different
credential (SDK ``StreamableHTTPSessionManager._handle_stateful_request``). That
owner-binding only activates when the request carries a validated
``scope["user"]``; :class:`_BearerAuthASGIApp` therefore attaches an
:class:`~mcp.server.auth.middleware.bearer_auth.AuthenticatedUser` keyed on
:attr:`~arknights_mcp.auth.principal.Principal.principal_id` (§V10 ``iss|sub``, the
one home for the namespacing -- §V37). Without it every session's owner is ``None``
and any validated principal could resume any other's session -- a cross-user leak.
The shared read-only core carries no other per-principal state (§V14: same DB +
same input → identical result ∀ caller), so the session binding is the whole
isolation surface. Redacted logging remains a separate M6 task (§T54). The intended
production shape is loopback ``127.0.0.1`` behind a TLS-terminating reverse proxy
(§I.api; §T55).
"""

from __future__ import annotations

import json

import anyio
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from arknights_mcp.app import ApplicationCore
from arknights_mcp.auth.oidc import AuthError, OidcSettings, OidcTokenVerifier
from arknights_mcp.auth.principal import Principal
from arknights_mcp.config import AppConfig
from arknights_mcp.middleware import (
    RateLimitMiddleware,
    RedactedLoggingMiddleware,
    RequestLimitsMiddleware,
)
from arknights_mcp.transports._server import build_server

#: Placeholder carried in the session-owner :class:`AccessToken` in place of the
#: real bearer. The owner-binding only needs the identity components, and §V12
#: forbids stashing the raw token anywhere it could be logged; the SDK's
#: ``authorization_context`` never reads this field.
_REDACTED_SESSION_TOKEN = "[redacted]"


class _SessionManagerASGIApp:
    """ASGI endpoint forwarding every request to the shared session manager.

    A raw ASGI app (not a request/response function) so Starlette hands the SDK
    the unbuffered ``(scope, receive, send)`` it needs for the Streamable HTTP
    protocol (POST request bodies, SSE / JSON responses, GET stream, DELETE
    teardown all on one route).
    """

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self._manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._manager.handle_request(scope, receive, send)


def build_asgi_app(
    core: ApplicationCore,
    *,
    path: str = "/mcp",
    json_response: bool = False,
    stateless: bool = False,
) -> Starlette:
    """Build the Streamable HTTP ASGI app for ``core`` (§V14; §I.api).

    The MCP server is :func:`build_server` -- the same one ``stdio`` runs -- so the
    two transports share one registry + one set of handlers (§V14). The returned
    Starlette app routes ``path`` (default ``/mcp``, §I.api) to the session manager
    and runs the manager's task group for the app's lifespan. The manager is also
    stashed on ``app.state.session_manager`` so the shared-server reuse is
    inspectable without opening a socket.
    """
    manager = StreamableHTTPSessionManager(
        app=build_server(core),
        json_response=json_response,
        stateless=stateless,
    )
    endpoint = _SessionManagerASGIApp(manager)
    app = Starlette(
        routes=[Route(path, endpoint=endpoint)],
        # The manager's task group must be entered before any request is handled
        # (``handle_request`` raises otherwise); tie its lifetime to the app's.
        # ``manager.run()`` is the SDK's own async context manager.
        lifespan=lambda _app: manager.run(),
    )
    app.state.session_manager = manager
    return app


def _bearer_token(scope: Scope) -> str | None:
    """Extract the ``Authorization: Bearer <token>`` credential, else ``None``.

    Case-insensitive scheme match per RFC 6750; a header without the ``Bearer``
    scheme or an empty token yields ``None`` (treated as missing credentials).
    """
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            decoded = value.decode("latin-1")
            if decoded[:7].lower() == "bearer ":
                token = decoded[7:].strip()
                return token or None
            return None
    return None


def _session_user(principal: Principal) -> AuthenticatedUser:
    """Project ``principal`` into the SDK's session-owner identity (§T53/§V10).

    The stateful :class:`StreamableHTTPSessionManager` binds each MCP session to
    ``authorization_context(scope["user"])`` -- a ``(client_id, iss, sub)`` tuple --
    and rejects a request whose credential does not match the session's owner. We
    want that owner key to be *exactly* the principal identity §V10 defines:
    ``iss|sub`` (:attr:`Principal.principal_id`), never the OAuth client (``azp``) --
    two clients acting for the same subject are the same principal, and a leak
    across *different* principals must be impossible. So we carry ``principal_id`` in
    the ``client_id`` slot and leave ``iss``/``sub`` unset: the owner tuple collapses
    to ``(principal_id, None, None)``, keyed solely on the one-home namespacing
    (§V37). The real bearer is never stored here (§V12) -- ``authorization_context``
    ignores the token field.
    """
    return AuthenticatedUser(
        AccessToken(
            token=_REDACTED_SESSION_TOKEN,
            client_id=principal.principal_id,
            scopes=sorted(principal.scopes),
        )
    )


class _BearerAuthASGIApp:
    """ASGI middleware enforcing §V10 bearer validation on every HTTP request.

    Wraps the Streamable HTTP app: an ``http`` request must carry a bearer token
    that :class:`~arknights_mcp.auth.oidc.OidcTokenVerifier` validates, else the
    request is rejected with a typed ``WWW-Authenticate`` challenge (401 for a
    bad/absent token, 403 for insufficient scope) and the inner app is never
    reached. Non-``http`` scopes (``lifespan``, ``websocket``) pass straight through
    so the session manager's task group still starts.

    On success the validated :class:`~arknights_mcp.auth.principal.Principal` is
    stashed on ``scope["state"]["principal"]`` (for §T54 per-principal limits +
    redacted logging), and an :class:`AuthenticatedUser` keyed on the principal is
    placed on ``scope["user"]`` so the session manager binds each MCP session to its
    creator and refuses cross-principal session reuse (§T53 isolation; §V10). Absent
    ``scope["user"]`` the SDK would own every session as ``None`` -- any validated
    caller could then resume any other's session.
    """

    def __init__(
        self,
        app: ASGIApp,
        verifier: OidcTokenVerifier,
        settings: OidcSettings,
    ) -> None:
        self._app = app
        self._verifier = verifier
        self._scope_challenge = " ".join(settings.required_scopes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        token = _bearer_token(scope)
        if token is None:
            await self._reject(send, 401, "invalid_token", "bearer token required")
            return
        try:
            principal = await anyio.to_thread.run_sync(self._verifier.verify, token)
        except AuthError as exc:
            await self._reject(send, exc.status, exc.error, exc.description)
            return
        except Exception:
            # Fail closed on any unexpected verifier fault; never leak details (§V12).
            await self._reject(send, 401, "invalid_token", "token validation failed")
            return
        # Attach the validated identity (§T54 limits/logging) and the session-owner
        # user so the SDK binds this session to its creator + refuses cross-principal
        # reuse (§T53 isolation; §V10). Both are set only after validation.
        scope["state"] = {**scope.get("state", {}), "principal": principal}
        scope["user"] = _session_user(principal)
        await self._app(scope, receive, send)

    async def _reject(self, send: Send, status: int, error: str, description: str) -> None:
        """Emit an RFC 6750 ``WWW-Authenticate`` challenge; no token/secret (§V12)."""
        params = [f'error="{error}"', f'error_description="{description}"']
        if error == "insufficient_scope" and self._scope_challenge:
            params.append(f'scope="{self._scope_challenge}"')
        challenge = "Bearer " + ", ".join(params)
        body = json.dumps({"error": error, "error_description": description}).encode("utf-8")
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"www-authenticate", challenge.encode("latin-1")),
        ]
        start: Message = {"type": "http.response.start", "status": status, "headers": headers}
        await send(start)
        await send({"type": "http.response.body", "body": body})


def wrap_remote_app(
    inner: ASGIApp,
    config: AppConfig,
    verifier: OidcTokenVerifier,
    settings: OidcSettings,
) -> ASGIApp:
    """Wrap the Streamable HTTP app in the auth-requiring remote middleware stack.

    Composition, outermost → innermost (§T54; §V11/§V12/§V10):

    ``RedactedLoggingMiddleware`` → ``_BearerAuthASGIApp`` → ``RateLimitMiddleware`` →
    ``RequestLimitsMiddleware`` → ``inner`` (the session-manager app).

    The order is load-bearing:

    * **Logging outermost** so it records *every* request's real outcome, including a
      ``401`` bearer challenge or a ``429`` limiter rejection, and so it can read the
      validated principal the bearer layer stashes on the scope once the inner stack
      returns (§V12).
    * **Bearer next** so the per-principal limits below it always see a validated
      :class:`~arknights_mcp.auth.principal.Principal` on the scope; a request that
      fails auth is rejected before any limiter bucket is touched.
    * **Rate/concurrency then request cap/timeout inside** so the §V11 controls wrap
      the actual handler: the concurrency slot is held for the request's whole
      lifetime, and the timeout bounds the handler's own work.

    Pre-auth flood protection (unauthenticated request storms that never reach a
    per-principal bucket) is the reverse proxy's job (§I.api; the §T55 nginx example).
    """
    limits = config.limits
    app: ASGIApp = RequestLimitsMiddleware(
        inner,
        max_request_bytes=limits.max_request_bytes,
        timeout_seconds=limits.request_timeout_seconds,
    )
    app = RateLimitMiddleware(
        app,
        requests_per_minute=limits.requests_per_minute_per_principal,
        max_concurrent=limits.max_concurrent_requests_per_principal,
    )
    app = _BearerAuthASGIApp(app, verifier, settings)
    app = RedactedLoggingMiddleware(app)
    return app


def serve_streamable_http(core: ApplicationCore, config: AppConfig) -> None:
    """Blocking entry point: serve ``core`` over Streamable HTTP (§T51/§T52/§T54).

    Binds ``[mcp.remote] bind_host:bind_port`` at ``path``. When the deployment
    requires auth (§V40: a non-loopback bind, or a loopback bind declared
    ``behind_proxy``), the §V9/§V40 startup gate is enforced (HTTPS assumption +
    valid OIDC, else :class:`~arknights_mcp.config.ConfigError`) and the app is
    wrapped in the full remote middleware stack (:func:`wrap_remote_app`: redacted
    logging + bearer validation + per-principal rate/concurrency + per-request
    size/timeout limits -- §V10/§V11/§V12). A genuine loopback dev bind stays authless
    and unmetered (§V9 exception). TLS termination is the reverse proxy's job (§I.api).
    """
    import uvicorn

    remote = config.mcp.remote
    if remote.requires_auth:
        # Fail closed before binding: refuse an unsafe posture (§V9/§V40).
        config.assert_remote_startup_safe()
    app: ASGIApp = build_asgi_app(core, path=remote.path)
    if remote.requires_auth:
        settings = OidcSettings.from_auth_config(config.auth)
        app = wrap_remote_app(app, config, OidcTokenVerifier(settings), settings)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=remote.bind_host,
            port=remote.bind_port,
            log_level="warning",
        )
    )
    server.run()
