"""§V121 (a) made executable: the widest legal request, read off the schema (T217 c).

B167 and B169 shipped one tool apart for the same reason -- every cap test drove a tool at
its *defaults*, so a payload that only overruns with all four include flags on and
``page_size=100`` was never rendered. §V121 (a) fixed the definition ("∀ include flag on, ∀
knob @max"); this module checks that :mod:`arknights_mcp.mcp.cap_pressure` derives that
shape from each tool's own published ``inputSchema`` rather than from a list somebody has to
remember to update.

The layers here are all offline -- schema shapes and declaration bookkeeping. The counted
figures they bracket are re-measured over the promoted build in
``tests/contract/test_frame_pressure.py``.

The load-bearing assertion is the negative one. A widener that quietly skips a parameter it
does not understand is worse than no widener: it reports full coverage over a narrowed
request space, which is the exact state the surface was in when both bugs shipped. So
:func:`~arknights_mcp.mcp.cap_pressure.classify_inputs` raises, and the arms that raise are
fired here from synthetic schemas rather than merely declared (§V113 b).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from arknights_mcp.mcp.cap_pressure import (
    CAP_PRESSURE_BASIS,
    CREEP_CEILING,
    FRAME_PRESSURE,
    PRESSURE_BY_TOOL,
    STALE_FLOOR,
    CapPressureError,
    EnumKnob,
    InputKind,
    ToolFramePressure,
    classify_inputs,
    enum_filter_variants,
    widest_knobs,
)
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_TOML = REPO_ROOT / "config" / "data_sources.toml"


@pytest.fixture
def tool_registry() -> Iterator[ToolRegistry]:
    """The real shared registry (§V14). Handlers are never invoked -- the schema and name
    enumeration needs no query -- so an empty connection is enough."""
    conn = sqlite3.connect(":memory:")
    try:
        yield build_tool_registry(
            lambda: conn,
            registry=load_source_registry(REGISTRY_TOML),
            mode="stdio",
            image_refs_enabled=True,
        )
    finally:
        conn.close()


def _blank(tool: str = "synthetic") -> ToolFramePressure:
    return ToolFramePressure(
        tool=tool,
        selectors=(),
        filters=(),
        enum_knobs=(),
        sheds=False,
        peak_frame_bytes=1,
        peak_at="-",
        counted="-",
    )


def test_only_a_wall_clock_payload_declares_a_volatile_field() -> None:
    # The volatile exemption is a hole in the contract guard's comparison, so it stays as
    # small as the surface makes it: exactly one tool's payload is wall-clock dependent.
    # Its liveness (published AND actually varying) is proven over the build in
    # tests/contract/test_frame_pressure.py; this pins that nothing else claims one.
    exempt = {row.tool: row.volatile for row in FRAME_PRESSURE if row.volatile}
    assert exempt == {"get_data_status": ("generated_at",)}


# --- the declaration is the registry's, not a remembered list -------------------


def test_every_registered_tool_declares_its_frame_pressure(tool_registry: ToolRegistry) -> None:
    # §V120 (f) "⊥ a remembered list of tools", both ways. A registered tool with no row
    # would be swept by nothing; a row naming no registered tool is dead weight that makes
    # the coverage count look larger than the surface it covers (B135's shape).
    registered = set(tool_registry.names())
    declared = set(PRESSURE_BY_TOOL)
    assert registered - declared == set(), "a registered tool declares no cap pressure"
    assert declared - registered == set(), "a cap-pressure row names no registered tool"
    assert len(FRAME_PRESSURE) == len(declared), "duplicate tool row in FRAME_PRESSURE"


def test_the_declaration_order_is_the_registration_order(tool_registry: ToolRegistry) -> None:
    # Not cosmetic: reading the two side by side is how a reviewer notices a missing row,
    # and ``list_tools`` order is itself pinned (§V14).
    assert tuple(row.tool for row in FRAME_PRESSURE) == tool_registry.names()


def test_every_declaration_names_its_basis(tool_registry: ToolRegistry) -> None:
    # §V121 (c): a bare number is not re-checkable. Every row names where it peaked, states
    # its unit (pre-shed frame bytes) and its share of the cap, and the module names the
    # build all of them were counted on.
    assert CAP_PRESSURE_BASIS == "2026-08-13T220624Z-en-cn"
    for row in FRAME_PRESSURE:
        assert row.peak_frame_bytes > 0, row.tool
        assert row.peak_at.strip(), row.tool
        assert "% of cap" in row.counted, row.tool
        assert "frame bytes" in row.counted, row.tool


def test_the_bands_bracket_the_declared_peak() -> None:
    # The pin has to be able to fail in both directions: growth past the ceiling is the
    # tripwire, and a declaration parked far above the real peak is a guard that cannot
    # fail (§V117).
    assert STALE_FLOOR < 1.0 < CREEP_CEILING


# --- the widest legal request is derived from the schema ------------------------


def test_every_registered_input_property_is_classified(tool_registry: ToolRegistry) -> None:
    # The partition must cover the whole published surface of every tool: an unplaced
    # property is a parameter the sweep would leave at its default.
    for spec in tool_registry.specs():
        kinds = classify_inputs(spec.input_schema, PRESSURE_BY_TOOL[spec.name])
        assert set(kinds) == set(spec.input_schema.get("properties", {})), spec.name


def test_every_input_kind_is_live_on_the_real_surface(tool_registry: ToolRegistry) -> None:
    # §V117's other half: a bucket no registered tool lands in is an untested rule, and an
    # untested rule is where the next unclassified parameter would silently land.
    seen: set[InputKind] = set()
    for spec in tool_registry.specs():
        seen |= set(classify_inputs(spec.input_schema, PRESSURE_BY_TOOL[spec.name]).values())
    assert seen == set(InputKind)


def test_the_widest_request_sets_every_flag_and_maxes_every_knob(
    tool_registry: ToolRegistry,
) -> None:
    # Checked against each schema rather than against a copy of the expected dict, so this
    # keeps holding when a tool grows a flag.
    for spec in tool_registry.specs():
        pressure = PRESSURE_BY_TOOL[spec.name]
        args = widest_knobs(spec.input_schema, pressure)
        kinds = classify_inputs(spec.input_schema, pressure)
        properties = spec.input_schema.get("properties", {})
        page_max = spec.input_schema.get("$defs", {}).get("PageParams", {})
        for name, kind in kinds.items():
            if kind is InputKind.FLAG:
                assert args[name] is True, (spec.name, name)
            elif kind is InputKind.PAGE:
                assert args[name]["page_size"] == page_max["properties"]["page_size"]["maximum"]
            elif kind is InputKind.BOUNDED_INT:
                assert args[name] == properties[name]["maximum"], (spec.name, name)
            elif kind is InputKind.SET_KNOB:
                assert args[name] == list(properties[name]["items"]["enum"]), (spec.name, name)
            elif kind is InputKind.ENUM_KNOB:
                assert args[name] in properties[name]["enum"], (spec.name, name)
            else:
                # Identity/selector are the sweep's to supply, an enum filter is its to
                # cross, and a narrowing filter's widest value IS its absence.
                assert name not in args, (spec.name, name, kind)


def test_the_declared_enum_widest_members_match_the_published_domain(
    tool_registry: ToolRegistry,
) -> None:
    # The order of enum members is semantic, not schematic, so the widest one is declared.
    # A new ``depth`` or ``mode`` member therefore fails HERE -- where somebody has to
    # decide whether it is wider -- instead of leaving the sweep one member short in
    # silence, which is the §V117 vocabulary failure one layer out.
    for spec in tool_registry.specs():
        pressure = PRESSURE_BY_TOOL[spec.name]
        kinds = classify_inputs(spec.input_schema, pressure)
        declared = {knob.name: knob for knob in pressure.enum_knobs}
        published = {name for name, kind in kinds.items() if kind is InputKind.ENUM_KNOB}
        assert set(declared) == published, spec.name
        for name, knob in declared.items():
            domain = tuple(spec.input_schema["properties"][name]["enum"])
            assert knob.domain == domain, (spec.name, name)
            assert knob.widest in domain, (spec.name, name)


def test_the_declared_selectors_and_filters_are_published_optional_strings(
    tool_registry: ToolRegistry,
) -> None:
    # No stale names: a selector or filter the tool no longer publishes would silently
    # shrink the set of properties the classifier demands a decision about.
    for spec in tool_registry.specs():
        pressure = PRESSURE_BY_TOOL[spec.name]
        kinds = classify_inputs(spec.input_schema, pressure)
        selectors = {n for n, k in kinds.items() if k is InputKind.SELECTOR}
        filters = {n for n, k in kinds.items() if k is InputKind.WINDOW_FILTER}
        assert selectors == set(pressure.selectors), spec.name
        assert filters == set(pressure.filters), spec.name


def test_enum_filter_variants_cross_the_domain_and_absence(tool_registry: ToolRegistry) -> None:
    # An enum filter changes WHICH rows come back, so absence is not automatically its
    # widest value: ``search_entities`` peaks at ``entity_type=stage`` on the promoted
    # build, above the unfiltered mix.
    spec = tool_registry.get("search_entities")
    variants = enum_filter_variants(spec.input_schema, PRESSURE_BY_TOOL["search_entities"])
    # server (2 members + absent) x entity_type (4 + absent)
    assert len(variants) == 15
    assert {} in variants
    assert {"server": "cn", "entity_type": "stage"} in variants

    plain = tool_registry.get("get_enemy")
    assert enum_filter_variants(plain.input_schema, PRESSURE_BY_TOOL["get_enemy"]) == ({},)


# --- the raising arms, fired rather than declared (§V113 b) ---------------------


def test_an_unclassified_property_raises() -> None:
    # A float knob with no bound: nothing in the schema says what its widest legal value
    # is, and treating that as "leave at the default" is how both bugs shipped.
    schema = {"type": "object", "properties": {"weight": {"type": "number"}}}
    with pytest.raises(CapPressureError, match="does not classify"):
        classify_inputs(schema, _blank())


def test_an_undeclared_optional_string_raises() -> None:
    # A row selector and a narrowing filter look identical in the schema, so an optional
    # string in neither declared list is a question the sweep cannot answer for itself.
    schema = {
        "type": "object",
        "properties": {"code": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}},
    }
    with pytest.raises(CapPressureError, match="neither a row selector nor a narrowing filter"):
        classify_inputs(schema, _blank())


def test_an_unbounded_array_property_raises() -> None:
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
    }
    with pytest.raises(CapPressureError, match="no enumerated item domain"):
        classify_inputs(schema, _blank())


def test_an_enum_knob_with_no_declared_widest_member_raises() -> None:
    schema = {
        "type": "object",
        "properties": {"depth": {"type": "string", "enum": ["a", "b"], "default": "a"}},
    }
    with pytest.raises(CapPressureError, match="declares no widest member"):
        widest_knobs(schema, _blank())


def test_a_dangling_page_ref_raises() -> None:
    schema = {"type": "object", "properties": {"page": {"$ref": "#/$defs/PageParams"}}}
    with pytest.raises(CapPressureError, match="publishes no such"):
        classify_inputs(schema, _blank())


def test_a_declared_widest_member_outside_the_domain_is_still_applied() -> None:
    # Not a raising arm: the domain pin above is what catches this, and it catches it with
    # the tool's real schema in hand. This records that the widener itself does not
    # second-guess the declaration -- one home for the decision (§V37).
    schema = {
        "type": "object",
        "properties": {"depth": {"type": "string", "enum": ["a", "b"], "default": "a"}},
    }
    pressure = ToolFramePressure(
        tool="synthetic",
        selectors=(),
        filters=(),
        enum_knobs=(EnumKnob(name="depth", widest="b", domain=("a", "b")),),
        sheds=False,
        peak_frame_bytes=1,
        peak_at="-",
        counted="-",
    )
    assert widest_knobs(schema, pressure) == {"depth": "b"}
