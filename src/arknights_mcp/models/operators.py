"""Bounded input models for the operator tools (§T30; §T44/§T45; §V5/§V22).

Covers ``get_operator`` (§T44) and ``compare_operator_modules`` (§T45). Heavy
operator sections (phases, skills, talents, modules) are opt-in include flags that
default ``False`` so the default response stays small (§V22); ``provenance`` and a
lightweight ``summary`` default on so a fact always carries its region attribution
(§V5). Module comparison is bounded to the three real module levels (§V19).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from arknights_mcp.models.common import MAX_ID_LEN, Region, StrictModel

#: Facts-only vs facts + deterministic module observations (§V7 conservative).
CompareMode = Literal["facts_only", "with_observations"]

#: Valid module levels in-game (§T45: module upgrade tiers 1/2/3, not operator potential).
_VALID_MODULE_LEVELS = frozenset({1, 2, 3})


class GetOperatorInput(StrictModel):
    """Parameters for ``get_operator`` (§I; §V5/§V22).

    ``server`` + ``game_id`` address one operator, region-attributed (§V5). The
    heavy sections (``include_phases``/``skills``/``talents``/``modules``) default
    ``False`` (§V22); ``include_summary`` and ``include_provenance`` default ``True``
    so a fact always carries a small summary + its region provenance (§V5).
    """

    server: Region
    game_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    include_summary: bool = True
    include_phases: bool = False
    include_skills: bool = False
    include_talents: bool = False
    include_modules: bool = False
    include_provenance: bool = True


class CompareOperatorModulesInput(StrictModel):
    """Parameters for ``compare_operator_modules`` (§I; §V7).

    Compares one operator's modules at the requested module ``levels`` (§T45:
    subset of {1, 2, 3}, deduped, non-empty; defaults to all three). ``mode``
    chooses facts only vs facts + conservative deterministic observations (§V7).
    """

    server: Region
    game_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    levels: tuple[int, ...] = (1, 2, 3)
    mode: CompareMode = "facts_only"

    @field_validator("levels", mode="after")
    @classmethod
    def _valid_levels(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value:
            raise ValueError("levels must not be empty")
        invalid = sorted(set(value) - _VALID_MODULE_LEVELS)
        if invalid:
            raise ValueError(f"levels must be a subset of {{1, 2, 3}}; got {invalid}")
        # Dedup + sort so the comparison order is deterministic (§V14).
        return tuple(sorted(set(value)))
