"""Parameterized read-only repositories (§V2).

The sanctioned SQL surface for the domain services: parameterized ``SELECT``
only, values bound via ``?`` placeholders, never string interpolation.
"""

from arknights_mcp.db.repositories.base import Repository
from arknights_mcp.db.repositories.stages import (
    StageEnemyRow,
    StageRepository,
    StageRow,
)

__all__ = ["Repository", "StageEnemyRow", "StageRepository", "StageRow"]
