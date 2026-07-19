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
stays authless -- the explicit §V9 exception. Per-principal limits and redacted
logging remain separate M6 tasks (§T53-§T54). The intended production shape is
loopback ``127.0.0.1`` behind a TLS-terminating reverse proxy (§I.api; §T55).
"""

from __future__ import annotations

import json

import anyio
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from arknights_mcp.app import ApplicationCore
from arknights_mcp.auth.oidc import AuthError, OidcSettings, OidcTokenVerifier
from arknights_mcp.config import AppConfig
from arknights_mcp.transports._server import build_server


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


class _BearerAuthASGIApp:
    """ASGI middleware enforcing §V10 bearer validation on every HTTP request.

    Wraps the Streamable HTTP app: an ``http`` request must carry a bearer token
    that :class:`~arknights_mcp.auth.oidc.OidcTokenVerifier` validates, else the
    request is rejected with a typed ``WWW-Authenticate`` challenge (401 for a
    bad/absent token, 403 for insufficient scope) and the inner app is never
    reached. Non-``http`` scopes (``lifespan``, ``websocket``) pass straight through
    so the session manager's task group still starts. The validated
    :class:`~arknights_mcp.auth.principal.Principal` is stashed on
    ``scope["state"]["principal"]`` for downstream per-principal isolation (§T53).
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
        # Attach the validated identity for per-principal isolation (§T53).
        scope["state"] = {**scope.get("state", {}), "principal": principal}
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


def serve_streamable_http(core: ApplicationCore, config: AppConfig) -> None:
    """Blocking entry point: serve ``core`` over Streamable HTTP (§T51/§T52).

    Binds ``[mcp.remote] bind_host:bind_port`` at ``path``. When the deployment
    requires auth (§V40: a non-loopback bind, or a loopback bind declared
    ``behind_proxy``), the §V9/§V40 startup gate is enforced (HTTPS assumption +
    valid OIDC, else :class:`~arknights_mcp.config.ConfigError`) and the app is
    wrapped in :class:`_BearerAuthASGIApp`. A genuine loopback dev bind stays
    authless (§V9 exception). TLS termination is the reverse proxy's job (§I.api).
    """
    import uvicorn

    remote = config.mcp.remote
    if remote.requires_auth:
        # Fail closed before binding: refuse an unsafe posture (§V9/§V40).
        config.assert_remote_startup_safe()
    app: ASGIApp = build_asgi_app(core, path=remote.path)
    if remote.requires_auth:
        settings = OidcSettings.from_auth_config(config.auth)
        app = _BearerAuthASGIApp(app, OidcTokenVerifier(settings), settings)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=remote.bind_host,
            port=remote.bind_port,
            log_level="warning",
        )
    )
    server.run()
