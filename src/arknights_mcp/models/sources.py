"""Bounded input models for the data-metadata tools.

``get_data_status`` (PRD Section 13.9) and ``get_data_sources`` take no
client parameters -- they report server-side posture. Both still declare an
explicit empty :class:`StrictModel` so every tool has a uniform, bounded
``inputSchema`` and ``extra="forbid"`` rejects any smuggled parameter.

The *output* of these tools is the public-safe projection owned by the source
registry / status services (``registry.public_view``, and the
``DataStatus``/``DataSourcesResult`` dataclasses). It is deliberately not
re-modelled here: a second projection would re-fork the allowlist.
"""

from __future__ import annotations

from arknights_mcp.models.common import StrictModel


class GetDataStatusInput(StrictModel):
    """Parameters for ``get_data_status`` -- none (PRD Section 13.9)."""


class GetDataSourcesInput(StrictModel):
    """Parameters for ``get_data_sources`` -- none."""
