"""Bounded Pydantic v2 input/output models for the MCP tools (§T30; §V22).

Every tool's parameters are a bounded :class:`~arknights_mcp.models.common.StrictModel`
(``extra="forbid"``, string caps, numeric bounds). The bounds are the enforcement
point for §V19 (search limit / page_size) and §V22 (opt-in heavy sections), and
they surface in each tool's ``inputSchema`` via
:func:`~arknights_mcp.models.common.tool_input_schema`.

Per-entity *output* fact payloads stay owned by the service layer (the
``StageFacts`` / ``DataStatus`` / ``SourceInfo`` dataclasses) -- a single home per
§V14/§V37; only the shared bounded output primitive :class:`PageInfo` lives here.
"""

from __future__ import annotations

from arknights_mcp.models.common import (
    MAX_ID_LEN,
    MAX_QUERY_LEN,
    PAGE_SIZE_DEFAULT,
    PAGE_SIZE_MAX,
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
    PageInfo,
    PageParams,
    Region,
    StrictModel,
    tool_input_schema,
)
from arknights_mcp.models.enemies import GetEnemyInput
from arknights_mcp.models.operators import (
    CompareMode,
    CompareOperatorModulesInput,
    GetOperatorInput,
)
from arknights_mcp.models.search import EntityType, SearchEntitiesInput
from arknights_mcp.models.sources import GetDataSourcesInput, GetDataStatusInput
from arknights_mcp.models.stages import (
    AnalysisDepth,
    AnalyzeStageInput,
    GetStageInput,
    SearchStagesInput,
)

__all__ = [
    # shared bases + bounds
    "StrictModel",
    "Region",
    "PageParams",
    "PageInfo",
    "tool_input_schema",
    "SEARCH_DEFAULT_LIMIT",
    "SEARCH_MAX_LIMIT",
    "PAGE_SIZE_DEFAULT",
    "PAGE_SIZE_MAX",
    "MAX_QUERY_LEN",
    "MAX_ID_LEN",
    # search
    "SearchEntitiesInput",
    "EntityType",
    # stages
    "SearchStagesInput",
    "GetStageInput",
    "AnalyzeStageInput",
    "AnalysisDepth",
    # enemies
    "GetEnemyInput",
    # operators
    "GetOperatorInput",
    "CompareOperatorModulesInput",
    "CompareMode",
    # data metadata
    "GetDataStatusInput",
    "GetDataSourcesInput",
]
