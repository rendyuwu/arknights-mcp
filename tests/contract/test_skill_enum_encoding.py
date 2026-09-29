"""The skill enum ENCODING, pinned against real upstream.

``skills.sp_type`` reaches the wire carrying two encodings of one concept: the three
named tokens and a bare ``8``. The earlier sweep read that as a legacy numeric form still round-
tripping through ``_enum_text``, and left two exits open -- (a) map the code to its name
at import time, (b) keep the code and declare the domain OPEN. Deciding between them is
a question about the SOURCE, not about our code, so this module asks the source::

    en  spType   name 8674 rows   |   int 8  1515 rows
    cn  spType   name 9108 rows   |   int 8  1745 rows
    en  skillType / durationType  |   100% named (10189 rows each)
    cn  skillType / durationType  |   100% named (10853 rows each)

Both forms ship in the SAME file at the SAME pin, and one skill (``sktok_mjcsdw``)
carries both across its own levels, so the numeric arm is not an older export format --
it is an enum member upstream never named. A second, independent export of the same game
data emits the same bare ``8`` on all 1352 skill ids it shares with the pin, so the name
does not exist to import rather than having been missed here. That rules out (a): a map
could only invent the meaning, which the real-corpus rule forbids. Exit (b) shipped, and
this module is what keeps the reasoning falsifiable -- a re-pin onto a tree where upstream finally
names the code, or sends a second one, fails here first.

The two clean siblings are pinned for the same reason they were named here: the
stringify is SHARED, so they are clean by DATA, never by construction. The day upstream
sends an int for ``skillType`` the code would reach the wire exactly as silently as
``8`` did, and this is the guard that sees it at the source.

The build-side counterpart (what the promoted corpus actually stores, and whether the
client-facing legend and openness caveat match it) lives in
``tests/contract/test_enum_domain_coverage.py``.

CI-only: needs network, gated behind ``ARKMCP_LIVE_UPSTREAM`` like the other live-upstream
guards. Nothing fetched is persisted (code-only distribution).
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import pytest
from tests.support import (
    LIVE_UPSTREAM_SKIP_REASON,
    arknights_assets_base_url,
    fetch_upstream_bytes,
    live_upstream_disabled,
)

from arknights_mcp.mcp.tools._enum_legend import ENUM_LEGENDS, OPEN_ENUM_LIMITATIONS

pytestmark = pytest.mark.skipif(live_upstream_disabled(), reason=LIVE_UPSTREAM_SKIP_REASON)

SERVERS = ("en", "cn")

#: ``(upstream key, emitted column, expected numeric arm)`` for the three fields the
#: shared ``_enum_text`` coercion feeds. ``spType`` is nested under ``spData``; the other
#: two sit on the skill level itself.
_ENCODINGS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("spType", "sp_type", frozenset({"8"})),
    ("skillType", "skill_type", frozenset()),
    ("durationType", "duration_type", frozenset()),
)

#: Floor on how many level rows carry the bare code at the pin (real: 1515 en / 1745 cn).
#: A corpus that stopped carrying it would let every assertion below pass vacuously while
#: the mixed encoding it exists for went unmeasured.
MIN_NUMERIC_SP_TYPE_ROWS = 1000


@lru_cache(maxsize=len(SERVERS))
def _skill_table(server: str) -> dict[str, Any]:
    """The pinned ``skill_table`` for ``server``; fetched once, never written."""
    url = f"{arknights_assets_base_url(server)}/gamedata/excel/skill_table.json"
    table = json.loads(fetch_upstream_bytes(url).decode("utf-8"))
    assert isinstance(table, dict) and table, f"{server} skill_table is not a populated dict"
    return table


def _raw_values(server: str, source_key: str) -> list[Any]:
    """Every raw value of ``source_key`` across every skill level, types intact.

    Read straight off the upstream JSON rather than through the importer: the point is
    what the SOURCE sends, so the expected side must not pass through the coercion under
    test.
    """
    out: list[Any] = []
    for entry in _skill_table(server).values():
        for level in entry.get("levels") or []:
            if not isinstance(level, dict):
                continue
            holder = level.get("spData") if source_key == "spType" else level
            if isinstance(holder, dict) and source_key in holder:
                out.append(holder[source_key])
    return out


@pytest.mark.parametrize("server", SERVERS)
@pytest.mark.parametrize(("source_key", "column", "numeric_arm"), _ENCODINGS)
def test_upstream_enum_arms_are_pinned(
    server: str, source_key: str, column: str, numeric_arm: frozenset[str]
) -> None:
    values = _raw_values(server, source_key)
    assert values, f"{server} {source_key} yielded no values"
    named = {value for value in values if isinstance(value, str)}
    numeric = {str(value) for value in values if isinstance(value, int)}
    assert not (named & numeric), f"{server} {source_key}: a token is both name and code"
    # A code upstream never named cannot be decoded, so a
    # NEW one -- or the first one on a sibling -- must fail loudly and be re-verified
    # before any client-facing text claims it exists.
    assert numeric == set(numeric_arm), f"{server} {source_key} numeric arm moved: {numeric}"
    # What the source names is what the client-facing legend documents.
    assert named == set(ENUM_LEGENDS[column]), f"{server} {column} named arm moved: {named}"
    # Leftovers would be a third encoding -- the shape assumption itself.
    assert all(isinstance(value, str | int) for value in values), f"{server} {source_key}"


@pytest.mark.parametrize("server", SERVERS)
def test_sp_type_mixes_both_encodings_in_one_pinned_tree(server: str) -> None:
    """The mixed encoding is UPSTREAM's own, and it is not a legacy form."""
    values = _raw_values(server, "spType")
    numeric_rows = [value for value in values if isinstance(value, int)]
    named_rows = [value for value in values if isinstance(value, str)]
    # Both arms in one file at one pin: whatever ``8`` is, it is not an older export the
    # named form replaced -- which is the framing this module corrected.
    assert named_rows, f"{server} spType carries no named rows"
    assert len(numeric_rows) >= MIN_NUMERIC_SP_TYPE_ROWS, len(numeric_rows)
    # Sharper still: one skill carries both forms across its OWN levels (``sktok_mjcsdw``
    # at the pin), which no per-export or per-era explanation survives.
    mixed = [
        skill_id
        for skill_id, entry in _skill_table(server).items()
        if len(
            {
                type((level.get("spData") or {}).get("spType")).__name__
                for level in entry.get("levels") or []
                if isinstance(level, dict)
            }
        )
        > 1
    ]
    assert mixed, f"{server}: no skill carries both spType encodings across its levels"
    # The openness that follows is what the client is told, so it names every
    # code the source actually sends.
    caveat = OPEN_ENUM_LIMITATIONS["sp_type"]
    for value in {str(value) for value in numeric_rows}:
        assert value in caveat, value


#: The four fields ``skill_table`` scopes PER LEVEL, with the number of
#: skills whose levels DISAGREE at the pin, counted per server (en, cn). An earlier sweep found the
#: mixed encoding while reading these same rows and left the level axis open: the importer
#: was storing level 1's value as the whole skill's, so a skill that changes between
#: mastery levels had three facts dropped and a fourth asserted for levels it does not
#: describe. ``durationType`` is at 0 today -- pinned anyway, because that is the
#: point: a field is uniform by DATA, never by construction, and the first skill whose
#: duration type changes must fail here rather than import silently flattened.
_LEVEL_SCOPED_FIELDS: tuple[tuple[str | None, str, int, int], ...] = (
    ("spData", "spType", 1, 1),
    (None, "skillType", 1, 1),
    (None, "durationType", 0, 0),
    (None, "name", 1, 3),
)


def _varying_skill_ids(server: str, holder_key: str | None, field: str) -> list[str]:
    """Skill ids whose own levels do not agree on ``field`` (types included)."""
    out: list[str] = []
    for skill_id, entry in _skill_table(server).items():
        seen: set[tuple[str, object]] = set()
        for level in entry.get("levels") or []:
            if not isinstance(level, dict):
                continue
            holder = level.get(holder_key) if holder_key else level
            if isinstance(holder, dict) and field in holder:
                value = holder[field]
                # (type, value) so a NAME and a CODE never collapse into one token -- the
                # axis this same skill (`sktok_mjcsdw`) is the live case for.
                seen.add((type(value).__name__, str(value)))
        if len(seen) > 1:
            out.append(skill_id)
    return out


@pytest.mark.parametrize("server", SERVERS)
@pytest.mark.parametrize(("holder_key", "field", "en_count", "cn_count"), _LEVEL_SCOPED_FIELDS)
def test_per_level_variance_count_is_pinned_at_the_source(
    server: str, holder_key: str | None, field: str, en_count: int, cn_count: int
) -> None:
    """How many skills vary is COUNTED at the pin, never assumed to be none."""
    varying = _varying_skill_ids(server, holder_key, field)
    expected = en_count if server == "en" else cn_count
    assert len(varying) == expected, f"{server} {field} varying skills moved: {sorted(varying)}"


@pytest.mark.parametrize("server", SERVERS)
def test_the_flattened_skill_carried_a_named_sp_type_on_a_later_level(server: str) -> None:
    """The fact a level-1 read discarded, at the source.

    ``sktok_mjcsdw`` is where the two bugs meet: the earlier sweep verified upstream ships no name
    for the code ``8``, which is true of the corpus -- and this skill's OWN level 2 ships a
    named token anyway. Storing level 1's value for the whole skill therefore kept the
    undecidable arm and threw away the decidable one, invisibly, because the result reads
    exactly like the open-domain case the earlier sweep documented.
    """
    levels = _skill_table(server)["sktok_mjcsdw"]["levels"]
    sp_types = [(level.get("spData") or {}).get("spType") for level in levels]
    assert sp_types[0] == 8, sp_types
    assert isinstance(sp_types[1], str) and sp_types[1] in ENUM_LEGENDS["sp_type"], sp_types
    skill_types = [level.get("skillType") for level in levels]
    assert skill_types == ["PASSIVE", "AUTO"], skill_types


@pytest.mark.parametrize("server", SERVERS)
def test_a_per_level_field_we_already_store_per_level_is_the_control(server: str) -> None:
    """``rangeId`` proves the shape was never the obstacle.

    It sits on the same level entries, varies for seven skills in both regions, and has
    always been stored per level -- so the four fields beside it were flattened by the
    MAPPING, not by anything the source made hard. A corpus where it stopped varying would
    make that argument vacuous, so the count is pinned too.
    """
    assert len(_varying_skill_ids(server, None, "rangeId")) == 7
