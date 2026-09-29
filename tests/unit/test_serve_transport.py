"""``serve`` transport gating (fast, in-process; no subprocess).

Covers the shared stdio server wiring and the Streamable HTTP
loopback-only guard: selecting ``streamable-http`` with a non-loopback bind must
fail closed with a clean exit rather than starting an unauthenticated remote
listener. The stdio handshake is covered by the
subprocess smoke in ``tests/integration/test_serve_stdio_smoke.py``; the
Streamable HTTP handshake by ``tests/integration/test_serve_streamable_http_smoke.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import anyio
from mcp import types

from arknights_mcp.app import build_application
from arknights_mcp.config import AppConfig
from arknights_mcp.mcp.envelopes import STATUS_VALUES, wire_size
from arknights_mcp.transports._server import dispatch_tool_call
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


def test_streamable_http_nonloopback_refused(tmp_path: Path) -> None:
    # Streamable-http binds loopback only in v0.1 (bearer validation is separate),
    # so a non-loopback bind fails closed to a clean exit 1 via the CLI's
    # handled-error path -- never an authless remote listener starting.
    from arknights_mcp.cli import main

    config = tmp_path / "config.toml"
    config.write_text(
        '[mcp.remote]\nenabled = true\nbind_host = "0.0.0.0"\n',
        encoding="utf-8",
    )
    rc = main(["--config", str(config), "serve", "--transport", "streamable-http"])
    assert rc == 1


def test_serve_defaults_to_stdio() -> None:
    # The parser default is stdio; the streamable-http gate must not trip for a
    # bare ``serve``. We assert the default resolves rather than running the loop
    # by checking the argparse-configured default directly.
    from arknights_mcp.cli import _build_parser

    args = _build_parser().parse_args(["serve"])
    assert args.transport == "stdio"


def test_build_server_exposes_shared_registry() -> None:
    # The stdio server is built from the shared core's registry -- no
    # per-transport tool list. The low-level Server carries the shared instructions.
    core = build_application(AppConfig())
    server = build_server(core)
    assert server.name == "arknights-mcp"
    assert server.instructions is not None
    assert server.instructions.startswith("Arknights Intelligence MCP")


def test_result_carries_the_envelope_in_both_wire_halves() -> None:
    # A content-only client reads ``content`` and never looks at
    # ``structuredContent``, so an empty ``content`` renders every call as
    # "(No response)". The result carries the same envelope twice: structured, and as
    # the compact JSON mirror in ``content``.
    result = _call_over_wire("get_enemy", {"server": "en", "game_id": "enemy_1007_slime"})
    assert result.structuredContent is not None
    assert result.structuredContent["schema_version"] == "0.4"
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    assert json.loads(block.text) == result.structuredContent
    # Compact, not the SDK's indent=2 fallback (~15% of dead bytes).
    assert "\n" not in block.text


def test_cap_measure_upper_bounds_the_emitted_frame() -> None:
    # The number the cap is enforced on must not be smaller than the
    # bytes the transport actually emits -- that gap (one measured copy, two shipped)
    # is the original defect. Measure the real serialized result against ``wire_size`` of the same
    # envelope.
    core = build_application(AppConfig())
    envelope = dispatch_tool_call(
        core.registry, "get_enemy", {"server": "en", "game_id": "enemy_1007_slime"}
    )
    result = _call_over_wire("get_enemy", {"server": "en", "game_id": "enemy_1007_slime"})
    emitted = len(result.model_dump_json(exclude_none=True).encode("utf-8"))
    assert emitted <= wire_size(envelope)


def test_unknown_tool_is_a_typed_envelope() -> None:
    # An unlisted tool name (the SDK does not validate names against
    # list_tools) fails closed to a typed ``not_found`` envelope, not a bare
    # ``isError`` string from an unhandled KeyError.
    result = _call_over_wire("does_not_exist", {})
    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["status"] == "not_found"
    assert result.structuredContent["status"] in STATUS_VALUES
