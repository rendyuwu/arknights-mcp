"""Private Streamable HTTP transport (§T51/M6; §V14; §I.api).

Serves the *same* shared core both transports use over the MCP Streamable HTTP
wire: a single ``POST /mcp`` ASGI endpoint (§I.api) driven by the SDK's
:class:`~mcp.server.streamable_http_manager.StreamableHTTPSessionManager`. The
session manager wraps :func:`arknights_mcp.transports._server.build_server`, the
one transport-agnostic server (§V14/§V37) -- so ``tools/list`` / ``tools/call``
dispatch the identical registry + handlers as ``stdio``; there is no second query
path to drift.

v0.1 scope (§T51): the transport itself. Bearer validation, per-principal limits,
and redacted logging are separate M6 tasks (§T52-§T54). Until token validation
lands (§T52), this binds **loopback only** -- :func:`serve_streamable_http`
refuses a non-loopback bind (§V9: authless non-loopback is forbidden; loopback dev
is the explicit exception). The intended production shape is loopback ``127.0.0.1``
behind a TLS-terminating reverse proxy (§I.api; deploy examples §T55).
"""

from __future__ import annotations

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from arknights_mcp.app import ApplicationCore
from arknights_mcp.config import AppConfig, ConfigError
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


def serve_streamable_http(core: ApplicationCore, config: AppConfig) -> None:
    """Blocking entry point: serve ``core`` over Streamable HTTP (§T51).

    Binds ``[mcp.remote] bind_host:bind_port`` at ``path``. Fail-closed (§V9):
    refuses a non-loopback bind because bearer validation is not wired yet
    (§T52) -- an authless listener may bind loopback only. TLS termination is the
    reverse proxy's job (§I.api); the process speaks plain HTTP on loopback.
    """
    import uvicorn

    remote = config.mcp.remote
    if not remote.is_loopback:
        # An unauthenticated non-loopback listener would violate §V9. Bearer
        # validation + the HTTPS/OAuth startup gate land in §T52; until then this
        # transport is loopback-only. Fail closed with a typed, path-free message.
        raise ConfigError(
            f"streamable-http may bind loopback only in v0.1 (§V9); bind_host "
            f"{remote.bind_host!r} is not loopback. Authenticated remote serving "
            "lands with OAuth/OIDC validation (§T52)."
        )

    app = build_asgi_app(core, path=remote.path)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=remote.bind_host,
            port=remote.bind_port,
            log_level="warning",
        )
    )
    server.run()
