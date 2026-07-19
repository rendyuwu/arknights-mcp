"""Local stdio transport (§T47; §V13: protocol->stdout, logs->stderr; §V14).

Runs the shared read-only core (:class:`~arknights_mcp.app.ApplicationCore`) as an
MCP server over stdio. The transport-agnostic server (``tools/list`` /
``tools/call`` adapters over the shared registry) is built once in
:mod:`arknights_mcp.transports._server` and reused by both transports (§V14/§V37);
this module only pumps that server over the stdio wire.

§V13 is structural here: the MCP JSON-RPC protocol owns stdout -- the SDK's
``stdio_server`` writes framed messages there and nothing else. Startup notices
and Python ``logging`` go to stderr (the CLI command owns that). This module
writes nothing to stdout itself.
"""

from __future__ import annotations

import anyio
from mcp.server.stdio import stdio_server

from arknights_mcp.app import ApplicationCore

# Re-exported: the shared server builder + name live in ``_server`` (single §V14
# home). Existing importers (`from arknights_mcp.transports.stdio import
# build_server`) and both transports resolve to that one home, not a per-transport
# copy of the dispatch logic (§V37).
from arknights_mcp.transports._server import SERVER_NAME, build_server

__all__ = ["SERVER_NAME", "build_server", "run_stdio", "serve_stdio"]


async def run_stdio(core: ApplicationCore) -> None:
    """Serve ``core`` over stdio until the client disconnects (EOF on stdin)."""
    server = build_server(core)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def serve_stdio(core: ApplicationCore) -> None:
    """Blocking entry point: run the stdio server on a fresh event loop (§T47)."""
    anyio.run(run_stdio, core)
