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
from pydantic import ValidationError

from arknights_mcp import __version__
from arknights_mcp.app import ApplicationCore
from arknights_mcp.instructions import server_instructions
from arknights_mcp.mcp.envelopes import ResponseEnvelope, error, invalid_input
from arknights_mcp.mcp.tool_registry import ToolRegistry

#: MCP ``serverInfo.name`` reported on ``initialize`` (matches the console script).
SERVER_NAME = "arknights-mcp"


def dispatch_tool_call(
    registry: ToolRegistry, name: str, arguments: dict[str, Any]
) -> ResponseEnvelope:
    """Look up + run one tool, mapping any failure to a typed envelope (§V14/§V23).

    The single dispatch home both transports share (§V37): ``stdio`` and Streamable
    HTTP call this exact function, so a tool call cannot diverge across modes.

    Three outcomes are all delivered as a typed :class:`ResponseEnvelope`, never a
    bare protocol error:

    * an unknown tool name -> ``not_found`` (the SDK does not validate names against
      ``list_tools``, so ``registry.get`` would otherwise raise ``KeyError``);
    * a malformed input model -> ``invalid_input`` (§V71 (c)/B60: the handler's
      ``model_validate`` raises a :class:`ValidationError`, which is caught here and
      wrapped in the same envelope with a clean, field-scoped message -- never the raw
      Pydantic framing or the ``errors.pydantic.dev`` URL);
    * a well-formed call -> whatever typed envelope the handler returns (``ok`` /
      ``not_found`` / ``data_stale`` / ... , with the ``database_unavailable`` /
      ``internal_error`` fail-closed guard living in the handler's ``run_guarded``).
    """
    if name not in registry:
        return error("not_found", f"unknown tool {name!r}")
    try:
        return registry.get(name).handler(**arguments)
    except ValidationError as exc:
        # §V71 (c)/B60: a malformed request is a client mistake delivered as a typed
        # result, not a leaked framework error.
        return invalid_input(exc)


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
        # Single dispatch home (§V14/§V37): look up the shared spec, run its handler,
        # and map an unknown name (§V23 not_found) or a malformed input model
        # (§V23/§V71 invalid_input) to a typed envelope -- never a bare protocol error.
        envelope = dispatch_tool_call(core.registry, name, arguments)
        # Carry the envelope as structured content only -- a single copy on the wire
        # (§V14; smoke test). Returning the dict would make the SDK ALSO emit an
        # indented ``json.dumps(indent=2)`` copy in ``content``, so a payload the
        # envelope builder measures once under the §V22 cap would ship ~2x that on
        # the wire (the B21 wire-vs-measured gap, reintroduced by the transport).
        # One copy keeps the measured cap == the wire bytes; returning a built
        # CallToolResult short-circuits the SDK's dict->(structured+text) split.
        return CallToolResult(content=[], structuredContent=envelope.to_dict())

    return server
