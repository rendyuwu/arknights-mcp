"""``python -m arknights_mcp`` entry point.

Mirrors the ``arknights-mcp`` console script (``project.scripts``) so the package
runs the same admin CLI whether invoked by module or by installed script -- e.g.
``python -m arknights_mcp serve --transport stdio`` (§T47).
"""

from __future__ import annotations

from arknights_mcp.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
