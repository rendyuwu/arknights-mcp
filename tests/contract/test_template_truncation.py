"""T204: §V109 template-cap guard against the REAL corpus (B154).

The §V18 length cap used to run *before* the §V18/T136 rich-text tag strip, at all
three §V65 (a) call sites. Two things follow from that order, and the second is why
the bug survived a milestone::

    raw      "...deal True damage; <@ba.vup>{atk_scale:0%}</> of ATK to ..."   805 chars
    capped   "...deal True damage; <@b"                                        512 chars
    stripped "...deal True damage; <@b"                                        428 chars

First, the 512 budget is spent on markup the client never sees. Second -- and worse --
the strip pulls the result back *under* the cap, so the stored string carries no
``len == cap`` fingerprint. In the shipped build ``2026-07-26T230656Z`` the longest EN
template is 428 chars and no row is 512 long, yet 349 of 12057 were cut mid-sentence.

That is a §V65 (a) grounding template: the one path closing B56's fabrication hole, and
exactly the text the server instructions tell a client to trust over the raw blackboard
keys. A truncated one reads as a *complete* sentence, so the client confidently states
half a mechanic -- strictly worse than the bare keys the template replaced.

This module is the guard §V109 demands, over real upstream bytes (§V29 class):

* every EN skill / talent / module template imports WHOLE -- zero truncations;
* the corpus post-strip maximum is asserted to sit under ``MAX_TEMPLATE_LENGTH``, so
  upstream growing past the ceiling fails loudly (re-pin or raise the cap) instead of
  silently resuming the mid-sentence cuts;
* a floor on how many templates exceed the old name-class cap, so the corpus is still
  shown to exercise the bug rather than passing vacuously.

CI-only: needs network, gated behind ``ARKMCP_LIVE_UPSTREAM`` like §T68 and the §V97
weld guard. Nothing fetched is persisted (§V16, code-only distribution).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.support import (
    LIVE_UPSTREAM_SKIP_REASON,
    arknights_assets_base_url,
    fetch_upstream_bytes,
    live_upstream_disabled,
)

from arknights_mcp.importers.modules import parse_modules
from arknights_mcp.importers.operators import parse_operators
from arknights_mcp.importers.skills import parse_skills
from arknights_mcp.util.text import (
    DEFAULT_MAX_TEXT_LENGTH,
    MAX_TEMPLATE_LENGTH,
    clean_template_text,
    strip_control_chars,
    strip_richtext_tags,
)

pytestmark = pytest.mark.skipif(live_upstream_disabled(), reason=LIVE_UPSTREAM_SKIP_REASON)

BASE_URL = arknights_assets_base_url("en")

#: Floor on how many real EN templates exceed the old name-class cap once their tags are
#: stripped. These are precisely the texts the pre-T204 order truncated, so if upstream
#: ever drops below this the corpus has stopped exercising B154 and the guard would pass
#: vacuously. At the pinned commit the real count is 141 (of 12057).
MIN_TEMPLATES_OVER_DEFAULT_CAP = 100


def _fetch_table(relative_path: str) -> Any:
    """Fetch + parse one pinned upstream table; never written to disk (§V16)."""
    return json.loads(fetch_upstream_bytes(f"{BASE_URL}/{relative_path}").decode("utf-8"))


def _raw_templates() -> list[tuple[str, str]]:
    """Every raw EN template string in the source, as ``(where, raw)``.

    Read straight off the upstream JSON rather than through a parser, so the expected
    side of the comparison is independent of the code under test.
    """
    out: list[tuple[str, str]] = []
    for game_id, entry in _fetch_table("gamedata/excel/skill_table.json").items():
        for index, level in enumerate(entry.get("levels") or []):
            if isinstance(level, dict) and isinstance(level.get("description"), str):
                out.append((f"skill {game_id} L{index + 1}", level["description"]))
    for game_id, entry in _fetch_table("gamedata/excel/character_table.json").items():
        for ti, talent in enumerate(entry.get("talents") or []):
            if not isinstance(talent, dict):
                continue
            for vi, cand in enumerate(talent.get("candidates") or []):
                if isinstance(cand, dict) and isinstance(cand.get("description"), str):
                    out.append((f"talent {game_id} {ti}.{vi}", cand["description"]))
    return out


#: The source keys the module trait/talent-change bundles carry their template under
#: (``modules._effect_template``); ``overrideDescripton`` is upstream's own misspelling.
_MODULE_TEMPLATE_KEYS = frozenset(
    {"additionalDescription", "overrideDescripton", "upgradeDescription", "description"}
)


def _raw_module_templates(node: Any) -> list[str]:
    """Every raw module-change template string anywhere in ``battle_equip_table``."""
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _MODULE_TEMPLATE_KEYS and isinstance(value, str):
                out.append(value)
            else:
                out.extend(_raw_module_templates(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_raw_module_templates(value))
    return out


def test_real_templates_are_never_truncated() -> None:
    """§V109: no real EN template is cut by the cap, and the cap has headroom."""
    templates = _raw_templates()
    assert templates, "pinned upstream tables produced no templates"

    truncated: list[tuple[str, int, int]] = []
    longest = 0
    over_default_cap = 0
    for where, raw in templates:
        # The full grounding text: the same pipeline with the cap taken off, so the
        # only difference between the two sides can be a truncation.
        full = strip_control_chars(strip_richtext_tags(raw)).strip()
        imported = clean_template_text(raw)
        longest = max(longest, len(imported))
        over_default_cap += len(imported) > DEFAULT_MAX_TEXT_LENGTH
        if imported != full:
            truncated.append((where, len(raw), len(imported)))

    assert not truncated, (
        f"{len(truncated)} of {len(templates)} real EN templates were truncated "
        f"mid-text by the §V65 (a) cap (§V109/B154); first few: {truncated[:5]}"
    )
    assert longest < MAX_TEMPLATE_LENGTH, (
        f"longest real EN template is {longest} chars against a "
        f"MAX_TEMPLATE_LENGTH of {MAX_TEMPLATE_LENGTH}: upstream has grown into the "
        "ceiling and templates are about to be cut mid-sentence again (§V109) -- raise "
        "the cap (and bump FIELD_POLICY_VERSION + TRANSFORM_VERSION), do not re-pin away"
    )
    assert over_default_cap >= MIN_TEMPLATES_OVER_DEFAULT_CAP, (
        f"only {over_default_cap} of {len(templates)} real EN templates exceed the "
        f"name-class cap ({DEFAULT_MAX_TEXT_LENGTH}); the corpus no longer exercises "
        f"B154 (expected >= {MIN_TEMPLATES_OVER_DEFAULT_CAP}) -- re-pin "
        "ARKNIGHTS_ASSETS_COMMIT or re-derive the floor"
    )


def test_parsed_skill_and_talent_templates_route_through_the_shared_home() -> None:
    """§V109/§V37: both operator call sites emit exactly ``clean_template_text(raw)``.

    Deliberately a wiring check, not a second truncation check: the truncation itself is
    asserted above, against a cap-free reference. What this catches is the specific
    regression B154 was -- a call site reading the ``apply_allowlist`` output (already
    capped at 512, tags intact) instead of the raw source, which no assertion phrased in
    terms of the shared helper alone would notice.
    """
    skill_raw = _fetch_table("gamedata/excel/skill_table.json")
    parsed_skills = {skill.game_id: skill for skill in parse_skills(skill_raw)}
    assert parsed_skills, "pinned skill_table produced no parsed skills"

    for game_id, entry in skill_raw.items():
        skill = parsed_skills.get(game_id)
        if skill is None:
            continue
        raw_levels = [lv for lv in (entry.get("levels") or []) if isinstance(lv, dict)]
        for index, (raw_level, level) in enumerate(zip(raw_levels, skill.levels, strict=True)):
            raw = raw_level.get("description")
            if not isinstance(raw, str) or not raw:
                continue
            assert level.description == (clean_template_text(raw) or None), (
                f"skill {game_id} L{index + 1}: imported template != full source (§V109)"
            )

    character_raw = _fetch_table("gamedata/excel/character_table.json")
    parsed_ops = {op.game_id: op for op in parse_operators(character_raw)}
    assert parsed_ops, "pinned character_table produced no parsed operators"

    for game_id, entry in character_raw.items():
        operator = parsed_ops.get(game_id)
        if operator is None:
            continue  # summon token / map trap: skipped by the importer, not a gap
        variants = {
            (talent.talent_index, variant.variant_index): variant
            for talent in operator.talents
            for variant in talent.variants
        }
        for ti, raw_talent in enumerate(entry.get("talents") or []):
            if not isinstance(raw_talent, dict):
                continue
            for vi, cand in enumerate(raw_talent.get("candidates") or []):
                if not isinstance(cand, dict):
                    continue
                raw = cand.get("description")
                if not isinstance(raw, str) or not raw:
                    continue
                variant = variants.get((ti, vi))
                assert variant is not None, f"{game_id}: talent {ti}.{vi} lost"
                assert variant.description == (clean_template_text(raw) or None), (
                    f"talent {game_id} {ti}.{vi}: imported template != full source (§V109)"
                )


def test_real_module_change_templates_are_never_truncated() -> None:
    """§V109: the third §V65 (a) call site -- module trait/talent-change templates.

    These ride ``trait_changes_json`` / ``talent_changes_json`` rather than a
    ``gameplay_description`` column, which is why the §V97 weld guard never covered
    them; the real corpus cut one of them at the pinned commit.
    """
    uniequip_raw = _fetch_table("gamedata/excel/uniequip_table.json")
    battle_equip_raw = _fetch_table("gamedata/excel/battle_equip_table.json")
    modules = parse_modules(uniequip_raw, battle_equip_raw)
    assert modules, "pinned uniequip/battle_equip tables produced no parsed modules"

    # The raw side, read straight off the source so it is independent of the parser.
    raw_by_text = {
        strip_control_chars(strip_richtext_tags(raw)).strip()
        for raw in _raw_module_templates(battle_equip_raw)
    }
    assert raw_by_text, "pinned battle_equip_table carries no module templates"

    checked = 0
    longest = 0
    for module in modules:
        for level in module.levels:
            changes = (*(level.trait_changes or ()), *(level.talent_changes or ()))
            for change in changes:
                description = change.get("description")
                if not isinstance(description, str):
                    continue
                checked += 1
                longest = max(longest, len(description))
                assert description in raw_by_text, (
                    f"module {module.game_id} L{level.level}: imported template is not "
                    f"the whole cleaned source -- truncated? (§V109/B154): {description!r}"
                )

    assert checked > 0, "no real module-change templates were checked"
    assert longest < MAX_TEMPLATE_LENGTH, (
        f"longest real EN module template is {longest} chars against a "
        f"MAX_TEMPLATE_LENGTH of {MAX_TEMPLATE_LENGTH} (§V109) -- see the skill/talent "
        "leg for what to do"
    )
