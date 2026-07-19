"""§T47 ``serve`` transport gating (fast, in-process; no subprocess).

The v0.1 milestone ships only the local ``stdio`` transport. Selecting the
Streamable HTTP transport (§T51/M6) must fail closed with a clean exit rather than
silently falling back to stdio or starting an unauthenticated remote listener
(§V9). The stdio handshake itself is covered by the subprocess smoke in
``tests/integration/test_serve_stdio_smoke.py``.
"""

from __future__ import annotations

import anyio
from mcp import types

from arknights_mcp.app import build_application
from arknights_mcp.config import AppConfig
from arknights_mcp.mcp.envelopes import STATUS_VALUES
from arknights_mcp.transports.stdio import build_server


def _call_over_wire(name: str, arguments: dict[str, object]) -> types.CallToolResult:
    """Drive the built stdio server's ``tools/call`` handler in-process (no subprocess)."""
    server = build_server(build_application(AppConfig()))
    handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )
    result = anyio.run(handler, req)
    call_result = result.root
    assert isinstance(call_result, types.CallToolResult)
    return call_result


def test_streamable_http_transport_refused() -> None:
    # A clean exit 1 via the CLI's handled-error path (§T51 deferred), not a crash.
    from arknights_mcp.cli import main

    rc = main(["serve", "--transport", "streamable-http"])
    assert rc == 1


def test_serve_defaults_to_stdio() -> None:
    # The parser default is stdio; the streamable-http gate must not trip for a
    # bare ``serve``. We assert the default resolves rather than running the loop
    # by checking the argparse-configured default directly.
    from arknights_mcp.cli import _build_parser

    args = _build_parser().parse_args(["serve"])
    assert args.transport == "stdio"


def test_build_server_exposes_shared_registry() -> None:
    # §V14: the stdio server is built from the shared core's registry -- no
    # per-transport tool list. The low-level Server carries the shared instructions.
    core = build_application(AppConfig())
    server = build_server(core)
    assert server.name == "arknights-mcp"
    assert server.instructions is not None
    assert server.instructions.startswith("Arknights Intelligence MCP")


def test_result_carries_a_single_wire_copy() -> None:
    # §V22 (B21 wire-vs-measured gap): the transport must not let the SDK emit the
    # envelope twice (structuredContent + an indented text mirror), which would
    # ship a payload the cap measured *once* at ~2x on the wire. The result carries
    # the envelope only in structuredContent; ``content`` holds no duplicate copy.
    result = _call_over_wire("get_enemy", {"server": "en", "game_id": "enemy_1007_slime"})
    assert result.content == []
    assert result.structuredContent is not None
    assert result.structuredContent["schema_version"] == "0.1"


def test_unknown_tool_is_a_typed_envelope() -> None:
    # §V23: an unlisted tool name (the SDK does not validate names against
    # list_tools) fails closed to a typed ``not_found`` envelope, not a bare
    # ``isError`` string from an unhandled KeyError.
    result = _call_over_wire("does_not_exist", {})
    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["status"] == "not_found"
    assert result.structuredContent["status"] in STATUS_VALUES
