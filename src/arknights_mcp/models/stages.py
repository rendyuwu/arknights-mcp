"""Bounded input models for the stage tools (§T30; §V19/§V22).

Covers ``search_stages`` (§T33), ``get_stage`` (§T34) and ``analyze_stage``
(§T40). The §V22 lever lives here: the heavy ``get_stage`` sections (tile grid,
routes, spawns) are opt-in include flags that default ``False``. The tile grid is
a single compact per-row block (§V74 (c)); routes and spawns page through the
bounded :class:`~arknights_mcp.models.common.PageParams`.
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
    ``game_id``. Requiring exactly one keeps the *call* unambiguous; ``server`` is
    mandatory so the fact is always region-attributed (§V5).

    §V102 (a) (§T195, B139): the selector CONTRACT is client-facing text, so its home is
    the tool description (:data:`~arknights_mcp.mcp.tools._shared.STAGE_SELECTOR_NOTE`,
    carried by all three stage tools), NOT a ``Field(description=...)`` here: the
    published schema strips every ``description`` keyword (§V71 b -- docstrings carry
    internal cites), so text placed here would never reach a client. What the schema DOES
    carry is the structural half -- the two optional selectors, their bounds, and
    ``additionalProperties: false``. The "exactly one" rule was previously learnable only
    by tripping this validator; the pick a shared ``stage_code`` resolves to is disclosed
    per response as a limitation (§V102 b), since ``stage_code`` is not unique (927 en
    codes are shared on the 2026-07-28 build).
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
    small (§V22). ``include_map`` returns the grid as one compact per-row block
    (§V74 (c)) -- a whole board fits one response, so it takes no page cursor; an
    over-budget board is omitted with a §V22 limitation. ``include_routes`` and
    ``include_spawns`` are paged through their **own** bounds -- ``routes_page`` /
    ``spawns_page`` -- so a client can hold the whole stage in one call yet page a
    large section (e.g. the spawn timeline) without shifting the others off (§V19).
    Every page is bounded, so no opted-in payload ever returns an unbounded slice.

    ``include_map_image`` (default ``False``, §V22) adds a render-own SVG of the
    stage grid (§T122) -- a DERIVED image drawn from the stored typed grid data,
    never third-party art (§V16) and never the §V63 URL reference. An over-budget
    board is omitted with a §V22 limitation rather than an oversized payload.
    """

    include_map: bool = False
    include_routes: bool = False
    include_spawns: bool = False
    include_map_image: bool = False
    routes_page: PageParams = Field(default_factory=PageParams)
    spawns_page: PageParams = Field(default_factory=PageParams)


class AnalyzeStageInput(_StageSelector):
    """Parameters for ``analyze_stage`` (§I; §V6).

    Selects a stage (region + one selector, §V5) and the evidence ``depth``. Every
    depth still returns the §V6 evidence-backed observations; deeper levels add
    detail, bounded by the §V22 response cap.
    """

    depth: AnalysisDepth = "standard"


class GetStageDropsInput(_StageSelector):
    """Parameters for ``get_stage_drops`` (§I; §V53/§V55).

    Selects a stage (region + one selector, §V5) whose penguin drop-rate cache to
    report. ``include_efficiency`` opts into the deterministic §T90 farming
    observations (sanity per item); off by default so the base response is the
    compact drop facts + provenance + expiry (§V22). Reuses the shared region +
    exactly-one-selector gate (§V37), so a drop lookup is region-attributed and
    unambiguous like the other stage tools.
    """

    include_efficiency: bool = False
