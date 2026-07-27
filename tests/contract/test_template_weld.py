"""T193: §V97 token-boundary guard against the REAL corpus (B130).

``sanitize_text`` used to delete control characters in place. Upstream EN game text
uses ``\\n`` as a real clause separator, so the deletion **welded** the two words
either side into a junk token that survived into the DB, the wire, and FTS::

    raw    "...each attack hits 1 additional target\\nUnlimited duration"
    stored "...each attack hits 1 additional targetUnlimited duration"

That string is the §V65 (a) effect TEMPLATE -- the one grounding path that closes
B56's fabrication hole, and exactly the text the server instructions tell a client to
trust over the raw blackboard key names. A welded template is misread, or fuses two
independent mechanics into one false claim. The same sanitize home (§V37) mangles
§V56 announcement titles ("Displayed Operators\\nRate Up !!!").

§V97 demands the guard be a **real-corpus** contract test, never a synthetic fixture:
a hand-written fixture proves only that the fixture matches the parser. So this module
drives the production parse path (``parse_skills`` / ``parse_operators`` /
``parse_announcements``) over real upstream bytes and asserts the *word sequence*
survives. At the pinned commit that is 9967 skill templates (2932 carrying a control
char) and 1799 talent variants.

**Oracle: token-sequence preservation, not a weld regex.** §V97 words the guard as
"no lowercase->uppercase weld", but run against the real corpus that pattern reports
162 upstream-NATIVE false positives -- ``AoE``, ``SilverAsh``, ``1SP``. Splitting on
whitespace instead is both stricter and false-positive free: ``str.split()`` is an
independent tokenizer (it splits on ``\\n`` too), so a deleted control char fuses two
tokens and the sequence diverges. Verified: with the pre-T193 deleting sanitize this
module reports 2753 bad skill rows; with the fix, 0.

CI-only: needs network, gated behind ``ARKMCP_LIVE_UPSTREAM`` like §T68, so the
default offline ``pytest -q`` skips the whole module. Nothing fetched is persisted --
the JSON is parsed in memory and discarded (§V16, code-only distribution).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import pytest
from tests.support import (
    LIVE_UPSTREAM_SKIP_REASON,
    arknights_assets_base_url,
    fetch_upstream_bytes,
    live_upstream_disabled,
)

from arknights_mcp.importers.announcements import parse_announcements
from arknights_mcp.importers.operators import parse_operators, parse_skills
from arknights_mcp.util.text import DEFAULT_MAX_TEXT_LENGTH, sanitize_text, strip_richtext_tags

pytestmark = pytest.mark.skipif(live_upstream_disabled(), reason=LIVE_UPSTREAM_SKIP_REASON)

BASE_URL = arknights_assets_base_url("en")

#: Env-supplied official announcement feed (§V56/§V61). There is deliberately NO
#: shipped default feed URL, so the announcement leg opts in via CI env rather than
#: hard-coding an endpoint in test code; absent -> that test skips.
ANNOUNCE_FEED_ENV = "ARKMCP_ANNOUNCE_FEED_URL"

#: Floor on how much of the real corpus actually carries a control char. A guard that
#: silently stops exercising its own bug is worthless: if upstream ever drops below
#: this, the test fails loudly asking for a re-pin rather than passing vacuously.
#: At the pinned commit the real counts are 2932 (skills) and 8 (talents).
MIN_SKILL_TEXTS_WITH_CONTROL = 500
MIN_TALENT_TEXTS_WITH_CONTROL = 5


def _fetch_table(relative_path: str) -> Any:
    """Fetch + parse one pinned upstream table; never written to disk (§V16)."""
    return json.loads(fetch_upstream_bytes(f"{BASE_URL}/{relative_path}").decode("utf-8"))


def _has_control_char(value: str) -> bool:
    return any(ord(ch) < 32 for ch in value)


def _assert_boundary_preserved(
    raw: str, actual: str | None, where: str, *, cap: int | None = None
) -> None:
    """The imported text must carry the same WORD SEQUENCE as the source (§V97).

    ``strip_richtext_tags`` is applied to the raw side because the importer strips
    those cosmetic tags too (§V18/T136) and a tag can legitimately join or separate
    tokens; the whitespace tokenization itself is ``str.split()``, which is
    independent of anything under test and splits on ``\\n``/``\\r``/``\\t``.

    ``cap=None`` (templates, §V109) compares the WHOLE sequence: a template must never
    be truncated at all. The earlier version of this helper branched on
    ``len(sanitize_text(raw)) >= DEFAULT_MAX_TEXT_LENGTH`` and compared a prefix with
    the last token dropped -- it looked straight at all 349 real mid-sentence
    truncations and normalized them (B154). A guard that excuses its own bug class is
    not a guard. The prefix comparison survives only for the announcement-title leg,
    which passes ``cap`` explicitly: titles are capped at the name-class 512 by design
    and are not a §V65 (a) grounding surface.
    """
    expected = strip_richtext_tags(raw).split()
    got = (actual or "").split()
    if cap is not None and len(sanitize_text(raw, max_length=cap)) >= cap:
        got = got[:-1]
        expected = expected[: len(got)]
    assert got == expected, (
        f"{where}: imported text lost the source word boundary (§V97/B130)\n"
        f"  raw      {raw!r}\n"
        f"  imported {actual!r}"
    )


def test_real_skill_templates_preserve_token_boundary() -> None:
    """§V97: every real EN skill-level TEMPLATE keeps its source word sequence."""
    skill_raw = _fetch_table("gamedata/excel/skill_table.json")
    parsed = {skill.game_id: skill for skill in parse_skills(skill_raw)}
    assert parsed, "pinned skill_table produced no parsed skills"

    checked = 0
    with_control = 0
    for game_id, entry in skill_raw.items():
        skill = parsed.get(game_id)
        if skill is None:
            continue
        raw_levels = [lv for lv in (entry.get("levels") or []) if isinstance(lv, dict)]
        assert len(raw_levels) == len(skill.levels), f"{game_id}: level count drifted"
        for index, (raw_level, level) in enumerate(zip(raw_levels, skill.levels, strict=True)):
            raw = raw_level.get("description")
            if not isinstance(raw, str) or not raw:
                continue
            checked += 1
            with_control += _has_control_char(raw)
            _assert_boundary_preserved(raw, level.description, f"skill {game_id} L{index + 1}")

    assert checked > 0, "no real skill templates were checked"
    assert with_control >= MIN_SKILL_TEXTS_WITH_CONTROL, (
        f"only {with_control} of {checked} real skill templates carry a control char "
        f"(expected >= {MIN_SKILL_TEXTS_WITH_CONTROL}); the corpus no longer exercises "
        "§V97 -- re-pin ARKNIGHTS_ASSETS_COMMIT or re-derive the floor"
    )


def test_real_talent_templates_preserve_token_boundary() -> None:
    """§V97: every real EN talent-variant TEMPLATE keeps its source word sequence."""
    character_raw = _fetch_table("gamedata/excel/character_table.json")
    parsed = {op.game_id: op for op in parse_operators(character_raw)}
    assert parsed, "pinned character_table produced no parsed operators"

    checked = 0
    with_control = 0
    for game_id, entry in character_raw.items():
        operator = parsed.get(game_id)
        if operator is None:
            continue  # summon token / map trap: skipped by the importer, not a gap
        variants = {
            (talent.talent_index, variant.variant_index): variant
            for talent in operator.talents
            for variant in talent.variants
        }
        for talent_index, raw_talent in enumerate(entry.get("talents") or []):
            if not isinstance(raw_talent, dict):
                continue
            for variant_index, cand in enumerate(raw_talent.get("candidates") or []):
                if not isinstance(cand, dict):
                    continue
                raw = cand.get("description")
                if not isinstance(raw, str) or not raw:
                    continue
                variant = variants.get((talent_index, variant_index))
                assert variant is not None, f"{game_id}: talent {talent_index}.{variant_index} lost"
                checked += 1
                with_control += _has_control_char(raw)
                _assert_boundary_preserved(
                    raw, variant.description, f"talent {game_id} {talent_index}.{variant_index}"
                )

    assert checked > 0, "no real talent templates were checked"
    assert with_control >= MIN_TALENT_TEXTS_WITH_CONTROL, (
        f"only {with_control} of {checked} real talent templates carry a control char "
        f"(expected >= {MIN_TALENT_TEXTS_WITH_CONTROL}); the corpus no longer exercises "
        "§V97 -- re-pin ARKNIGHTS_ASSETS_COMMIT or re-derive the floor"
    )


def test_real_announcement_titles_preserve_token_boundary() -> None:
    """§V97/§V56: real EN announcement TITLES keep their source word sequence.

    The second domain B130 verified live. Unlike the gamedata tables the official feed
    is not pinnable -- it is whatever is published today -- so this asserts the
    invariant relative to whatever the feed returns and does not require a control
    char to be present on any given day. The pinned template legs above carry the
    "corpus still exercises the bug" floor.
    """
    feed_url = os.environ.get(ANNOUNCE_FEED_ENV, "").strip()
    if not feed_url:
        pytest.skip(f"no announcement feed configured; set {ANNOUNCE_FEED_ENV} (CI only)")

    feed_raw = json.loads(fetch_upstream_bytes(feed_url).decode("utf-8"))
    entries = {
        str(entry["announceId"]): entry
        for entry in (feed_raw.get("announceList") or [])
        if isinstance(entry, dict) and entry.get("announceId") is not None
    }
    assert entries, "official EN announcement feed returned no entries"

    parsed = parse_announcements(feed_raw, fetched_at=datetime.now(tz=UTC))
    assert parsed, "real announcement feed parsed to zero rows"

    for announcement in parsed:
        raw = entries[announcement.announce_id].get("title")
        if not isinstance(raw, str) or not raw:
            continue
        _assert_boundary_preserved(
            raw,
            announcement.title,
            f"announcement {announcement.announce_id}",
            cap=DEFAULT_MAX_TEXT_LENGTH,
        )
