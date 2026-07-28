"""§T29 tool-registry tests: one shared registry (§V14), read-only enforced
(§V2/§V28), unique names, and correct ``mcp.types.Tool`` projection."""

from __future__ import annotations

import pytest
from mcp.types import Tool

from arknights_mcp.mcp.envelopes import ResponseEnvelope, ok
from arknights_mcp.mcp.tool_registry import (
    MAX_TOOL_DESCRIPTION_CHARS,
    ToolRegistry,
    ToolRegistryError,
    ToolSpec,
    build_default_registry,
)


def _handler(**_: object) -> ResponseEnvelope:
    return ok({"ran": True})


def _spec(name: str = "get_thing", *, read_only: bool = True) -> ToolSpec:
    return ToolSpec(
        name=name,
        title="Get Thing",
        description="Read a thing.",
        handler=_handler,
        input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
        read_only=read_only,
    )


def test_default_registry_starts_empty() -> None:
    reg = build_default_registry()
    assert reg.names() == ()
    assert reg.to_mcp_tools() == []


def test_register_and_lookup_preserves_order() -> None:
    reg = ToolRegistry()
    reg.register(_spec("search_entities"))
    reg.register(_spec("get_stage"))

    assert reg.names() == ("search_entities", "get_stage")
    assert "get_stage" in reg
    assert reg.get("get_stage").title == "Get Thing"


def test_duplicate_name_is_rejected() -> None:
    reg = ToolRegistry()
    reg.register(_spec("get_stage"))
    with pytest.raises(ToolRegistryError):
        reg.register(_spec("get_stage"))


def test_non_read_only_tool_is_rejected() -> None:
    # §V2/§V28: MCP tools are read-only; admin/mutating ops stay CLI-only.
    reg = ToolRegistry()
    with pytest.raises(ToolRegistryError):
        reg.register(_spec("purge_source", read_only=False))


def test_over_budget_description_is_rejected() -> None:
    # §V71 (f)/§V111 (d) (§T207, B156): the description budget is enforced at
    # registration for the same reason read-only is -- a rule only a test knows about is
    # one a new tool can ship past, and B156's failure was precisely that §V71 (f) "read
    # satisfied" while the worst offender had never been audited.
    reg = ToolRegistry()
    over = ToolSpec(
        name="get_verbose",
        title="Get Thing",
        description="x" * (MAX_TOOL_DESCRIPTION_CHARS + 1),
        handler=_handler,
    )
    with pytest.raises(ToolRegistryError, match="over the"):
        reg.register(over)
    assert "get_verbose" not in reg


def test_description_exactly_at_budget_is_accepted() -> None:
    # The bound is inclusive: the cap is a budget to spend, not to stay under.
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="get_verbose",
            title="Get Thing",
            description="x" * MAX_TOOL_DESCRIPTION_CHARS,
            handler=_handler,
        )
    )
    assert "get_verbose" in reg


def test_over_budget_rejection_names_the_alternative_homes() -> None:
    # §V111 (b): over budget is closed by MOVING a fact to a named home, never by
    # deleting a mandated one -- so the error has to say which homes exist, or the next
    # reader closes it the one way the spec forbids.
    reg = ToolRegistry()
    spec = ToolSpec(
        name="get_verbose",
        title="Get Thing",
        description="x" * (MAX_TOOL_DESCRIPTION_CHARS + 1),
        handler=_handler,
    )
    with pytest.raises(ToolRegistryError) as excinfo:
        reg.register(spec)
    message = str(excinfo.value)
    for home in ("input schema", "response legend", "limitation", "MCP resource"):
        assert home in message, home
    assert "never delete a mandated one" in message


def test_projection_stamps_read_only_hint() -> None:
    reg = ToolRegistry()
    reg.register(_spec("get_stage"))

    tools = reg.to_mcp_tools()
    assert len(tools) == 1
    tool = tools[0]
    assert isinstance(tool, Tool)
    assert tool.name == "get_stage"
    assert tool.inputSchema == {"type": "object", "properties": {"id": {"type": "string"}}}
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False


def test_no_param_tool_defaults_to_empty_object_schema() -> None:
    spec = ToolSpec(
        name="get_data_status",
        title="Data status",
        description="Report active snapshot status.",
        handler=_handler,
    )
    assert spec.input_schema == {"type": "object", "properties": {}}
