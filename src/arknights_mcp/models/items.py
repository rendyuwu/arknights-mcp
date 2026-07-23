"""Bounded input model for ``get_item_drops`` (§T30; §T104; §V5/§V19/§V22/§V60).

An item's drop-across-stages comparison is region-attributed (§V5) and looked up
by the item's unique ``game_id`` (the §V60 reverse of ``get_stage_drops``). The
string cap keeps a crafted id from carrying an oversized blob (§V18).

Unlike a stage's few item-drops, the reverse view is unbounded: a common material
drops across ~100-200 permanent stages, so both growable lists -- the per-stage
drop facts and the ranked efficiency observations -- page through their **own**
bounded :class:`~arknights_mcp.models.common.PageParams` (§V22/§V19, B21). The page
bounds surface in the tool ``inputSchema`` exactly as validated.
"""

from __future__ import annotations

from pydantic import Field

from arknights_mcp.models.common import MAX_ID_LEN, PageParams, Region, StrictModel


class GetItemDropsInput(StrictModel):
    """Parameters for ``get_item_drops`` (§I; §V19/§V22/§V60).

    ``server`` is mandatory so the comparison is region-attributed and en/cn are
    never silently mixed (§V5); ``game_id`` is the unique item key (§V18 cap).
    ``include_efficiency`` opts into the deterministic §T103 ranked farming
    observations (sanity per item); off by default so the base response is the
    compact per-stage drop facts + provenance + expiry (§V22).

    The two modes carry different per-stage shapes (§T161/B82 -- the ranking subsumes
    the stage rows so the response never lists the same stages twice). Without
    ``include_efficiency``, ``stages_page`` pages the raw per-stage drop facts
    (stage-code order). With it, the ranked observation is the single per-stage list and
    ``efficiency_page`` pages it (ascending sanity per item), while ``stages_page`` no
    longer applies. Neither cursor ever yields an unbounded slice (§V19/§V22, B21). The
    service ranks + computes the stale verdict + provenance over the FULL set before
    slicing, so page 1 is always the most-efficient N and the global order holds across
    pages.
    """

    server: Region
    game_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    include_efficiency: bool = False
    stages_page: PageParams = Field(default_factory=PageParams)
    efficiency_page: PageParams = Field(default_factory=PageParams)
