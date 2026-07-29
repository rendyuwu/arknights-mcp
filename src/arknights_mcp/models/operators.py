"""Bounded input models for the operator tools (§T30; §T44/§T45; §V5/§V22).

Covers ``get_operator`` (§T44) and ``compare_operator_modules`` (§T45). Heavy
operator sections (phases, skills, talents, modules) are opt-in include flags that
default ``False`` so the default response stays small (§V22); a lightweight
``summary`` defaults on. Region attribution rides the envelope unconditionally
(§V5); the in-``data`` provenance echo is opt-in (``include_provenance`` defaults
``False``) so the default response carries the snapshot exactly once (§V66/B64).
Module comparison is bounded to the three real module levels (§V19).
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import Field, field_validator

from arknights_mcp.models.common import MAX_ID_LEN, Region, StrictModel

#: Facts-only vs facts + deterministic module observations (§V7 conservative).
CompareMode = Literal["facts_only", "with_observations"]

#: One in-game module upgrade tier (§T45: 1/2/3, not operator potential). Typed as a
#: ``Literal`` so the domain reaches the PUBLISHED JSON Schema as ``enum: [1, 2, 3]``
#: (§V107): a client that only has the schema could otherwise learn the domain solely
#: by spending a call on the resulting error (B149).
ModuleLevel = Literal[1, 2, 3]

#: Valid module levels in-game, derived FROM :data:`ModuleLevel` so the published enum
#: and the check that reports it cannot drift (§V37 one home).
_VALID_MODULE_LEVELS = frozenset(get_args(ModuleLevel))


class GetOperatorInput(StrictModel):
    """Parameters for ``get_operator`` (§I; §V5/§V22).

    ``server`` + ``game_id`` address one operator, region-attributed (§V5). The
    heavy sections (``include_phases``/``skills``/``talents``/``modules``) default
    ``False`` (§V22); ``include_summary`` defaults ``True`` so a fact always carries
    a small summary. Region provenance rides the envelope unconditionally (§V5);
    ``include_provenance`` toggles an in-``data`` echo and defaults ``False`` so the
    default response carries the snapshot exactly once (§V66/B64).
    """

    server: Region
    game_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    include_summary: bool = True
    include_phases: bool = False
    include_skills: bool = False
    include_talents: bool = False
    include_modules: bool = False
    include_provenance: bool = False


class CompareOperatorModulesInput(StrictModel):
    """Parameters for ``compare_operator_modules`` (§I; §V7).

    Compares one operator's modules at the requested module ``levels`` (§T45:
    subset of {1, 2, 3}, deduped, non-empty; defaults to all three). ``mode``
    chooses facts only vs facts + conservative deterministic observations (§V7).
    ``levels`` is typed :data:`ModuleLevel`, so the {1, 2, 3} domain ships in the
    published schema as ``enum`` rather than in prose alone (§V107/B149).
    """

    server: Region
    game_id: str = Field(min_length=1, max_length=MAX_ID_LEN)
    levels: tuple[ModuleLevel, ...] = (1, 2, 3)
    mode: CompareMode = "facts_only"

    @field_validator("levels", mode="before")
    @classmethod
    def _reject_out_of_domain(cls, value: Any) -> Any:
        """Report an out-of-domain level in the domain's own words (§V71 c; B149).

        Runs BEFORE the ``ModuleLevel`` coercion on purpose. The Literal is what
        publishes the ``enum`` (§V107), but its own rejection reads as a per-item
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
        # Dedup + sort so the comparison order is deterministic (§V14).
        return tuple(sorted(set(value)))
