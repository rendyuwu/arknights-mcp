"""``arknights-mcp serve`` -- run the read-only MCP server (§I; §T47).

``serve`` is not an admin mutation: it opens the promoted build strictly
read-only (§V2) and never touches the network (§V1). It is the one CLI command
that runs a transport rather than building/inspecting data.

v0.1 ships the local ``stdio`` transport (§T47). The ``streamable-http`` transport
(private OAuth/OIDC, §T51/M6) is not available yet: selecting it fails closed with
a typed error rather than starting an unauthenticated remote listener (§V9).
"""

from __future__ import annotations

import argparse
import sys

from arknights_mcp.app import build_application
from arknights_mcp.cli._shared import CliContext
from arknights_mcp.config import load_config
from arknights_mcp.transports.stdio import serve_stdio


def _notice(message: str) -> None:
    """Operational notice on stderr -- stdout is the MCP protocol stream (§V13)."""
    print(message, file=sys.stderr)


def _cmd_serve(args: argparse.Namespace, ctx: CliContext) -> int:
    """Run the MCP server for the selected transport.

    stdio: build the shared core and serve until the client disconnects. The
    server owns stdout for the MCP protocol (§V13), so this command's own notices
    go to stderr only.
    """
    transport = args.transport
    if transport == "streamable-http":
        # Deferred to §T51/M6. Refuse rather than silently fall back to stdio or
        # start an unauthenticated remote listener (§V9).
        raise ValueError(
            "the streamable-http transport is not available in v0.1 (§T51/M6); "
            "use --transport stdio"
        )

    config = load_config(args.config)
    core = build_application(config)
    # stderr only: stdout is reserved for the MCP JSON-RPC stream (§V13).
    _notice(f"serve: stdio transport (schema_version {config.mcp.schema_version})")
    try:
        serve_stdio(core)
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        _notice("serve: shutting down")
    finally:
        core.provider.close()
    return 0
