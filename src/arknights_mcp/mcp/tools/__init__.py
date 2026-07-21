"""MCP tool implementations (§T32+).

Each module bridges a bounded input model (§T30 -- the §V18/§V19 gate) and a
shared domain service (§T31/§T17...) to the typed response envelope (§T29 -- one
§V23 status per result), and exposes a ``build_*_spec`` factory that registers
into the single shared
:class:`~arknights_mcp.mcp.tool_registry.ToolRegistry` (§V14). A tool owns no
query logic of its own -- only the model -> service -> envelope mapping -- so
both transports dispatch the exact same read-only (§V2) behaviour.

:func:`build_tool_registry` is the single home (§V37) for *which* tools exist:
it assembles every available M2 tool into one :class:`ToolRegistry`, so both
transports (stdio + Streamable HTTP, §V14) dispatch the identical set instead of
each re-enumerating it. It lives here rather than in ``tool_registry`` because it
depends on the concrete tool modules, which in turn import ``ToolSpec`` from
``tool_registry`` (keeping the assembler here avoids that import cycle).
"""

from __future__ import annotations

from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools._shared import ConnectionProvider
from arknights_mcp.mcp.tools.announcements import build_get_announcements_spec
from arknights_mcp.mcp.tools.drops import (
    build_get_item_drops_spec,
    build_get_stage_drops_spec,
)
from arknights_mcp.mcp.tools.enemy import build_get_enemy_spec
from arknights_mcp.mcp.tools.metadata import (
    build_get_data_sources_spec,
    build_get_data_status_spec,
)
from arknights_mcp.mcp.tools.module_compare import build_compare_operator_modules_spec
from arknights_mcp.mcp.tools.operator import build_get_operator_spec
from arknights_mcp.mcp.tools.search import build_search_entities_spec, build_search_stages_spec
from arknights_mcp.mcp.tools.stage import build_analyze_stage_spec, build_get_stage_spec
from arknights_mcp.sources.registry import SourceRegistry

#: The entity/analysis tool builders, each bound to only the read-only connection.
#: The two data-metadata tools (get_data_status/get_data_sources) need extra
#: dependencies (deployment mode / the live source registry), so they are wired
#: separately in :func:`build_tool_registry` rather than in this uniform tuple.
#: Single §V37 home for the tool list: both transports pick up whatever registers
#: here (§V14). Registration order is deterministic, so ``list_tools`` is stable.
_TOOL_BUILDERS = (
    build_search_entities_spec,
    build_search_stages_spec,
    build_get_stage_spec,
    build_get_enemy_spec,
    build_get_operator_spec,
    build_compare_operator_modules_spec,
    build_analyze_stage_spec,
    build_get_stage_drops_spec,
    build_get_item_drops_spec,
    build_get_announcements_spec,
)


def build_tool_registry(
    get_conn: ConnectionProvider,
    *,
    registry: SourceRegistry,
    mode: str,
) -> ToolRegistry:
    """Assemble the shared MCP tool registry with every available tool (§V14/§V37).

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build; every registered spec is read-only (§V2) and bound to it. ``registry``
    is the live source posture ``get_data_sources`` projects (§V27), and ``mode``
    is the deployment-mode label ``get_data_status`` reports. Both transports call
    this so they dispatch one identical tool set of all ten §I.tool tools (§V14) --
    there is no per-transport tool list to drift. Registration order is
    deterministic, so ``list_tools`` is stable.
    """
    tool_registry = ToolRegistry()
    for build in _TOOL_BUILDERS:
        tool_registry.register(build(get_conn))
    # The two data-metadata tools (§T77) round out the §I.tool set; they carry the
    # extra deployment-mode / source-registry deps the entity tools lack.
    tool_registry.register(build_get_data_status_spec(get_conn, mode=mode))
    tool_registry.register(build_get_data_sources_spec(get_conn, registry=registry))
    return tool_registry
