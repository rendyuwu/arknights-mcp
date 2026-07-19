"""``arknights-mcp serve`` -- run the read-only MCP server (§I; §T47/§T51).

``serve`` is not an admin mutation: it opens the promoted build strictly
read-only (§V2) and never touches the network (§V1). It is the one CLI command
that runs a transport rather than building/inspecting data.

Two transports, one shared core (§V14): the local ``stdio`` transport (§T47) and
the private Streamable HTTP transport (§T51). Both build the same
:class:`~arknights_mcp.app.ApplicationCore`, so they dispatch the identical tool
set. Streamable HTTP enforces OAuth/OIDC bearer validation (§T52) whenever the
deployment requires auth (§V40: non-loopback bind, or loopback ``behind_proxy``); a
genuine loopback dev bind stays authless (§V9 exception). Non-secret OIDC
descriptors are overlaid from the environment (§I.env) before the startup gate runs.
"""

from __future__ import annotations

import argparse
import os
import sys

from arknights_mcp.app import build_application
from arknights_mcp.cli._shared import CliContext
from arknights_mcp.config import load_config
from arknights_mcp.mcp.envelopes import SCHEMA_VERSION
from arknights_mcp.transports.stdio import serve_stdio
from arknights_mcp.transports.streamable_http import serve_streamable_http


def _notice(message: str) -> None:
    """Operational notice on stderr -- stdout is the MCP protocol stream (§V13)."""
    print(message, file=sys.stderr)


def _cmd_serve(args: argparse.Namespace, ctx: CliContext) -> int:
    """Run the MCP server for the selected transport.

    Both transports build the shared core and serve until shutdown. Operational
    notices go to stderr only; for ``stdio`` stdout is reserved for the MCP
    JSON-RPC stream (§V13).
    """
    # Overlay non-secret OIDC descriptors from the environment (§I.env): issuer,
    # audience, jwks_url are supplied via env for remote serving.
    config = load_config(args.config, env=os.environ)
    core = build_application(config)
    try:
        if args.transport == "streamable-http":
            remote = config.mcp.remote
            # stderr only (§V13): announce the bind before entering the blocking
            # server loop. An auth-requiring deployment enforces the §V9/§V40 gate +
            # bearer validation inside serve_streamable_http (§T52).
            auth_state = "bearer-enforced" if remote.requires_auth else "loopback-dev (authless)"
            _notice(
                f"serve: streamable-http on {remote.bind_host}:{remote.bind_port}"
                f"{remote.path} [{auth_state}] (schema_version {SCHEMA_VERSION})"
            )
            serve_streamable_http(core, config)
        else:
            # Report the envelope wire version every tool result stamps (§V21), not
            # the unrelated config.mcp.schema_version knob -- the log must match the
            # wire.
            _notice(f"serve: stdio transport (schema_version {SCHEMA_VERSION})")
            serve_stdio(core)
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        _notice("serve: shutting down")
    finally:
        core.provider.close()
    return 0
