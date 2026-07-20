"""Bounded input model for ``get_item_drops`` (§T30; §T104; §V5/§V60).

An item's drop-across-stages comparison is region-attributed (§V5) and looked up
by the item's unique ``game_id`` (the §V60 reverse of ``get_stage_drops``). The
string cap keeps a crafted id from carrying an oversized blob (§V18).
"""

from __future__ import annotations

from pydantic import Field

from arknights_mcp.models.common import MAX_ID_LEN, Region, StrictModel


class GetItemDropsInput(StrictModel):
    """Parameters for ``get_item_drops`` (§I; §V60).

    ``server`` is mandatory so the comparison is region-attributed and en/cn are
    never silently mixed (§V5); ``game_id`` is the unique item key (§V18 cap).
    ``include_efficiency`` opts into the deterministic §T103 ranked farming
    observations (sanity per item); off by default so the base response is the
    compact per-stage drop facts + provenance + expiry (§V22).
    """

    server: Region
    game_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    include_efficiency: bool = False
