"""``arknights-mcp`` command-line entry point (admin-only).

Stub scaffold (PRD Section 20 / SPEC.md §I CLI). The real subcommands land in
later tasks:

* ``sync`` / ``import`` / ``validate`` / ``status`` / ``doctor`` -> T21-T25
* ``source list|enable|disable|purge`` -> T26
* ``serve --transport stdio|streamable-http`` -> T29 / T51
* config loading + startup safety checks (V9) -> T8

Admin operations are CLI-only and are never exposed as MCP tools (V28).
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point.

    Argument parsing and the subcommand dispatch table are implemented in later
    §T tasks; this M0 stub exists so the ``arknights-mcp`` entry point resolves
    and the package layout is verifiable.
    """
    _ = sys.argv if argv is None else argv
    print(
        "arknights-mcp: CLI not implemented yet (M0 scaffold). "
        "See SPEC.md §T for the build order.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
