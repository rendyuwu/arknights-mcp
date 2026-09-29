"""Shared low-level MCP ``Server`` builder for every transport.

The transport-agnostic construction lives here in exactly one home: both the
local ``stdio`` transport and the Streamable HTTP transport
dispatch the *same* :class:`~arknights_mcp.mcp.tool_registry.ToolRegistry` with the
*same* handlers. Neither transport re-declares ``tools/list`` /
``tools/call`` -- they adapt this one server to their wire (stdio pipes vs an ASGI
session), so a query cannot diverge across modes.

The two handlers are thin adapters over the shared registry -- no query logic
lives here:

* ``tools/list`` -> the shared registry's tool specs (read-only, bounded schema, and
  the shared envelope ``outputSchema`` every tool declares);
* ``tools/call`` -> the spec's handler, whose typed
  :class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope` is returned in *both*
  halves of the result: as ``structuredContent`` and as the compact JSON mirror in
  ``content``, because a content-only client reads ``content`` alone. A
  ``not_found``/degraded outcome is a normal result carried in the envelope, never a
  protocol error.
"""

from __future__ import annotations

from typing import Any

from mcp.server.lowlevel import Server
from mcp.types import TextContent, Tool
from pydantic import ValidationError

from arknights_mcp import __version__
from arknights_mcp.app import ApplicationCore
from arknights_mcp.instructions import server_instructions
from arknights_mcp.mcp.envelopes import ResponseEnvelope, error, invalid_input, mirror_text
from arknights_mcp.mcp.tool_registry import ToolRegistry

#: MCP ``serverInfo.name`` reported on ``initialize`` (matches the console script).
SERVER_NAME = "arknights-mcp"


def dispatch_tool_call(
    registry: ToolRegistry, name: str, arguments: dict[str, Any]
) -> ResponseEnvelope:
    """Look up + run one tool, mapping any failure to a typed envelope.

    The single dispatch home both transports share: ``stdio`` and Streamable
    HTTP call this exact function, so a tool call cannot diverge across modes.

    Three outcomes are all delivered as a typed :class:`ResponseEnvelope`, never a
    bare protocol error:

    * an unknown tool name -> ``not_found`` (the SDK does not validate names against
      ``list_tools``, so ``registry.get`` would otherwise raise ``KeyError``);
    * a malformed input model -> ``invalid_input`` (the handler's ``model_validate``
      raises a :class:`ValidationError`, which is caught here and wrapped in the same
      envelope with a clean, field-scoped message -- never the raw Pydantic framing
      or the ``errors.pydantic.dev`` URL);
    * a well-formed call -> whatever typed envelope the handler returns (``ok`` /
      ``not_found`` / ``data_stale`` / ... , with the ``database_unavailable`` /
      ``internal_error`` fail-closed guard living in the handler's ``run_guarded``).
    """
    if name not in registry:
        return error("not_found", f"unknown tool {name!r}")
    try:
        return registry.get(name).handler(**arguments)
    except ValidationError as exc:
        # A malformed request is a client mistake delivered as a typed
        # result, not a leaked framework error.
        return invalid_input(exc)


def build_server(core: ApplicationCore) -> Server[object, object]:
    """Build the low-level MCP ``Server`` bound to the shared registry.

    The server carries the same ``instructions`` string both transports use
    (PRD section 13.1). Its two handlers are thin adapters over the shared registry
    -- no query logic lives here, so ``stdio`` and Streamable HTTP dispatch an
    identical tool set with identical handlers.
    """
    server: Server[object, object] = Server(
        SERVER_NAME,
        version=__version__,
        instructions=server_instructions(),
    )

    # The low-level SDK's registration decorators are untyped; the handler bodies
    # below are fully typed. Ignore only the decorator-typing noise (SDK v1).
    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def _list_tools() -> list[Tool]:
        # Deterministic, read-only, bounded-schema tool set.
        return core.registry.to_mcp_tools()

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(
        name: str, arguments: dict[str, Any]
    ) -> tuple[list[TextContent], dict[str, Any]]:
        # Single dispatch home: look up the shared spec, run its handler,
        # and map an unknown name (not_found) or a malformed input model
        # (invalid_input) to a typed envelope -- never a bare protocol error.
        envelope = dispatch_tool_call(core.registry, name, arguments)
        # The envelope rides BOTH halves of the result -- ``structuredContent``
        # for a structured client, and the same payload as compact JSON text in
        # ``content`` for a content-only one. A content-only client reads ``content``
        # alone (LibreChat: ``result?.content ?? []``), so the earlier structured-only
        # result rendered every call as "(No response)" while initialize/tools/list
        # looked healthy. ``mirror_text`` is compact, not the SDK's ``indent=2``
        # fallback, and it is the same function the wire cap measures (``wire_size``),
        # so the duplication is accounted for rather than deleted -- the
        # wire-vs-measured gap closed on the measure side.
        #
        # Returned as an (unstructured, structured) tuple rather than a built
        # CallToolResult on purpose: the SDK validates structuredContent against the
        # tool's declared ``outputSchema`` on this path, and short-circuits
        # that check for a prebuilt result.
        return [TextContent(type="text", text=mirror_text(envelope))], envelope.to_dict()

    return server
