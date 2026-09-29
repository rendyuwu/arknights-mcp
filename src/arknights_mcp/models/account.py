"""Bounded input models for the account roster tools (ADR 0020).

``server`` is required on every call; account sync covers ``en`` only, and the
service answers ``unsupported_server`` for ``cn``. Both lists page through the
bounded :class:`~arknights_mcp.models.common.PageParams`.
"""

from __future__ import annotations

from pydantic import Field

from arknights_mcp.models.common import MAX_ID_LEN, PageParams, Region, StrictModel


class GetMyRosterInput(StrictModel):
    server: Region
    min_rarity: int | None = Field(default=None, ge=1, le=6)
    min_elite: int | None = Field(default=None, ge=0, le=2)
    page: PageParams = Field(default_factory=PageParams)


class GetMyOperatorInput(StrictModel):
    server: Region
    game_id: str = Field(min_length=1, max_length=MAX_ID_LEN)


class GetMyInventoryInput(StrictModel):
    server: Region
    page: PageParams = Field(default_factory=PageParams)
