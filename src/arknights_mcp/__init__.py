"""Arknights Intelligence MCP.

Read-only Arknights intelligence exposed over the Model Context Protocol from
one shared application core across two transports (local ``stdio`` and a private
OAuth/OIDC Streamable HTTP endpoint), backed by versioned SQLite snapshots. See
``SPEC.md`` for goal (§G), constraints (§C), interfaces (§I), and invariants (§V).
"""

from __future__ import annotations

__version__ = "0.2.0"

__all__ = ["__version__"]
