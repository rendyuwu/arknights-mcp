"""Bounded input-model tests.

Primary invariant: heavy sections opt-in, pagination bounded; touches search
limit / page_size bounds (on the wire), untrusted-string caps +
``extra="forbid"``, and region ``en``|``cn`` only.
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import Field, ValidationError

from arknights_mcp.models import (
    PAGE_SIZE_MAX,
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
    AnalyzeStageInput,
    CompareOperatorModulesInput,
    GetEnemyInput,
    GetStageInput,
    PageParams,
    SearchEntitiesInput,
    SearchStagesInput,
    tool_input_schema,
)
from arknights_mcp.models.common import StrictModel
from arknights_mcp.models.operators import ModuleLevel

# --- search limit bounded (default 10, max 50, rejected out of range) ---


def test_search_limit_defaults_to_ten() -> None:
    assert SearchEntitiesInput(query="dusk").limit == SEARCH_DEFAULT_LIMIT == 10


def test_search_limit_max_accepted() -> None:
    assert SearchEntitiesInput(query="dusk", limit=SEARCH_MAX_LIMIT).limit == 50


def test_search_limit_over_max_rejected() -> None:
    with pytest.raises(ValidationError):
        SearchEntitiesInput(query="dusk", limit=SEARCH_MAX_LIMIT + 1)


def test_search_limit_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        SearchEntitiesInput(query="dusk", limit=0)


def test_search_stages_shares_the_same_window() -> None:
    with pytest.raises(ValidationError):
        SearchStagesInput(query="4-4", limit=SEARCH_MAX_LIMIT + 1)


# --- get_stage heavy sections are opt-in (default off) ---


def test_get_stage_include_flags_default_off() -> None:
    got = GetStageInput(server="en", stage_code="4-4")
    assert (got.include_map, got.include_routes, got.include_spawns) == (False, False, False)


# --- pagination bounded (page >= 1, page_size <= PAGE_SIZE_MAX) ---


def test_page_size_max_accepted() -> None:
    assert PageParams(page_size=PAGE_SIZE_MAX).page_size == 100


def test_page_size_over_max_rejected() -> None:
    with pytest.raises(ValidationError):
        PageParams(page_size=PAGE_SIZE_MAX + 1)


def test_page_below_one_rejected() -> None:
    with pytest.raises(ValidationError):
        PageParams(page=0)


# --- untrusted strings length-capped; unknown params rejected ---


def test_query_over_length_cap_rejected() -> None:
    with pytest.raises(ValidationError):
        SearchEntitiesInput(query="x" * 201)


def test_empty_query_rejected() -> None:
    with pytest.raises(ValidationError):
        SearchEntitiesInput(query="")


def test_unknown_parameter_rejected() -> None:
    with pytest.raises(ValidationError):
        SearchEntitiesInput(query="dusk", limitt=5)  # type: ignore[call-arg]


# --- region is en|cn only; a fact tool requires one ---


def test_bad_region_rejected() -> None:
    with pytest.raises(ValidationError):
        GetEnemyInput(server="jp", game_id="enemy_1007_slime")  # type: ignore[arg-type]


def test_fact_tool_requires_server() -> None:
    with pytest.raises(ValidationError):
        GetEnemyInput(game_id="enemy_1007_slime")  # type: ignore[call-arg]


def test_search_region_optional() -> None:
    assert SearchEntitiesInput(query="dusk").server is None


# --- the extra-locale (ja/ko) NAME-alias filter is RETIRED ---


def test_search_entities_rejects_locale_param() -> None:
    # The `locale` filter is gone (founder 2026-07-23, EN+CN only).
    # The model is `extra="forbid"`, so a client still sending `locale` is
    # rejected at the gate -- never silently accepted or ignored.
    for value in ("ja", "ko", "en", "zh"):
        with pytest.raises(ValidationError):
            SearchEntitiesInput(query="dusk", locale=value)  # type: ignore[call-arg]


# --- selector: exactly one of stage_code | game_id ---


def test_both_selectors_rejected() -> None:
    with pytest.raises(ValidationError):
        GetStageInput(server="en", stage_code="4-4", game_id="main_04-04")


def test_neither_selector_rejected() -> None:
    with pytest.raises(ValidationError):
        GetStageInput(server="en")


def test_analyze_stage_depth_default_standard() -> None:
    got = AnalyzeStageInput(server="en", game_id="main_04-04")
    assert got.depth == "standard"


# --- module compare: levels bounded to {1,2,3}, deduped + sorted ---


def test_compare_levels_default_all_three() -> None:
    got = CompareOperatorModulesInput(server="en", game_id="char_002_amiya")
    assert got.levels == (1, 2, 3)


def test_compare_levels_deduped_and_sorted() -> None:
    got = CompareOperatorModulesInput(server="en", game_id="char_002_amiya", levels=(3, 1, 1))
    assert got.levels == (1, 3)


def test_compare_invalid_level_rejected() -> None:
    with pytest.raises(ValidationError):
        CompareOperatorModulesInput(server="en", game_id="char_002_amiya", levels=(4,))


def test_compare_empty_levels_rejected() -> None:
    with pytest.raises(ValidationError):
        CompareOperatorModulesInput(server="en", game_id="char_002_amiya", levels=())


def test_compare_levels_domain_is_in_the_published_schema() -> None:
    """The {1,2,3} domain reaches the CLIENT, not just the validator.

    It lived in prose plus a runtime check, so the only way to learn it was to spend a
    call and read the error -- avoidable, since ``page_size``'s ``maximum: 100`` proves
    the same schema can carry a bound. The enum is generated from ``ModuleLevel`` itself,
    so the published domain and the check that reports it cannot drift.
    """
    schema = tool_input_schema(CompareOperatorModulesInput)
    assert schema["properties"]["levels"]["items"]["enum"] == [1, 2, 3]
    assert set(get_args(ModuleLevel)) == set(schema["properties"]["levels"]["items"]["enum"])


def test_compare_out_of_domain_level_reports_the_whole_domain() -> None:
    """The disclosure does not cost the model-grade message.

    The Literal alone would reject per ITEM ("Input should be 1, 2 or 3"), naming neither
    the field's whole domain nor which values offended; the domain check runs first and
    keeps the sentence a client can act on.
    """
    with pytest.raises(ValidationError) as excinfo:
        CompareOperatorModulesInput(server="en", game_id="char_002_amiya", levels=(0, 5))
    assert "levels must be a subset of {1, 2, 3}; got [0, 5]" in str(excinfo.value)


# --- bounds surface on the wire (generated inputSchema) ---


def test_input_schema_declares_search_limit_bound() -> None:
    schema = tool_input_schema(SearchEntitiesInput)
    assert schema["properties"]["limit"]["maximum"] == SEARCH_MAX_LIMIT
    assert schema["properties"]["limit"]["minimum"] == 1
    # extra="forbid" -> closed object; a client cannot add fields.
    assert schema["additionalProperties"] is False


def test_input_schema_declares_page_size_bound() -> None:
    schema = tool_input_schema(PageParams)
    assert schema["properties"]["page_size"]["maximum"] == PAGE_SIZE_MAX


def test_input_schema_declares_query_length_cap() -> None:
    schema = tool_input_schema(SearchEntitiesInput)
    assert schema["properties"]["query"]["maxLength"] == 200


def test_input_schema_strips_description_keyword_but_keeps_a_field_named_description() -> None:
    # The strip drops the auto-published schema *description* keyword (a class
    # docstring / Field description carries internal cites), but it must key off schema
    # *position*, not the literal string -- a model field literally named "description" is
    # a property NAME, not a keyword, so it has to survive on the wire. Otherwise the
    # published inputSchema would omit a parameter the model still enforces
    # (extra="forbid"), breaking the wire<->model parity.
    class _Model(StrictModel):
        """Docstring with an internal cite that must never reach the wire."""

        description: str = Field(max_length=10, description="internal cite; must be stripped")

    schema = tool_input_schema(_Model)
    # the class-docstring schema-level description keyword is gone ...
    assert "description" not in schema
    # ... but the property literally named "description" survives with its bound + required
    # entry (a client can still discover the parameter the model enforces) ...
    assert schema["properties"]["description"]["maxLength"] == 10
    assert "description" in schema["required"]
    # ... while that property's OWN Field-description keyword is still stripped (position).
    assert "description" not in schema["properties"]["description"]
