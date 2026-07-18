"""Parameterized read-only repositories (§V2).

The sanctioned SQL surface for the domain services: parameterized ``SELECT``
only, values bound via ``?`` placeholders, never string interpolation.
"""

from arknights_mcp.db.repositories.base import Repository
from arknights_mcp.db.repositories.enemies import (
    EnemyLevelRow,
    EnemyRepository,
    EnemyRow,
)
from arknights_mcp.db.repositories.search import SearchHitRow, SearchRepository
from arknights_mcp.db.repositories.stages import (
    StageEnemyRow,
    StageRepository,
    StageRow,
)

__all__ = [
    "EnemyLevelRow",
    "EnemyRepository",
    "EnemyRow",
    "Repository",
    "SearchHitRow",
    "SearchRepository",
    "StageEnemyRow",
    "StageRepository",
    "StageRow",
]
