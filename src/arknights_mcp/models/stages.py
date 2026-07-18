"""Bounded input models for the stage tools (§T30; §V19/§V22).

Covers ``search_stages`` (§T33), ``get_stage`` (§T34) and ``analyze_stage``
(§T40). The §V22 lever lives here: the heavy ``get_stage`` sections (map tiles,
routes, spawns) are opt-in include flags that default ``False``, and each is paged
through the bounded :class:`~arknights_mcp.models.common.PageParams`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from arknights_mcp.models.common import (
    MAX_ID_LEN,
    MAX_QUERY_LEN,
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
    PageParams,
    Region,
    StrictModel,
)

#: Detail depth for ``analyze_stage`` (§T40). Deeper levels return more evidence,
#: still bounded by the §V22 response cap.
AnalysisDepth = Literal["summary", "standard", "detailed"]


class _StageSelector(StrictModel):
    """Region + exactly-one-of (stage_code | game_id) selector (§V5).

    A stage is addressed by its human ``stage_code`` (e.g. ``4-4``) or its unique
    ``game_id``. Requiring exactly one keeps the lookup unambiguous; ``server`` is
    mandatory so the fact is always region-attributed (§V5).
    """

    server: Region
    stage_code: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LEN)
    game_id: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LEN)

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> _StageSelector:
        if (self.stage_code is None) == (self.game_id is None):
            raise ValueError("provide exactly one of stage_code or game_id")
        return self


class SearchStagesInput(StrictModel):
    """Parameters for ``search_stages`` (§I; §V19).

    ``query`` is length-capped free text (§V18); an exact ``stage_code`` match is
    ranked first by the tool (§T33). ``server`` optionally scopes to one region
    (§V5). ``limit`` is bounded to the §V19 window (default 10, max 50).
    """

    query: str = Field(min_length=1, max_length=MAX_QUERY_LEN)
    server: Region | None = None
    limit: int = Field(default=SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT)


class GetStageInput(_StageSelector):
    """Parameters for ``get_stage`` (§I; §V22).

    The heavy sections are opt-in: ``include_map`` (tile grid), ``include_routes``
    and ``include_spawns`` each default ``False`` so the default response stays
    small (§V22). Each requested section is paged through its **own** bounds --
    ``map_page`` / ``routes_page`` / ``spawns_page`` -- so a client can hold the
    whole stage in one call yet page a large section (e.g. the spawn timeline)
    without shifting the others off (§V19). Every page is bounded, so no opted-in
    payload ever returns an unbounded slice.
    """

    include_map: bool = False
    include_routes: bool = False
    include_spawns: bool = False
    map_page: PageParams = Field(default_factory=PageParams)
    routes_page: PageParams = Field(default_factory=PageParams)
    spawns_page: PageParams = Field(default_factory=PageParams)


class AnalyzeStageInput(_StageSelector):
    """Parameters for ``analyze_stage`` (§I; §V6).

    Selects a stage (region + one selector, §V5) and the evidence ``depth``. Every
    depth still returns the §V6 evidence-backed observations; deeper levels add
    detail, bounded by the §V22 response cap.
    """

    depth: AnalysisDepth = "standard"
