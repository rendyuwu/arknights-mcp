"""Local stdio transport (§T47; §V13: protocol->stdout, logs->stderr; §V14).

Runs the shared read-only core (:class:`~arknights_mcp.app.ApplicationCore`) as an
MCP server over stdio. Both transports dispatch the *same* registry (§V14); this
module only adapts it to the MCP stdio wire:

* ``tools/list`` -> the shared registry's tool specs (read-only, bounded schema);
* ``tools/call`` -> the spec's handler, whose typed
  :class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope` (§V23) is returned as the
  call's structured content. A ``not_found``/degraded outcome is a normal result
  carried in the envelope, never a protocol error.

§V13 is structural here: the MCP JSON-RPC protocol owns stdout -- the SDK's
``stdio_server`` writes framed messages there and nothing else. Startup notices
and Python ``logging`` go to stderr (the CLI command owns that). This module
writes nothing to stdout itself.
"""

from __future__ import annotations

from typing import Any

import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool

from arknights_mcp import __version__
from arknights_mcp.app import ApplicationCore
from arknights_mcp.instructions import server_instructions

#: MCP ``serverInfo.name`` reported on ``initialize`` (matches the console script).
SERVER_NAME = "arknights-mcp"


def build_server(core: ApplicationCore) -> Server[object, object]:
    """Build the low-level MCP ``Server`` bound to the shared registry (§V14).

    The server carries the same ``instructions`` string both transports use
    (§V14; PRD §13.1). Its two handlers are thin adapters over the shared registry
    -- no query logic lives here.
    """
    server: Server[object, object] = Server(
        SERVER_NAME,
        version=__version__,
        instructions=server_instructions(),
    )

    # The low-level SDK's registration decorators are untyped; the handler bodies
    # below are fully typed. Ignore only the decorator-typing noise (§V25 SDK v1).
    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def _list_tools() -> list[Tool]:
        # Deterministic, read-only, bounded-schema tool set (§V14/§V2).
        return core.registry.to_mcp_tools()

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Single dispatch home (§V14): look up the shared spec and run its handler.
        # The handler validates its own bounded input model (§V18/§V19) and returns
        # a typed envelope (§V23); we surface it as structured content.
        spec = core.registry.get(name)
        return spec.handler(**arguments).to_dict()

    return server


async def run_stdio(core: ApplicationCore) -> None:
    """Serve ``core`` over stdio until the client disconnects (EOF on stdin)."""
    server = build_server(core)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def serve_stdio(core: ApplicationCore) -> None:
    """Blocking entry point: run the stdio server on a fresh event loop (§T47)."""
    anyio.run(run_stdio, core)
