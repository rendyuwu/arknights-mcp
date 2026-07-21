"""§T30 bounded input-model tests.

Primary invariant §V22 (heavy sections opt-in, pagination bounded); touches §V19
(search limit / page_size bounds land on the wire), §V18 (untrusted-string caps +
``extra="forbid"``) and §V5 (region ``en``|``cn`` only).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

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

# --- §V19/§V22: search limit bounded (default 10, max 50, rejected out of range) ---


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


# --- §V22: get_stage heavy sections are opt-in (default off) ---


def test_get_stage_include_flags_default_off() -> None:
    got = GetStageInput(server="en", stage_code="4-4")
    assert (got.include_map, got.include_routes, got.include_spawns) == (False, False, False)


# --- §V19/§V22: pagination bounded (page >= 1, page_size <= PAGE_SIZE_MAX) ---


def test_page_size_max_accepted() -> None:
    assert PageParams(page_size=PAGE_SIZE_MAX).page_size == 100


def test_page_size_over_max_rejected() -> None:
    with pytest.raises(ValidationError):
        PageParams(page_size=PAGE_SIZE_MAX + 1)


def test_page_below_one_rejected() -> None:
    with pytest.raises(ValidationError):
        PageParams(page=0)


# --- §V18: untrusted strings length-capped; unknown params rejected ---


def test_query_over_length_cap_rejected() -> None:
    with pytest.raises(ValidationError):
        SearchEntitiesInput(query="x" * 201)


def test_empty_query_rejected() -> None:
    with pytest.raises(ValidationError):
        SearchEntitiesInput(query="")


def test_unknown_parameter_rejected() -> None:
    with pytest.raises(ValidationError):
        SearchEntitiesInput(query="dusk", limitt=5)  # type: ignore[call-arg]


# --- §V5: region is en|cn only; a fact tool requires one ---


def test_bad_region_rejected() -> None:
    with pytest.raises(ValidationError):
        GetEnemyInput(server="jp", game_id="enemy_1007_slime")  # type: ignore[arg-type]


def test_fact_tool_requires_server() -> None:
    with pytest.raises(ValidationError):
        GetEnemyInput(game_id="enemy_1007_slime")  # type: ignore[call-arg]


def test_search_region_optional() -> None:
    assert SearchEntitiesInput(query="dusk").server is None


# --- §V57/B50: locale filter domain is the extra-locale tags (ja|ko) ONLY ---


def test_search_locale_extra_locale_tags_accepted() -> None:
    # §V57/B50: the jp/kr NAME-alias tags are the only valid locale filter values.
    assert SearchEntitiesInput(query="dusk", locale="ja").locale == "ja"
    assert SearchEntitiesInput(query="dusk", locale="ko").locale == "ko"


def test_search_locale_defaults_to_none() -> None:
    assert SearchEntitiesInput(query="dusk").locale is None


def test_search_locale_fact_region_locale_rejected() -> None:
    # §V57/B50: a fact-region locale (en/zh) is degenerate (≈ server=) AND
    # asymmetric-broken (only operators self-alias) -> rejected at the model gate,
    # never a silent narrow that keeps operators and drops every enemy.
    for bad in ("en", "zh"):
        with pytest.raises(ValidationError):
            SearchEntitiesInput(query="dusk", locale=bad)  # type: ignore[arg-type]


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


# --- §V18/§V19/§V22: bounds surface on the wire (generated inputSchema) ---


def test_input_schema_declares_search_limit_bound() -> None:
    schema = tool_input_schema(SearchEntitiesInput)
    assert schema["properties"]["limit"]["maximum"] == SEARCH_MAX_LIMIT
    assert schema["properties"]["limit"]["minimum"] == 1
    # extra="forbid" -> closed object; a client cannot add fields (§V18).
    assert schema["additionalProperties"] is False


def test_input_schema_declares_page_size_bound() -> None:
    schema = tool_input_schema(PageParams)
    assert schema["properties"]["page_size"]["maximum"] == PAGE_SIZE_MAX


def test_input_schema_declares_query_length_cap() -> None:
    schema = tool_input_schema(SearchEntitiesInput)
    assert schema["properties"]["query"]["maxLength"] == 200
