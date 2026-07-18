"""§T47 ``serve`` transport gating (fast, in-process; no subprocess).

The v0.1 milestone ships only the local ``stdio`` transport. Selecting the
Streamable HTTP transport (§T51/M6) must fail closed with a clean exit rather than
silently falling back to stdio or starting an unauthenticated remote listener
(§V9). The stdio handshake itself is covered by the subprocess smoke in
``tests/integration/test_serve_stdio_smoke.py``.
"""

from __future__ import annotations

from arknights_mcp.app import build_application
from arknights_mcp.config import AppConfig
from arknights_mcp.transports.stdio import build_server


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
