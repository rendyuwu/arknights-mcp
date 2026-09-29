"""Bounded input models for the operator tools.

Covers ``get_operator``, ``compare_operator_modules``, and ``find_operators``. Heavy
operator sections (phases, skills, talents, modules) are opt-in include flags that
default ``False`` so the default response stays small; a lightweight
``summary`` defaults on. Region attribution rides the envelope unconditionally;
the in-``data`` provenance echo is opt-in (``include_provenance`` defaults
``False``) so the default response carries the snapshot exactly once.
Module comparison is bounded to the three real module levels.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import Field, field_validator, model_validator

from arknights_mcp.models.common import MAX_ID_LEN, PageParams, Region, StrictModel

#: Facts-only vs facts + deterministic module observations (conservative).
CompareMode = Literal["facts_only", "with_observations"]

#: One in-game module upgrade tier (1/2/3, not operator potential). Typed as a
#: ``Literal`` so the domain reaches the PUBLISHED JSON Schema as ``enum: [1, 2, 3]``:
#: a client that only has the schema could otherwise learn the domain solely
#: by spending a call on the resulting error.
ModuleLevel = Literal[1, 2, 3]

#: Valid module levels in-game, derived FROM :data:`ModuleLevel` so the published enum
#: and the check that reports it cannot drift (one home).
_VALID_MODULE_LEVELS = frozenset(get_args(ModuleLevel))

#: A base (RIIC) facility: the ``building_data`` ``roomType`` domain (ADR 0021).
RoomType = Literal[
    "CONTROL",
    "DORMITORY",
    "HIRE",
    "MANUFACTURE",
    "MEETING",
    "POWER",
    "TRADING",
    "TRAINING",
    "WORKSHOP",
]

#: ``find_operators`` needs a narrowing filter; ``collab=False`` alone would page
#: every operator. The service raises the same message.
FIND_FILTER_REQUIRED = "set room_type or faction, or collab to true"


class GetOperatorInput(StrictModel):
    """Parameters for ``get_operator``.

    ``server`` + ``game_id`` address one operator, region-attributed. The
    heavy sections (``include_phases``/``skills``/``talents``/``modules``) default
    ``False``; ``include_summary`` defaults ``True`` so a fact always carries
    a small summary. Region provenance rides the envelope unconditionally;
    ``include_provenance`` toggles an in-``data`` echo and defaults ``False`` so the
    default response carries the snapshot exactly once.
    """

    server: Region
    game_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    include_summary: bool = True
    include_phases: bool = False
    include_skills: bool = False
    include_talents: bool = False
    include_modules: bool = False
    include_base_skills: bool = False
    include_provenance: bool = False


class CompareOperatorModulesInput(StrictModel):
    """Parameters for ``compare_operator_modules``.

    Compares one operator's modules at the requested module ``levels`` (a
    subset of {1, 2, 3}, deduped, non-empty; defaults to all three). ``mode``
    chooses facts only vs facts + conservative deterministic observations.
    ``levels`` is typed :data:`ModuleLevel`, so the {1, 2, 3} domain ships in the
    published schema as ``enum`` rather than in prose alone.
    """

    server: Region
    game_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    levels: tuple[ModuleLevel, ...] = (1, 2, 3)
    mode: CompareMode = "facts_only"

    @field_validator("levels", mode="before")
    @classmethod
    def _reject_out_of_domain(cls, value: Any) -> Any:
        """Report an out-of-domain level in the domain's own words.

        Runs BEFORE the ``ModuleLevel`` coercion on purpose. The Literal is what
        publishes the ``enum``, but its own rejection reads as a per-item
        type error; this keeps the message that names the whole domain and the
        offending values. Anything that is not a sequence of ints falls through
        untouched, so the type error stays pydantic's to report.
        """
        if not isinstance(value, list | tuple):
            return value
        levels = [item for item in value if isinstance(item, int) and not isinstance(item, bool)]
        if len(levels) != len(value):
            return value
        invalid = sorted(set(levels) - _VALID_MODULE_LEVELS)
        if invalid:
            raise ValueError(f"levels must be a subset of {{1, 2, 3}}; got {invalid}")
        return value

    @field_validator("levels", mode="after")
    @classmethod
    def _valid_levels(cls, value: tuple[ModuleLevel, ...]) -> tuple[ModuleLevel, ...]:
        if not value:
            raise ValueError("levels must not be empty")
        # Dedup + sort so the comparison order is deterministic.
        return tuple(sorted(set(value)))


class FindOperatorsInput(StrictModel):
    """Parameters for ``find_operators`` (ADR 0021).

    Needs ``room_type`` or ``faction``, or ``collab=True``; ``collab=False`` only
    narrows another filter. Pages through the bounded ``PageParams``.
    """

    server: Region
    room_type: RoomType | None = None
    faction: str | None = Field(default=None, min_length=1, max_length=MAX_ID_LEN)
    collab: bool | None = None
    page: PageParams = Field(default_factory=PageParams)

    @model_validator(mode="after")
    def _require_filter(self) -> FindOperatorsInput:
        if self.room_type is None and self.faction is None and self.collab is not True:
            raise ValueError(FIND_FILTER_REQUIRED)
        return self
