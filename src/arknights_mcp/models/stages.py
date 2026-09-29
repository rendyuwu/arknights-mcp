"""Bounded input models for the stage tools.

Covers ``search_stages``, ``get_stage`` and ``analyze_stage``.
The opt-in lever lives here: the heavy ``get_stage`` sections (tile grid,
routes, spawns) are opt-in include flags that default ``False``. The tile grid is
a single compact per-row block; routes and spawns page through the
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

#: Detail depth for ``analyze_stage``. Deeper levels return more evidence,
#: still bounded by the response cap.
AnalysisDepth = Literal["summary", "standard", "detailed"]


class _StageSelector(StrictModel):
    """Region + exactly-one-of (stage_code | game_id) selector.

    A stage is addressed by its human ``stage_code`` (e.g. ``4-4``) or its unique
    ``game_id``. Requiring exactly one keeps the *call* unambiguous; ``server`` is
    mandatory so the fact is always region-attributed.

    The selector CONTRACT is client-facing text, so its home is
    the tool description (:data:`~arknights_mcp.mcp.tools._shared.STAGE_SELECTOR_NOTE`,
    carried by all three stage tools), NOT a ``Field(description=...)`` here: the
    published schema strips every ``description`` keyword (docstrings carry
    internal notes), so text placed here would never reach a client. What the schema DOES
    carry is the structural half -- the two optional selectors, their bounds, and
    ``additionalProperties: false``. The "exactly one" rule was previously learnable only
    by tripping this validator; the pick a shared ``stage_code`` resolves to is disclosed
    per response as a limitation, since ``stage_code`` is not unique (927 en
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
    """Parameters for ``search_stages``.

    ``query`` is length-capped free text; an exact ``stage_code`` match is
    ranked first by the tool. ``server`` optionally scopes to one region.
    ``limit`` is bounded to the window (default 10, max 50).
    """

    query: str = Field(min_length=1, max_length=MAX_QUERY_LEN)
    server: Region | None = None
    limit: int = Field(default=SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT)


class GetStageInput(_StageSelector):
    """Parameters for ``get_stage``.

    The heavy sections are opt-in: ``include_map`` (tile grid), ``include_routes``
    and ``include_spawns`` each default ``False`` so the default response stays
    small. ``include_map`` returns the grid as one compact per-row block
    -- a whole board fits one response, so it takes no page cursor; an
    over-budget board is omitted with a limitation. ``include_routes`` and
    ``include_spawns`` are paged through their **own** bounds -- ``routes_page`` /
    ``spawns_page`` -- so a client can hold the whole stage in one call yet page a
    large section (e.g. the spawn timeline) without shifting the others off.
    Every page is bounded, so no opted-in payload ever returns an unbounded slice.

    ``include_map_image`` (default ``False``) adds a render-own SVG of the
    stage grid -- a DERIVED image drawn from the stored typed grid data,
    never third-party art and never a raw image-asset URL reference. An over-budget
    board is omitted with a limitation rather than an oversized payload.
    """

    include_map: bool = False
    include_routes: bool = False
    include_spawns: bool = False
    include_map_image: bool = False
    routes_page: PageParams = Field(default_factory=PageParams)
    spawns_page: PageParams = Field(default_factory=PageParams)


class AnalyzeStageInput(_StageSelector):
    """Parameters for ``analyze_stage``.

    Selects a stage (region + one selector) and the evidence ``depth``. Every
    depth still returns the evidence-backed observations; deeper levels add
    detail, bounded by the response cap.
    """

    depth: AnalysisDepth = "standard"


class GetStageDropsInput(_StageSelector):
    """Parameters for ``get_stage_drops``.

    Selects a stage (region + one selector) whose penguin drop-rate cache to
    report. ``include_efficiency`` opts into the deterministic farming
    observations (sanity per item); off by default so the base response is the
    compact drop facts + provenance + expiry. Reuses the shared region +
    exactly-one-selector gate, so a drop lookup is region-attributed and
    unambiguous like the other stage tools.
    """

    include_efficiency: bool = False
