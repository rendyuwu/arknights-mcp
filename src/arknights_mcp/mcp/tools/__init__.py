"""MCP tool implementations.

Each module bridges a bounded input model (the input gate) and a
shared domain service to the typed response envelope (one
status per result), and exposes a ``build_*_spec`` factory that registers
into the single shared
:class:`~arknights_mcp.mcp.tool_registry.ToolRegistry`. A tool owns no
query logic of its own -- only the model -> service -> envelope mapping -- so
both transports dispatch the exact same read-only behaviour.

:func:`build_tool_registry` is the single home for *which* tools exist:
it assembles every available M2 tool into one :class:`ToolRegistry`, so both
transports (stdio + Streamable HTTP) dispatch the identical set instead of
each re-enumerating it. It lives here rather than in ``tool_registry`` because it
depends on the concrete tool modules, which in turn import ``ToolSpec`` from
``tool_registry`` (keeping the assembler here avoids that import cycle).
"""

from __future__ import annotations

from arknights_mcp.db.account import AccountStore
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools._shared import ConnectionProvider
from arknights_mcp.mcp.tools.account import (
    build_get_my_inventory_spec,
    build_get_my_operator_spec,
    build_get_my_roster_spec,
)
from arknights_mcp.mcp.tools.announcements import build_get_announcements_spec
from arknights_mcp.mcp.tools.banners import build_get_banners_spec
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

#: :func:`build_tool_registry` is the single home for *which* tools exist + their
#: registration order. Most builders need only the read-only connection; the three
#: image-ref-bearing builders (get_enemy/get_operator/get_banners) take the extra
#: image-ref emission gate, the three account tools (ADR 0020) take the optional account
#: roster store, and the two data-metadata tools (get_data_status/get_data_sources) take
#: the deployment mode / live source registry. Both transports pick up the identical
#: assembled set; the fixed order keeps ``list_tools`` stable.


def build_tool_registry(
    get_conn: ConnectionProvider,
    *,
    registry: SourceRegistry,
    mode: str,
    image_refs_enabled: bool = False,
    account_store: AccountStore | None = None,
) -> ToolRegistry:
    """Assemble the shared MCP tool registry with every available tool.

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build; every registered spec is read-only and bound to it. ``registry``
    is the live source posture ``get_data_sources`` projects, and ``mode``
    is the deployment-mode label ``get_data_status`` reports. ``image_refs_enabled``
    is the combined emission gate (config private-only posture AND the
    ``arknights_game_resource`` source enabled, computed once by the app layer via
    :func:`~arknights_mcp.services.image_refs.refs_enabled`); it is threaded into
    ``get_operator``/``get_enemy``/``get_banners`` so the additive ``image_refs`` field
    is emitted only when the source is enabled, and defaults ``False`` so a
    caller that does not opt in never emits it. ``account_store`` is the owner's account
    roster database (ADR 0020, read through a SELECT-only role); the three account tools
    are always registered and answer ``database_unavailable`` when it is ``None``. Both
    transports call this so they dispatch one identical tool set
    -- there is no per-transport tool list to drift. Registration order is
    deterministic (the order below), so ``list_tools`` is stable.
    """
    tool_registry = ToolRegistry()
    # Explicit, ordered assembly so ``list_tools`` stays stable: the three
    # image-ref-bearing tools slot into their historical positions among the
    # connection-only builders. Single home for which tools exist + their order.
    ordered = (
        build_search_entities_spec(get_conn),
        build_search_stages_spec(get_conn),
        build_get_stage_spec(get_conn),
        build_get_enemy_spec(get_conn, image_refs_enabled=image_refs_enabled),
        build_get_operator_spec(get_conn, image_refs_enabled=image_refs_enabled),
        build_compare_operator_modules_spec(get_conn),
        build_analyze_stage_spec(get_conn),
        build_get_stage_drops_spec(get_conn),
        build_get_item_drops_spec(get_conn),
        build_get_announcements_spec(get_conn),
        build_get_banners_spec(get_conn, image_refs_enabled=image_refs_enabled),
        build_get_my_roster_spec(get_conn, account_store=account_store),
        build_get_my_operator_spec(get_conn, account_store=account_store),
        build_get_my_inventory_spec(get_conn, account_store=account_store),
    )
    for spec in ordered:
        tool_registry.register(spec)
    # The two data-metadata tools round out the tool set; they carry the
    # extra deployment-mode / source-registry deps the entity tools lack.
    tool_registry.register(build_get_data_status_spec(get_conn, mode=mode))
    tool_registry.register(build_get_data_sources_spec(get_conn, registry=registry))
    return tool_registry
