"""§T134 client-facing text pins (§V71/§V48/§V23; B60).

Every MCP tool title, description, and published input schema -- plus the
``suggested_action`` strings a tool returns -- is part of the client contract
(§V21), read by an MCP client LLM. These tests pin the §V71 rules and extend the
§V48 doc-terminology pin across the WHOLE tool surface (not just one tool):

* (b) no internal spec cite (``§V`` / ``§T`` / ``§B``) or maintainer jargon
  ("degenerate", "asymmetric-broken") reaches the client, and no raw pydantic framing
  / ``errors.pydantic.dev`` URL (B60) -- in a title, description, OR the published
  input schema (the model docstring pydantic would otherwise publish verbatim);
* (a) a ``suggested_action`` naming an admin CLI command (which the client cannot run,
  §V28) is phrased "ask the server admin to run ..."; an entity lookup names the
  MCP-callable ``search_*`` tool a client CAN invoke;
* (d) a numeric field with a unit states the unit (seconds) in the description;
* (e/f) the drop/banner list descriptions are short sentences, not a clause chain.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.mcp.tools._shared import DB_UNAVAILABLE_ACTION
from arknights_mcp.mcp.tools.drops import _ITEM_NOT_FOUND_ACTION
from arknights_mcp.mcp.tools.drops import _NOT_FOUND_ACTION as _DROPS_NOT_FOUND_ACTION
from arknights_mcp.mcp.tools.enemy import _NOT_FOUND_ACTION as _ENEMY_NOT_FOUND_ACTION
from arknights_mcp.mcp.tools.module_compare import _NOT_FOUND_ACTION as _MODULE_NOT_FOUND_ACTION
from arknights_mcp.mcp.tools.operator import _NOT_FOUND_ACTION as _OPERATOR_NOT_FOUND_ACTION
from arknights_mcp.mcp.tools.search import _DATA_STALE_ACTION
from arknights_mcp.mcp.tools.stage import _NOT_FOUND_ACTION as _STAGE_NOT_FOUND_ACTION
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: Internal-only markers that must never reach a client (§V71 (b), B60).
_CITE_JARGON = ("§", "degenerate", "asymmetric-broken", "errors.pydantic.dev")
#: A bug cite like ``B60`` / ``B18`` carries no ``§`` sigil, so pin it separately.
_BUG_CITE = re.compile(r"\bB\d{1,3}\b")


def _no_conn():  # type: ignore[no-untyped-def]
    raise RuntimeError("no connection needed for description/schema inspection")


def _registry():  # type: ignore[no-untyped-def]
    # image_refs_enabled=True exercises the widest text surface (the image-ref
    # sentences on get_operator/get_enemy/get_banners).
    return build_tool_registry(
        _no_conn,
        registry=load_source_registry(REGISTRY),
        mode="local",
        image_refs_enabled=True,
    )


def _published_texts() -> list[tuple[str, str]]:
    """Every published client-facing string: ``(label, text)`` for each surface."""
    out: list[tuple[str, str]] = []
    for spec in _registry().specs():
        tool = spec.to_mcp_tool()
        out.append((f"{tool.name}.title", tool.title))
        out.append((f"{tool.name}.description", tool.description))
        # The published input schema (pydantic would embed the model docstring here).
        out.append((f"{tool.name}.inputSchema", json.dumps(tool.inputSchema, ensure_ascii=False)))
    return out


# --- (b): no internal cites / jargon / framework framing in published text -----


def test_no_published_text_carries_internal_cites_or_jargon() -> None:
    offenders: list[tuple[str, str]] = []
    for label, text in _published_texts():
        for marker in _CITE_JARGON:
            if marker in text:
                offenders.append((label, marker))
        if _BUG_CITE.search(text):
            offenders.append((label, "bug-cite"))
    assert offenders == [], f"internal cites/jargon leaked to the client: {offenders}"


def test_input_schema_carries_no_prose_description() -> None:
    # §V71 (b): the model docstring is NOT published as the schema description (that is
    # where the §V cites lived); the structural contract is preserved instead.
    for spec in _registry().specs():
        schema = spec.to_mcp_tool().inputSchema
        assert "description" not in schema, spec.name
        # Structural bounds still ride the wire (§V18/§V19/§V22).
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False


# --- (a): suggested_action names an MCP tool or asks the admin, never a bare CLI ---

#: Every ``suggested_action`` that references an admin CLI command (``arknights-mcp
#: ...``) across the tool surface. The client cannot run these (§V28), so each must be
#: phrased as an "ask the server admin" instruction (§V71 (a)).
_CLI_ACTIONS = (
    DB_UNAVAILABLE_ACTION,
    _DATA_STALE_ACTION,
    _STAGE_NOT_FOUND_ACTION,
    _ENEMY_NOT_FOUND_ACTION,
    _OPERATOR_NOT_FOUND_ACTION,
    _MODULE_NOT_FOUND_ACTION,
    _DROPS_NOT_FOUND_ACTION,
    _ITEM_NOT_FOUND_ACTION,
)


@pytest.mark.parametrize("action", _CLI_ACTIONS)
def test_admin_cli_action_is_phrased_as_ask_the_admin(action: str) -> None:
    # §V71 (a)/§V28: a CLI command the client cannot run is phrased "ask the server
    # admin to run ...", never a bare command the client would try to invoke.
    assert "arknights-mcp" in action
    assert "ask the server admin to run" in action


@pytest.mark.parametrize(
    ("action", "tool"),
    [
        (_STAGE_NOT_FOUND_ACTION, "search_stages"),
        (_ENEMY_NOT_FOUND_ACTION, "search_entities"),
        (_OPERATOR_NOT_FOUND_ACTION, "search_entities"),
        (_MODULE_NOT_FOUND_ACTION, "search_entities"),
        (_DROPS_NOT_FOUND_ACTION, "search_stages"),
    ],
)
def test_entity_not_found_action_names_an_mcp_tool(action: str, tool: str) -> None:
    # §V71 (a): a not_found next step names an MCP-callable tool the client CAN invoke.
    assert tool in action


def test_no_cli_action_suggests_query_time_download() -> None:
    # §V24: a suggested action never hints a query-time download/scrape fallback.
    for action in _CLI_ACTIONS:
        lowered = action.lower()
        assert "download" not in lowered and "scrape" not in lowered


# --- (d): numeric fields with a unit state the unit (seconds) ------------------


def _desc(name: str) -> str:
    for spec in _registry().specs():
        if spec.name == name:
            return spec.description
    raise AssertionError(f"tool {name!r} not registered")


def test_unit_fields_state_seconds_in_descriptions() -> None:
    # §V71 (d): duration / interval / spawn_time / attack_interval are in seconds.
    enemy = _desc("get_enemy")
    assert "attack interval in seconds" in enemy
    stage = _desc("get_stage")
    assert "spawn_time" in stage and "seconds" in stage
    analyze = _desc("analyze_stage")
    assert "seconds" in analyze
    operator = _desc("get_operator")  # the blackboard-key glossary
    assert "duration = effect length in seconds" in operator
    assert "interval = interval in seconds" in operator


# --- (e/f): the list descriptions are short sentences, not a clause chain ------


@pytest.mark.parametrize("name", ["get_banners", "get_item_drops", "get_stage_drops"])
def test_list_descriptions_are_short_sentences(name: str) -> None:
    # §V71 (e/f): split into short sentences -- no semicolon clause chains, and no
    # single sentence long enough to bury a caveat under client context pressure.
    desc = _desc(name)
    assert ";" not in desc, name
    sentences = [s.strip() for s in desc.split(". ") if s.strip()]
    assert len(sentences) >= 4, name  # genuinely split, not one run-on
    longest = max(len(s) for s in sentences)
    assert longest <= 240, (name, longest)


# --- (b) T137/B62: RUNTIME-emitted client strings carry no cites/jargon either --

# T134 pinned the PUBLISHED surface (titles/descriptions/schemas). B62 showed a
# RUNTIME-emitted constant -- the banner limitation -- still shipped "(§V62)"/"(§V18)"
# to the client, because the published-surface scan never looked at the emitted
# limitation/observation/suggested_action strings. These tests extend the §V71 (b) pin
# to those runtime strings across the layers that build them.

#: The layers that assemble runtime-emitted client-facing text (limitations,
#: observations, suggested_actions, messages, captions, glossaries).
_EMITTING_LAYERS = (
    REPO_ROOT / "src" / "arknights_mcp" / "services",
    REPO_ROOT / "src" / "arknights_mcp" / "analyzers",
    REPO_ROOT / "src" / "arknights_mcp" / "mcp" / "tools",
    REPO_ROOT / "src" / "arknights_mcp" / "mcp" / "envelopes.py",
)

#: A module-level constant whose name carries one of these tokens holds client-facing
#: emitted text, so its string value(s) must be free of internal cites/jargon (§V71 (b)).
#: Keyed on the NAME so the scan auto-extends to a future emitted constant and cannot
#: silently pass by scanning nothing (the B62 weak-test lesson -- a floor guards it too).
#: The service-layer page/limit ``ValueError`` args (which do carry "§V19") are inline in
#: functions, not module-level constants, and are swallowed by ``run_guarded`` into a
#: fixed ``internal_error`` before any client sees them, so they are correctly excluded.
_EMITTED_NAME_TOKENS = (
    "LIMITATION",
    "ACTION",
    "MESSAGE",
    "SUMMARY",
    "TITLE",
    "CAPTION",
    "GLOSSARY",
    "NOTE",
    "HINT",
)


def _py_files(paths):  # type: ignore[no-untyped-def]
    for p in paths:
        if p.is_file():
            yield p
        else:
            yield from p.rglob("*.py")


def _string_values(node):  # type: ignore[no-untyped-def]
    """The str value(s) of a constant RHS: a bare str, or a tuple/list of str literals."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [
            elt.value
            for elt in node.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]
    return []


def _emitted_constants() -> list[tuple[str, str]]:
    """Every ``(label, value)`` for a module-level emitted-text constant in the layers."""
    found: list[tuple[str, str]] = []
    for f in _py_files(_EMITTING_LAYERS):
        tree = ast.parse(f.read_text(), filename=str(f))
        for node in tree.body:  # module level only -- not strings inside functions
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
                value = node.value
            else:
                continue
            if value is None:
                continue
            for name in names:
                if not any(tok in name.upper() for tok in _EMITTED_NAME_TOKENS):
                    continue
                for s in _string_values(value):
                    found.append((f"{f.name}:{node.lineno}:{name}", s))
    return found


def test_runtime_emitted_constants_carry_no_cites_or_jargon() -> None:
    # §V71 (b)/B62: extend the T134 published-surface pin to RUNTIME-emitted client
    # constants (limitations, suggested_actions, messages, ...), where the banner
    # limitation cite leak lived. Cites stay in code comments, never in the string.
    constants = _emitted_constants()
    # Floor so the scan cannot pass by matching nothing (the B62 weak-test lesson).
    assert len(constants) >= 15, f"emitted-constant scan matched too few ({len(constants)})"
    offenders: list[tuple[str, str]] = []
    for label, text in constants:
        for marker in _CITE_JARGON:
            if marker in text:
                offenders.append((label, marker))
        if _BUG_CITE.search(text):
            offenders.append((label, "bug-cite"))
    assert offenders == [], f"internal cites/jargon in runtime-emitted constants: {offenders}"


def test_builder_emitted_limitations_carry_no_cites() -> None:
    # Some client-facing limitations are built by a function, not a module constant, so
    # the convention scan above cannot see them; pin the known builders directly (§V71).
    from arknights_mcp.mcp.tools._shared import absent_field_limitation
    from arknights_mcp.services.stage_map_render import _oversize_limitation

    emitted = [_oversize_limitation(), *absent_field_limitation(("attack_type", "immunities"))]
    for text in emitted:
        for marker in _CITE_JARGON:
            assert marker not in text, (text, marker)
        assert not _BUG_CITE.search(text), text
