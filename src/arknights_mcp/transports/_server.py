"""Shared low-level MCP ``Server`` builder for every transport (§V14/§V37).

The transport-agnostic construction lives here in exactly one home: both the
local ``stdio`` transport (§T47) and the Streamable HTTP transport (§T51)
dispatch the *same* :class:`~arknights_mcp.mcp.tool_registry.ToolRegistry` with the
*same* handlers (§V14). Neither transport re-declares ``tools/list`` /
``tools/call`` -- they adapt this one server to their wire (stdio pipes vs an ASGI
session), so a query cannot diverge across modes.

The two handlers are thin adapters over the shared registry -- no query logic
lives here:

* ``tools/list`` -> the shared registry's tool specs (read-only, bounded schema);
* ``tools/call`` -> the spec's handler, whose typed
  :class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope` (§V23) is returned as the
  call's structured content. A ``not_found``/degraded outcome is a normal result
  carried in the envelope, never a protocol error.
"""

from __future__ import annotations

from typing import Any

from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, Tool

from arknights_mcp import __version__
from arknights_mcp.app import ApplicationCore
from arknights_mcp.instructions import server_instructions
from arknights_mcp.mcp.envelopes import error

#: MCP ``serverInfo.name`` reported on ``initialize`` (matches the console script).
SERVER_NAME = "arknights-mcp"


def build_server(core: ApplicationCore) -> Server[object, object]:
    """Build the low-level MCP ``Server`` bound to the shared registry (§V14).

    The server carries the same ``instructions`` string both transports use
    (§V14; PRD §13.1). Its two handlers are thin adapters over the shared registry
    -- no query logic lives here, so ``stdio`` and Streamable HTTP dispatch an
    identical tool set with identical handlers.
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
    async def _call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        # Single dispatch home (§V14): look up the shared spec and run its handler.
        # An unknown tool name is a typed result, not a bare protocol error: the SDK
        # does not validate names against list_tools, so a name outside the registry
        # would otherwise raise KeyError from ``registry.get`` and surface as an
        # untyped ``isError`` string. Fail it closed to a typed ``not_found``
        # envelope (§V23) so every result carries a status from the vocabulary.
        if name not in core.registry:
            envelope = error("not_found", f"unknown tool {name!r}")
        else:
            # The handler validates its own bounded input model (§V18/§V19) and
            # returns a typed envelope (§V23).
            envelope = core.registry.get(name).handler(**arguments)
        # Carry the envelope as structured content only -- a single copy on the wire
        # (§V14; smoke test). Returning the dict would make the SDK ALSO emit an
        # indented ``json.dumps(indent=2)`` copy in ``content``, so a payload the
        # envelope builder measures once under the §V22 cap would ship ~2x that on
        # the wire (the B21 wire-vs-measured gap, reintroduced by the transport).
        # One copy keeps the measured cap == the wire bytes; returning a built
        # CallToolResult short-circuits the SDK's dict->(structured+text) split.
        return CallToolResult(content=[], structuredContent=envelope.to_dict())

    return server
