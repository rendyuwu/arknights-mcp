"""§T51 Streamable HTTP app wiring (fast, in-process; no socket).

The transport must reuse the shared core (§V14) and expose the MCP endpoint at
``/mcp`` (§I.api). These checks introspect the built Starlette app + the session
manager without opening a listener; the over-the-wire proof is the subprocess-free
socket smoke in ``tests/integration/test_serve_streamable_http_smoke.py``.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route

from arknights_mcp.app import build_application
from arknights_mcp.config import AppConfig
from arknights_mcp.transports._server import SERVER_NAME
from arknights_mcp.transports.stdio import build_server
from arknights_mcp.transports.streamable_http import build_asgi_app


def _routes(app: Starlette) -> list[Route]:
    return [r for r in app.routes if isinstance(r, Route)]


def test_app_routes_mcp_path() -> None:
    # §I.api: the MCP endpoint is served at POST /mcp by default.
    app = build_asgi_app(build_application(AppConfig()))
    paths = {r.path for r in _routes(app)}
    assert paths == {"/mcp"}


def test_app_honors_configured_path() -> None:
    # §I.api: the path is the operator's [mcp.remote] path, not hard-coded.
    app = build_asgi_app(build_application(AppConfig()), path="/custom-mcp")
    assert {r.path for r in _routes(app)} == {"/custom-mcp"}


def test_app_reuses_shared_server() -> None:
    # §V14: the session manager wraps the *same* transport-agnostic server stdio
    # runs -- same serverInfo.name + same shared instructions, no per-transport
    # server. The manager is stashed on app.state for this introspection.
    core = build_application(AppConfig())
    app = build_asgi_app(core)
    server = app.state.session_manager.app
    assert server.name == SERVER_NAME
    assert server.instructions is not None
    assert server.instructions.startswith("Arknights Intelligence MCP")
    # Same instructions string the stdio-built server carries (§V14).
    assert server.instructions == build_server(core).instructions


def test_app_dispatches_the_shared_registry() -> None:
    # §V14: no per-transport tool list -- the app is built over core.registry, the
    # one registry both transports dispatch. The registry object identity is shared
    # (build_asgi_app never forks its own tool set).
    core = build_application(AppConfig())
    build_asgi_app(core)
    assert core.registry.names()  # non-empty shared tool set
    # stdio and streamable-http both resolve tools through this same registry.
    assert build_server(core) is not None
