"""``arknights-mcp`` command-line entry point (admin-only).

Admin operations (``sync``, ``import``, ``validate``, ``status``, ``doctor``,
``source ...``) are CLI-only and are never exposed as MCP tools (§V28). Network
access happens only here, in ``sync`` -- never at query time (§V1). Every build
produces a *candidate* that is promoted only after it passes validation, and the
active database is never mutated in place (§V3, §V4).

This package splits the former ``cli.py`` (§V38): one module per command group
(:mod:`~arknights_mcp.cli.sync` §T21, :mod:`~arknights_mcp.cli.import_` §T22,
:mod:`~arknights_mcp.cli.validate` §T23, :mod:`~arknights_mcp.cli.status`
status+doctor §T25, :mod:`~arknights_mcp.cli.source` §T26, :mod:`~arknights_mcp.cli.serve` stdio
§T47) over shared helpers in :mod:`~arknights_mcp.cli._shared`. This module wires
the argument parser and dispatches. ``serve --transport streamable-http`` (M6)
lands with §T51.
"""

from __future__ import annotations

import argparse
import sys

from arknights_mcp.cli._shared import (
    _HANDLED_ERRORS,
    DEFAULT_CONFIG_PATH,
    CliContext,
    _err,
)
from arknights_mcp.cli.import_ import _cmd_import
from arknights_mcp.cli.serve import _cmd_serve
from arknights_mcp.cli.source import (
    _cmd_source_disable,
    _cmd_source_enable,
    _cmd_source_list,
    _cmd_source_purge,
)
from arknights_mcp.cli.status import _cmd_doctor, _cmd_status
from arknights_mcp.cli.sync import _cmd_sync
from arknights_mcp.cli.validate import _cmd_validate
from arknights_mcp.sources.arknights_assets import Fetcher

# Re-exported so ``arknights_mcp.cli.is_placeholder`` resolves to the single
# shared home and the §V37 no-re-duplication guard (test_text.py) still holds.
from arknights_mcp.util.text import is_placeholder


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arknights-mcp",
        description="Admin CLI for the read-only Arknights Intelligence MCP (CLI-only ops, §V28).",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="path to config.toml (default: ./config.toml)",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_sync = sub.add_parser("sync", help="build a candidate from an allowlisted remote source")
    p_sync.add_argument("--server", required=True, choices=["en", "cn", "all"])
    p_sync.set_defaults(func=_cmd_sync)

    p_import = sub.add_parser("import", help="build a candidate from a local snapshot directory")
    p_import.add_argument("--server", required=True, choices=["en", "cn"])
    p_import.add_argument("--source-path", required=True, help="path to the snapshot directory")
    p_import.set_defaults(func=_cmd_import)

    p_validate = sub.add_parser("validate", help="run the validation gate against a database")
    p_validate.add_argument("--database", required=True, help="path to the database to validate")
    p_validate.add_argument(
        "--allow-empty",
        action="store_true",
        help="do not require at least one imported snapshot (schema-only check)",
    )
    p_validate.set_defaults(func=_cmd_validate)

    p_status = sub.add_parser("status", help="show the active snapshot + schema version")
    p_status.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p_status.set_defaults(func=_cmd_status)

    p_doctor = sub.add_parser("doctor", help="report environment/config/database health")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_serve = sub.add_parser("serve", help="run the read-only MCP server (stdio)")
    p_serve.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="transport to serve (v0.1: stdio; streamable-http lands in M6/§T51)",
    )
    p_serve.set_defaults(func=_cmd_serve)

    p_source = sub.add_parser("source", help="manage data sources (list/enable/disable/purge)")
    source_sub = p_source.add_subparsers(dest="source_command", metavar="<action>")

    p_src_list = source_sub.add_parser("list", help="list registered sources + enabled status")
    p_src_list.add_argument("--json", action="store_true", help="emit public-safe JSON")
    p_src_list.set_defaults(func=_cmd_source_list)

    p_src_enable = source_sub.add_parser("enable", help="resume sync for a source (keeps data)")
    p_src_enable.add_argument("source_id")
    p_src_enable.add_argument("--reason", help="note recorded in the policy-event journal")
    p_src_enable.set_defaults(func=_cmd_source_enable)

    p_src_disable = source_sub.add_parser(
        "disable", help="stop new sync for a source; keep current data (§V20)"
    )
    p_src_disable.add_argument("source_id")
    p_src_disable.add_argument("--reason", help="note recorded in the policy-event journal")
    p_src_disable.set_defaults(func=_cmd_source_disable)

    p_src_purge = source_sub.add_parser(
        "purge", help="rebuild without a source's rows; current DB active until validated (§V20)"
    )
    p_src_purge.add_argument("source_id")
    p_src_purge.add_argument(
        "--rebuild", action="store_true", help="required: rebuild + validate before promoting"
    )
    p_src_purge.add_argument("--reason", help="note recorded in the policy-event journal")
    p_src_purge.set_defaults(func=_cmd_source_purge)

    return parser


def main(argv: list[str] | None = None, *, fetcher: Fetcher | None = None) -> int:
    """Console-script entry point. ``fetcher`` is a test seam for ``sync`` (§T21)."""
    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return 2
    ctx = CliContext(fetcher=fetcher)
    try:
        return int(func(args, ctx))
    except _HANDLED_ERRORS as exc:
        _err(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "CliContext", "is_placeholder"]
