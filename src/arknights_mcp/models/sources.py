"""Bounded input models for the data-metadata tools (§T30; §V27).

``get_data_status`` (§I; PRD §13.9) and ``get_data_sources`` (§I; §V27) take no
client parameters -- they report server-side posture. Both still declare an
explicit empty :class:`StrictModel` so every tool has a uniform, bounded
``inputSchema`` and ``extra="forbid"`` rejects any smuggled parameter (§V18).

The *output* of these tools is the public-safe projection owned by the source
registry / status services (``registry.public_view`` -- §V34, and the
``DataStatus``/``DataSourcesResult`` dataclasses). It is deliberately not
re-modelled here: a second projection would re-fork the §V27 allowlist (B18).
"""

from __future__ import annotations

from arknights_mcp.models.common import StrictModel


class GetDataStatusInput(StrictModel):
    """Parameters for ``get_data_status`` -- none (§I; PRD §13.9)."""


class GetDataSourcesInput(StrictModel):
    """Parameters for ``get_data_sources`` -- none (§I; §V27)."""
