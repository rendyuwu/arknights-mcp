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

from arknights_mcp.instructions import (
    BLACKBOARD_KEY_ENTRIES,
    BLACKBOARD_KEY_GLOSSARY,
    SERVER_INSTRUCTIONS,
)
from arknights_mcp.mcp.resources import build_default_resources
from arknights_mcp.mcp.tool_registry import MAX_TOOL_DESCRIPTION_CHARS
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.mcp.tools._enum_legend import (
    ENUM_LEGENDS,
    OPEN_ENUM_LIMITATIONS,
    TOOL_ENUM_LEGEND_FIELDS,
)
from arknights_mcp.mcp.tools._shared import (
    ATTACK_RANGE_DENIED_NOTE,
    BLACKBOARD_GLOSSARY_POINTER,
    CONFIDENCE_SCALE_NOTE,
    DB_UNAVAILABLE_ACTION,
    ENEMY_STAT_SCALE_NOTE,
    LEVEL_VARIANT_NOTE,
    MODULE_CHANGE_DEDUP_NOTE,
    SEARCH_COVERAGE_POINTER,
    SEARCH_COVERAGE_URI,
    STAGE_MAP_GUIDE_POINTER,
    STAGE_MAP_GUIDE_URI,
)
from arknights_mcp.mcp.tools.drops import _ITEM_NO_DROPS_LIMITATION, _ITEM_NOT_FOUND_ACTION
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


#: The client-facing surfaces: every string these modules build is either published in a
#: schema or emitted into an envelope a client reads.
_CLIENT_TEXT_ROOTS = ("mcp", "services", "analyzers")


def _client_facing_strings() -> list[tuple[str, int, str]]:
    """Every string LITERAL the client-facing modules build, as ``(file, line, text)``.

    §T203: the parametrized check above reads a hand-written tuple of eight
    ``suggested_action`` constants. That list is the bug, not the check -- §V71 (a) was
    written for errors, so when §V108 extended it to limitations, two limitations that
    had acquired a bare CLI imperative were invisible to it (``_STALE_LIMITATION`` told
    the client to "re-sync the penguin drop source", ``range_grid`` told it to "Run
    `arknights-mcp sync`"). Same enumeration failure as §T208's CI module list and
    §T196's five per-surface null rollouts: a rule policed by a list someone must
    remember to extend is a rule with a hole in it.

    So this walks the source instead. An f-string is rendered with ``{}`` standing in
    for its interpolations, and its own Constant children are skipped so a spliced
    string is judged once, whole, rather than once per fragment. Docstrings are skipped:
    they are documentation for maintainers, not text any client receives.
    """
    found: list[tuple[str, int, str]] = []
    for root in _CLIENT_TEXT_ROOTS:
        base = Path(__file__).resolve().parents[2] / "src" / "arknights_mcp" / root
        for path in sorted(base.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            skip: set[int] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.JoinedStr):
                    skip.update(id(c) for c in ast.walk(node) if c is not node)
                if isinstance(
                    node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    first = node.body[0] if node.body else None
                    if (
                        isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)
                    ):
                        skip.add(id(first.value))
            for node in ast.walk(tree):
                if id(node) in skip:
                    continue
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    text = node.value
                elif isinstance(node, ast.JoinedStr):
                    text = "".join(
                        v.value
                        if isinstance(v, ast.Constant) and isinstance(v.value, str)
                        else "{}"
                        for v in node.values
                    )
                else:
                    continue
                rel = path.relative_to(base.parents[2]).as_posix()
                found.append((rel, node.lineno, text))
    return found


def test_every_client_string_naming_the_cli_asks_the_admin() -> None:
    # §V71 (a)/§V28 swept MECHANICALLY (§T203): the client cannot run the admin CLI, so
    # ANY client-facing string naming it -- suggested_action, limitation, observation,
    # description alike -- must ask the admin rather than instruct the caller.
    offenders = [
        f"{path}:{line}: {text[:120]}"
        for path, line, text in _client_facing_strings()
        if "arknights-mcp" in text and "ask the server admin to run" not in text.lower()
    ]
    assert offenders == [], "client text issues a CLI command the client cannot run: " + str(
        offenders
    )


def test_cli_naming_sweep_is_non_degenerate() -> None:
    # §V96: a sweep that matched nothing would pass no matter how the text drifted. The
    # eight hand-listed actions above are a floor on what it must reach.
    swept = [text for _, _, text in _client_facing_strings() if "arknights-mcp" in text]
    assert len(swept) >= len(_CLI_ACTIONS)


@pytest.mark.parametrize(
    ("action", "tool"),
    [
        (_STAGE_NOT_FOUND_ACTION, "search_stages"),
        (_ENEMY_NOT_FOUND_ACTION, "search_entities"),
        (_OPERATOR_NOT_FOUND_ACTION, "search_entities"),
        (_MODULE_NOT_FOUND_ACTION, "search_entities"),
        (_DROPS_NOT_FOUND_ACTION, "search_stages"),
        # §V60/B91: a resolved item with zero drop cache (craft/synthesis-only) points at
        # get_data_status (a freshness self-check the client CAN call), not an admin re-sync.
        # T198/§V106 (b) moved that text from a not_found suggested_action onto the ``ok``
        # envelope's limitation; the §V71 (a) rule it must satisfy did not move with it.
        (_ITEM_NO_DROPS_LIMITATION, "get_data_status"),
    ],
)
def test_entity_not_found_action_names_an_mcp_tool(action: str, tool: str) -> None:
    # §V71 (a): a not_found next step names an MCP-callable tool the client CAN invoke.
    assert tool in action


def test_resolved_no_drops_action_is_not_a_cli_resync() -> None:
    # §V60/B91: the craft/synthesis-only empty answer must NOT hint an admin re-sync --
    # the item has no drop to fetch, so a sync would mislead as "the cache is unsynced".
    action = _ITEM_NO_DROPS_LIMITATION
    assert "arknights-mcp" not in action
    assert "download" not in action.lower() and "scrape" not in action.lower()


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
    # §V84/§T169: the blackboard-key glossary (which glosses the second-valued keys)
    # lives once in the server instructions now, not in each tool description.
    assert "duration = effect length in seconds" in BLACKBOARD_KEY_GLOSSARY
    assert "interval = interval in seconds" in BLACKBOARD_KEY_GLOSSARY
    assert BLACKBOARD_KEY_GLOSSARY in SERVER_INSTRUCTIONS


def test_drop_rate_and_times_glossed_in_drop_descriptions() -> None:
    # §V71 (e)/B87: drop_rate reads like a probability but is expected items per run
    # (quantity / times); times is the sample run count. BOTH drop tools gloss BOTH so a
    # client never reads 0.1192 as an 11.9% chance.
    for name in ("get_stage_drops", "get_item_drops"):
        desc = _desc(name)
        assert "expected number of items per run" in desc, name
        assert "not a probability" in desc, name
        assert "sample run count" in desc, name


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


# --- §V75/B68: sibling-search ranking divergence documented in BOTH descriptions ---


def test_search_sibling_ranking_divergence_documented() -> None:
    # §V75 (B68): search_stages ranks an exact stage-code match first; search_entities
    # does not. The divergence must be documented in BOTH sibling descriptions so a
    # client never discovers it only by a miss -- search_stages states its exact-code
    # rule, and search_entities cross-refs search_stages for a stage code.
    entities = _desc("search_entities")
    stages = _desc("search_stages")
    assert "search_stages" in entities  # cross-ref to the sibling tool
    assert "stage code" in entities.lower()
    assert "exact stage-code match is ranked first" in stages


# --- §V79/B81: overlapping module tools cross-ref each other in BOTH descriptions ---


def test_module_tool_overlap_cross_ref_documented() -> None:
    # §V79 (B81): compare_operator_modules and get_operator(include_modules) share a
    # payload surface (same per-level module bundles). BOTH descriptions must name the
    # sibling + state when to prefer it, so a client never double-fetches or picks the
    # wrong tool -- get_operator points to compare_operator_modules for side-by-side
    # module levels, and compare_operator_modules points back to get_operator for the
    # full kit.
    get_op = _desc("get_operator")
    compare = _desc("compare_operator_modules")
    assert "compare_operator_modules" in get_op  # cross-ref to the sibling tool
    assert "get_operator" in compare  # cross-ref back
    assert "include_modules" in compare  # names the overlapping surface + when-to-prefer


# --- §V84/B89: the blackboard glossary has ONE home + descriptions point to it ---


def test_blackboard_glossary_single_home_with_pointers() -> None:
    # §V84 (B89): the ~1KB glossary was embedded in BOTH get_operator +
    # compare_operator_modules descriptions = 2x every-session context cost. It now lives
    # once in the shared entries home, and each emitting description carries only a short
    # pointer -- no >=500-char block is duplicated across the two descriptions.
    assert BLACKBOARD_KEY_GLOSSARY in SERVER_INSTRUCTIONS
    assert len(BLACKBOARD_KEY_GLOSSARY) >= 500  # the block §V84 forbids duplicating
    for name in ("get_operator", "compare_operator_modules"):
        desc = _desc(name)
        assert BLACKBOARD_GLOSSARY_POINTER in desc, name  # the one-line pointer
        assert BLACKBOARD_KEY_GLOSSARY not in desc, name  # not the glossary itself
    # §V71 (b): the pointer is client-facing, so it carries no internal cite/jargon.
    assert "§" not in BLACKBOARD_GLOSSARY_POINTER
    assert not _BUG_CITE.search(BLACKBOARD_GLOSSARY_POINTER)


def test_glossary_pointer_names_a_client_fetchable_home() -> None:
    # §V84 (B144, T194): the pointer used to say "provided in this server's instructions".
    # ``instructions`` is an OPTIONAL initialize field a client may drop, and the client
    # that reported this received none -- so for it the pointer named a surface that did
    # not exist and the §V65 (c) grounding path evaporated silently. The home must be
    # reachable FROM THE TOOL CALL: an MCP resource.
    assert "arknights://glossary/blackboard" in BLACKBOARD_GLOSSARY_POINTER
    assert "instructions" not in BLACKBOARD_GLOSSARY_POINTER
    # The named URI is really registered, and really serves the glossary (§V37 one home):
    # a pointer to an unregistered URI would dangle exactly like the old one.
    resources = build_default_resources(
        _no_conn, registry=load_source_registry(REGISTRY), mode="local"
    )
    uris = {str(r.uri) for r in resources.list_resources()}
    assert "arknights://glossary/blackboard" in uris
    body = json.loads(resources.read("arknights://glossary/blackboard").contents[0].text)
    assert body["status"] == "ok"
    served = {tuple(e["keys"]): e["meaning"] for e in body["data"]["entries"]}
    assert served == {keys: meaning for keys, meaning in BLACKBOARD_KEY_ENTRIES}


# --- §V104/B142 (T194) + §V104 (b)/B158 (T207): every emitted enum states its DOMAIN ---

#: The enum-valued wire fields this server emits, mapped to ``(tool, values)``. Each
#: value set was COUNTED against the shipped en+cn build (§V96: a domain is counted, not
#: guessed) -- ``difficulty`` is the case that proves it, since three descriptions named
#: four values while the wire emits five (``SIX_STAR``: 76 main-story rows).
#:
#: §T207 moved the HOME, never the requirement (§V111 b): §V104 (b) sanctions a static
#: response-side ``*_legend`` as an EQUAL home for an OUTPUT domain, so every token here
#: must now appear in the legend the emitting tool hoists rather than in its description.
#: The move is what let §V71 (f) get a number at all -- §V104 and §V71 (f) were writing to
#: the same bounded string with no rule saying which yields (B158).
_ENUM_DOMAINS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("get_stage", "difficulty", ("NORMAL", "FOUR_STAR", "SIX_STAR", "TOUGH", "EASY")),
    ("search_stages", "difficulty", ("NORMAL", "FOUR_STAR", "SIX_STAR", "TOUGH", "EASY")),
    ("search_entities", "difficulty", ("NORMAL", "FOUR_STAR", "SIX_STAR", "TOUGH", "EASY")),
    (
        "get_stage",
        "stage_type",
        ("MAIN", "SUB", "ACTIVITY", "DAILY", "CAMPAIGN", "CLIMB_TOWER", "SPECIAL_STORY", "GUIDE"),
    ),
    ("get_enemy", "enemy_class", ("NORMAL", "ELITE", "BOSS")),
    ("get_enemy", "motion_type", ("WALK", "FLY")),
    ("analyze_stage", "enemy_class", ("NORMAL", "ELITE", "BOSS")),
    ("get_operator", "skill_type", ("AUTO", "MANUAL", "PASSIVE")),
    (
        "get_operator",
        "sp_type",
        ("INCREASE_WITH_TIME", "INCREASE_WHEN_ATTACK", "INCREASE_WHEN_TAKEN_DAMAGE"),
    ),
    ("get_operator", "duration_type", ("NONE", "AMMO")),
    (
        "get_operator",
        "profession",
        ("PIONEER", "WARRIOR", "TANK", "SNIPER", "CASTER", "MEDIC", "SUPPORT", "SPECIAL"),
    ),
    ("get_operator", "position", ("MELEE", "RANGED")),
    (
        "get_stage_drops",
        "item_type",
        (
            "MATERIAL",
            "CHIP",
            "CARD_EXP",
            "RECRUIT_TAG",
            "ACTIVITY_ITEM",
            "FURN",
            "TEMP",
            "ARKPLANNER",
            "LGG_SHD",
        ),
    ),
    (
        "get_item_drops",
        "item_type",
        (
            "MATERIAL",
            "CHIP",
            "CARD_EXP",
            "RECRUIT_TAG",
            "ACTIVITY_ITEM",
            "FURN",
            "TEMP",
            "ARKPLANNER",
            "LGG_SHD",
        ),
    ),
    (
        "get_banners",
        "rule_type",
        (
            "NORMAL",
            "SINGLE",
            "DOUBLE",
            "LINKAGE",
            "LIMITED",
            "SPECIAL",
            "ATTAIN",
            "BACKFLOW",
            "CLASSIC",
            "CLASSIC_DOUBLE",
            "CLASSIC_ATTAIN",
            "FESCLASSIC",
        ),
    ),
)


@pytest.mark.parametrize(("tool", "field", "values"), _ENUM_DOMAINS)
def test_emitted_enum_domain_is_published_in_its_legend(
    tool: str, field: str, values: tuple[str, ...]
) -> None:
    # §V104 (B142): an emitted enum whose domain is stated nowhere leaves the client to
    # guess -- get_banners named 4 of 12 rule types, so the ONE classification its
    # description delegated was undecidable for the rest.
    assert field in TOOL_ENUM_LEGEND_FIELDS[tool], (tool, field)
    legend = ENUM_LEGENDS[field]
    # Exhaustive over the emitted values AND carrying nothing the wire does not emit --
    # a legend that invents a value teaches a domain as badly as one that drops one.
    assert sorted(legend) == sorted(values), (tool, field)
    # §V104 (b): the legend DECODES, so no entry may be a bare token with no gloss.
    assert all(gloss for gloss in legend.values()), (tool, field)


def test_input_only_enum_domain_stays_in_the_description() -> None:
    # §V104 (a)/(b) split by WHEN the domain is needed: ``mode`` is not a row value a
    # legend could ride beside -- get_data_status reports one deployment posture per
    # server -- so the description remains its home, and it is short enough to be one.
    desc = _desc("get_data_status")
    for value in ("local", "remote"):
        assert value in desc, value
    assert "mode" in desc


def test_open_enum_domains_are_declared_open() -> None:
    # §V104 (c): a source-defined domain no legend can close must SAY it is open, so a
    # token outside the listed set reads as source-defined, not as an error. §T207 moved
    # the home from the description to the LIMITATION §V104 (c) actually names -- the
    # caveat now rides beside the legend it qualifies.
    for field in ("item_type", "rule_type"):
        text = OPEN_ENUM_LIMITATIONS[field]
        assert "may grow" in text, field
        assert "not as an error" in text, field
    # get_announcements: category is the publisher's own grouping, never a fixed set. It
    # has no legend (the domain cannot be enumerated at all), so the description states it.
    assert "category is the feed's own grouping token" in _desc("get_announcements")


def test_sp_type_mixed_encoding_is_disclosed_as_open() -> None:
    # §V104 (c)/§V99 (B157): sp_type carries BOTH named tokens and a bare numeric code
    # (``8`` on 1145 rows of the promoted build). Disclosure makes the value legal but not
    # decidable, so this is the FLOOR, not the resolution -- and the legend must NOT
    # invent a name for the numeric arm (§V29/§V96 forbid guessing it).
    text = OPEN_ENUM_LIMITATIONS["sp_type"]
    assert "8" in text
    assert "never given a fabricated meaning" in text
    assert "8" not in ENUM_LEGENDS["sp_type"]
    # §T208: the disclosure is a floor, so it also routes to what IS decidable for those
    # skills -- the named sibling and the level's own SP numbers (§V108 class: say where
    # the answer lives instead of stopping at "cannot be decoded").
    for neighbour in ("skill_type", "sp_cost", "initial_sp"):
        assert neighbour in text, neighbour


def test_scale_bearing_stats_state_their_scale() -> None:
    # §V104 extends §V71 (e) from units to SCALES: res/move_speed/weight sat on the same
    # stat block as attack_interval (documented "in seconds") with nothing said, so
    # "res: 80" could be a percentage or a flat value. §T207 moved the home from both
    # descriptions to a standing limitation -- a scale is read beside its number.
    assert "res is arts damage reduction in percent" in ENEMY_STAT_SCALE_NOTE
    assert "move_speed is in tiles per second" in ENEMY_STAT_SCALE_NOTE
    assert "shift-resistance rank" in ENEMY_STAT_SCALE_NOTE


def test_denied_attack_radius_is_disclosed_as_an_answer_not_a_gap() -> None:
    # §V114 (b)/(c) (B161): for the enemies whose source states "no attack radius", the
    # generic absent-field sentence ("the source carried no such data") is FALSE -- the
    # source answered. The note must say what the source DID, and must not translate the
    # marker into a claim about the enemy's reach, which nothing upstream states.
    assert "states that it has no base attack radius" in ATTACK_RANGE_DENIED_NOTE
    assert "rather than leaving the radius unstated" in ATTACK_RANGE_DENIED_NOTE
    assert "does not say what that means for the enemy's reach" in ATTACK_RANGE_DENIED_NOTE
    # ...and it never claims the data is missing, which is the sentence it replaces.
    assert "no such data" not in ATTACK_RANGE_DENIED_NOTE


def test_confidence_scale_stated_on_every_observation_tool() -> None:
    # §V104: confidence rides every observation (§V6) and had no stated scale, so 0.8 read
    # as a calibrated 80% -- a probability this server never computes. §T207 moved the
    # home from four descriptions to a standing limitation (§V111 a).
    assert "confidence is a 0 to 1 heuristic tier" in CONFIDENCE_SCALE_NOTE
    assert "not a calibrated probability" in CONFIDENCE_SCALE_NOTE


def test_contradictory_enum_reading_is_glossed() -> None:
    # §V104/§V74 (d): duration_type "NONE" ships beside duration: 30 on 10937 skill-level
    # rows in the shipped build. Without a gloss the pair reads as a contradiction. The
    # gloss moved INTO the legend entry, which is the one place the client meets the value.
    gloss = ENUM_LEGENDS["duration_type"]["NONE"]
    assert "the source declares no duration type" in gloss
    assert "not that the skill has no duration" in gloss


def test_moved_blocks_left_every_description() -> None:
    # §V111 (b) is MOVE, never DELETE -- and a move that leaves the text behind is not a
    # move at all, it is a §V37 duplication that keeps billing the budget. Each block
    # below now has exactly one home (a legend, a limitation, or an MCP resource), so it
    # must appear in NO tool description.
    descs = {spec.name: spec.description for spec in _registry().specs()}
    moved = {
        "confidence scale": CONFIDENCE_SCALE_NOTE,
        "enemy stat scales": ENEMY_STAT_SCALE_NOTE,
        "module change dedup": MODULE_CHANGE_DEDUP_NOTE,
    }
    offenders = [
        (label, name) for label, block in moved.items() for name, d in descs.items() if block in d
    ]
    assert offenders == [], offenders


def test_level_variant_join_key_is_named() -> None:
    # §V104/§V69: a spawn's enemy_level_variant is the join key into the enemy's per-tier
    # stat block, and neither side named the other before.
    #
    # §T195/§V111 (a): get_stage's copy MOVED to a limitation on the responses that emit
    # the key (the opt-in spawn rows) to pay for the §V102 selector contract, so it is
    # asserted where it now lives -- see tests/unit/test_stage_selector_ambiguity.py,
    # which drives include_spawns and checks the note arrives beside the values.
    #
    # §T203: analyze_stage's copy moved the same way, to its depth="detailed" limitation
    # (tests/unit/test_view_routing.py). This test used to assert both descriptions on the
    # grounds that they "emit the key unconditionally" -- which was never true of
    # analyze_stage: only the detailed occurrence row carries level_variant, so at
    # summary/standard depth the description glossed a key the response did not have.
    # get_enemy is the one tool that really does emit level variants on every call, so it
    # is the one that still owes the gloss pre-call.
    assert "enemy_level_variant" in _desc("get_enemy")
    assert "enemy_level_variant" in LEVEL_VARIANT_NOTE


# --- §V71 (f)/§V111 (d) (T207, B156): the description budget is a NUMBER, over ALL ---

#: The pre-call fact each description must LEAD with (§V111 c): the selector or query
#: parameter a caller has to supply to call the tool at all. A truncating client keeps the
#: head of the string, so what a caller needs BEFORE the call has to be in it -- that is
#: the ordering mitigation T194 shipped for get_operator, generalized to every tool.
_LEADING_SELECTOR: dict[str, str] = {
    "search_entities": "name",
    "search_stages": "stage code",
    "get_stage": "stage_code",
    "get_enemy": "game_id",
    "get_operator": "game_id",
    "compare_operator_modules": "game_id",
    "analyze_stage": "stage_code",
    "get_stage_drops": "stage_code",
    "get_item_drops": "item game_id",
    "get_announcements": "region",
    "get_banners": "region",
    "get_data_status": "build",
    "get_data_sources": "source",
}


def test_every_registered_description_is_within_budget() -> None:
    # §V111 (d)/B156: §V71 (f) used to be qualitative, so B145 could be closed by halving
    # ONE tool while the never-audited get_stage became the new longest string on the
    # server -- "the halving moved the crown, not the problem". A cap that is a NUMBER
    # measured over EVERY registered tool is the only form that cannot be satisfied by
    # moving the crown. Registration enforces it too; this pins the measurement itself and
    # names the offender, which a ToolRegistryError at import time cannot do for all 13.
    over = [
        (spec.name, len(spec.description))
        for spec in _registry().specs()
        if len(spec.description) > MAX_TOOL_DESCRIPTION_CHARS
    ]
    assert over == [], f"over the {MAX_TOOL_DESCRIPTION_CHARS}-char budget: {over}"


def test_every_tool_is_measured_by_the_budget_guard() -> None:
    # Guard the guard: the test above passes vacuously if the registry ever comes back
    # empty, and _LEADING_SELECTOR below silently skips a tool it does not name.
    names = {spec.name for spec in _registry().specs()}
    assert len(names) >= 13
    assert names == set(_LEADING_SELECTOR)


def test_pre_call_facts_lead_every_description() -> None:
    # §V111 (c): ORDER is the fallback mitigation for a truncating client -- the facts a
    # caller needs to form the call must survive the cut, so they lead. Checked against
    # the FIRST sentence, which is the part any truncation keeps.
    for spec in _registry().specs():
        first = spec.description.split(". ")[0]
        assert _LEADING_SELECTOR[spec.name] in first, (spec.name, first)


def test_no_shared_block_is_duplicated_across_two_descriptions() -> None:
    # §V84: ">=2 tool descriptions must not duplicate a >=500-char identical block". Only
    # the blackboard glossary was ever guarded, so a 788-char coverage note sat
    # byte-identical in BOTH search descriptions until T207 measured for it. This checks
    # every pair, so the next such block fails on arrival instead of years later.
    bar = 500
    descs = [(spec.name, spec.description) for spec in _registry().specs()]
    offenders: list[tuple[str, str, str]] = []
    for i, (name_a, a) in enumerate(descs):
        windows = {a[j : j + bar] for j in range(len(a) - bar + 1)}
        for name_b, b in descs[i + 1 :]:
            shared = next((w for w in windows if w in b), None)
            if shared is not None:
                offenders.append((name_a, name_b, shared[:80]))
    assert offenders == [], f"{bar}+ char blocks duplicated across descriptions: {offenders}"


def test_moved_guides_point_at_registered_fetchable_resources() -> None:
    # §V84/B144: a pointer must name a surface reachable FROM THE TOOL CALL. Text moved
    # off the description surface to an MCP resource is only MOVED (§V111 b) if that
    # resource really exists and really serves it -- otherwise the fact is deleted and the
    # pointer dangles, the exact failure B144 reported for the instructions-based glossary.
    resources = build_default_resources(
        _no_conn, registry=load_source_registry(REGISTRY), mode="local"
    )
    uris = {str(r.uri) for r in resources.list_resources()}
    for uri, pointer in (
        (STAGE_MAP_GUIDE_URI, STAGE_MAP_GUIDE_POINTER),
        (SEARCH_COVERAGE_URI, SEARCH_COVERAGE_POINTER),
    ):
        assert uri in pointer, uri
        assert uri in uris, uri
        body = json.loads(resources.read(uri).contents[0].text)
        assert body["status"] == "ok", uri
        assert body["data"]["entries"], uri
        # §V71 (b): the served guide is client-facing text like any other.
        served = " ".join(e["note"] for e in body["data"]["entries"])
        assert "§" not in served and not _BUG_CITE.search(served), uri


def test_pointers_are_carried_by_the_tools_that_lost_the_text() -> None:
    # A move is only complete when the tool whose description shed the block names its new
    # home. get_stage shed the map-reading guide; BOTH search tools shed the coverage note.
    assert STAGE_MAP_GUIDE_POINTER in _desc("get_stage")
    for name in ("search_entities", "search_stages"):
        assert SEARCH_COVERAGE_POINTER in _desc(name), name


# --- §V71 (f)/B145 (T194): get_operator's description is no longer the fattest ---


def test_get_operator_description_dropped_the_response_side_mechanics() -> None:
    # B145: >half the description described skin/image-ref MECHANICS that belong beside
    # the values (the image_refs_legend + the standing limitations now carry them), and it
    # was long enough that a client tool-listing truncated it mid-sentence.
    desc = _desc("get_operator")
    for gone in (
        "e0 (elite-0)",
        "marks art belonging to an alternate playable form",
        "paid marks an outfit the source flags as purchasable",
        "An absent optional ref field means default art",
        "do not infer mechanics from a key name alone",  # BLACKBOARD_LIMITATION says it
    ):
        assert gone not in desc, gone
    # The pre-call facts a caller needs survive a truncating client: they lead.
    head = desc[:640]
    assert "game_id char_002_amiya" in head  # a worked example selector
    assert "include_provenance only adds a second copy" in head  # the flag's real effect
    assert "compare_operator_modules" in head  # which sibling tool to prefer


# --- §V47/B146 (T194): get_announcements no longer primes the client for nothing ---


def test_announcements_description_drops_the_stale_disabled_claim() -> None:
    # B146: the description asserted "The announcement source is disabled by default, so a
    # region with no imported feed returns an empty list" -- false since the M9 review
    # flipped the adapter ENABLED (§V56), and both feeds import on a normal sync. It
    # primed a client to expect nothing and disbelieve what it got.
    desc = _desc("get_announcements")
    assert "disabled by default" not in desc
    registry = load_source_registry(REGISTRY)
    for source_id in ("arknights_global_official_news", "arknights_cn_official_news"):
        entry = registry.get(source_id)
        assert entry is not None and entry.enabled, source_id
    # The honest replacement points at a tool the client CAN call to check (§V71 a).
    assert "get_data_status" in desc


# --- §V71 (b)/B131 (T194): the registry is the THIRD client-facing surface ---


def test_registry_public_text_carries_no_internal_cites() -> None:
    # §V71 (b) SCOPE += REGISTRY DATA: config/data_sources.toml ships VERBATIM to a client
    # through get_data_sources, so a cite parked in `purpose` IS a wire cite. T137 swept
    # the CODE constants after B62 and never looked at this surface (the one-surface-not-
    # all class). ``internal_ref`` is the one explicitly ignorable carve-out -- everything
    # else a client reads must be cite-free.
    offenders: list[tuple[str, str, str]] = []
    for entry in load_source_registry(REGISTRY).entries.values():
        for field, value in entry.public_view().items():
            if field == "internal_ref" or not isinstance(value, str):
                continue
            for marker in _CITE_JARGON:
                if marker in value:
                    offenders.append((entry.source_id, field, marker))
            if _BUG_CITE.search(value):
                offenders.append((entry.source_id, field, "bug-cite"))
    assert offenders == [], f"internal cites in public registry text: {offenders}"


def test_registry_decision_ids_live_in_internal_ref() -> None:
    # The carve-out is real, not a way to delete the bookkeeping: the founder-decision /
    # ADR / milestone refs that used to sit inside `purpose` prose still exist, in the
    # field a client can skip whole.
    registry = load_source_registry(REGISTRY)
    news = registry.get("arknights_global_official_news")
    images = registry.get("arknights_game_resource")
    assert news is not None and images is not None
    assert "D14" in news.internal_ref
    assert "ADR 0008" in images.internal_ref
    # ... and they are gone from the prose a client reads.
    assert "D14" not in news.purpose
    assert "ADR 0008" not in images.purpose and "§V63" not in images.purpose


def test_registry_status_tokens_are_glossed_for_the_client() -> None:
    # B131: the *_status fields are snake_case machine tokens on a self-described public
    # registry, two of them whole sentences compressed into one token. The tokens stay
    # machine-readable; what changed is that the tool now tells the client what they are.
    registry = load_source_registry(REGISTRY)
    primary = registry.get("arknights_assets_gamedata")
    images = registry.get("arknights_game_resource")
    assert primary is not None and images is not None
    assert primary.permission_status == "not_granted"
    assert images.license_status == "agpl_3_0_code_only"
    desc = _desc("get_data_sources")
    assert "short machine tokens" in desc
    assert "internal_ref" in desc and "ignore it" in desc


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
