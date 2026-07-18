"""Bounded input model for ``get_enemy`` (§T30; §T35; §V5).

An enemy fact is region-attributed (§V5) and looked up by its unique ``game_id``.
The string cap keeps a crafted id from carrying an oversized blob (§V18).
"""

from __future__ import annotations

from pydantic import Field

from arknights_mcp.models.common import MAX_ID_LEN, Region, StrictModel


class GetEnemyInput(StrictModel):
    """Parameters for ``get_enemy`` (§I; §V5).

    ``server`` is mandatory so the returned facts are region-attributed and en/cn
    are never silently mixed (§V5); ``game_id`` is the unique enemy key (§V18 cap).
    """

    server: Region
    game_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
