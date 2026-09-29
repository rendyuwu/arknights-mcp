"""Response-side ENUM LEGEND machinery shared by every tool.

Split out of :mod:`~arknights_mcp.mcp.tools._shared` at the module-size hard cap: publishing
an output enum's value domain is one responsibility with one home, and it is
the one that grows every time a new typed column reaches the wire -- ``damage_types``,
``targeting`` and ``immunities`` all arrived in a single task.

Re-exported by ``_shared`` so every tool keeps one import site.
"""

from __future__ import annotations

from collections.abc import Sequence

#: The RESPONSE-SIDE value domain for every OUTPUT enum
#: this server emits. Each domain used to be spelled out in the description of every tool
#: that emits the field -- ``difficulty`` in three, ``item_type`` in two -- which put two
#: rules (every emitted enum states its domain, and the description is bounded) on the
#: same string with no priority between them, an edit now settled. An
#: OUTPUT domain is read POST-call, so the legend arrives beside the value it decodes and
#: costs none of the description budget; the precedent is :data:`IMAGE_REFS_LEGEND`, hoisted
#: the same way by :func:`attach_image_ref_disclosures`, and the older ``tile_grid`` legend.
#:
#: The map is STATIC: it is the whole label vocabulary of the field, never
#: filtered to the values the rows of one response happen to carry -- a filtered legend
#: teaches a client a PARTIAL domain it then caches.
#:
#: Every token below was COUNTED over the promoted build ``2026-07-28T030224Z``
#: (a domain is counted, never guessed); ``TOUGH``/``EASY`` are the one pair that is not
#: stored, being DERIVED at query time from a ``tough_``/``easy_`` game id whose stored
#: difficulty says ``NORMAL``, so the stored set alone would drop them.
#:
#: The gloss of each value is exactly the gloss its description carried -- this is a MOVE,
#: never a rewrite, so no value gains a meaning that was not already shipped and
#: verified. Where the shipped description glossed a token only as the source's own, the
#: gloss says so rather than inventing one (a source token's meaning is never guessed;
#: ``sp_type``'s numeric arm is the live case). A domain that is OPEN carries
#: its openness in :data:`OPEN_ENUM_LIMITATIONS` instead. Client-facing text,
#: so no internal cites/jargon.
_SOURCE_TOKEN = "the source's own token"

ENUM_LEGENDS: dict[str, dict[str, str]] = {
    "difficulty": {
        "NORMAL": "the base stage, from the source's own difficulty field",
        "FOUR_STAR": "a challenge stage, from the source's own difficulty field",
        "SIX_STAR": (
            "a second variant of some main-story stages, from the source's own difficulty field"
        ),
        "TOUGH": (
            "the harder variant, derived from the stage's game id; it shares its code and "
            "name with the base stage, and this tag is what tells the two apart"
        ),
        "EASY": (
            "the easier variant, derived from the stage's game id; it shares its code and "
            "name with the base stage, and this tag is what tells the two apart"
        ),
    },
    "stage_type": dict.fromkeys(
        (
            "MAIN",
            "SUB",
            "ACTIVITY",
            "DAILY",
            "CAMPAIGN",
            "CLIMB_TOWER",
            "SPECIAL_STORY",
            "GUIDE",
        ),
        "the source's own category token",
    ),
    "enemy_class": dict.fromkeys(("NORMAL", "ELITE", "BOSS"), _SOURCE_TOKEN),
    "motion_type": {"WALK": "ground", "FLY": "aerial"},
    "damage_types": {
        "PHYSIC": "physical damage, reduced by a defender's def",
        "MAGIC": "arts damage, reduced by res instead of def",
        "NO_DAMAGE": "this enemy deals no damage",
        "HEAL": "this enemy heals rather than damages",
    },
    "targeting": {
        "MELEE": "attacks only what it is blocked by or adjacent to",
        "RANGED": "attacks from beyond melee reach",
        "ALL": "attacks both ground and aerial targets",
        "NONE": "the source states no attack targeting for this enemy",
    },
    "immunities": {
        "STUN": "ignores stun",
        "SILENCE": "ignores silence",
        "SLEEP": "ignores sleep",
        "FROZEN": "ignores freeze",
        "LEVITATE": "ignores levitate",
        "DISARMED_COMBAT": "ignores disarm",
        "FEARED": "ignores fear",
        "PALSY": "ignores palsy",
        "ATTRACT": "ignores forced-attraction pulls",
    },
    "profession": {
        "PIONEER": "Vanguard",
        "WARRIOR": "Guard",
        "TANK": "Defender",
        "SPECIAL": "Specialist",
        "SUPPORT": "Supporter",
        "SNIPER": "Sniper",
        "CASTER": "Caster",
        "MEDIC": "Medic",
    },
    "position": dict.fromkeys(("MELEE", "RANGED"), _SOURCE_TOKEN),
    "applies_to": {
        "token": "the change describes the operator's summon or token, not the operator",
        "operator": "the change describes the operator itself",
    },
    "skill_type": dict.fromkeys(("AUTO", "MANUAL", "PASSIVE"), _SOURCE_TOKEN),
    "sp_type": dict.fromkeys(
        ("INCREASE_WITH_TIME", "INCREASE_WHEN_ATTACK", "INCREASE_WHEN_TAKEN_DAMAGE"),
        _SOURCE_TOKEN,
    ),
    "duration_type": {
        "NONE": (
            "the source declares no duration type -- not that the skill has no duration: "
            "read the level's own duration, in seconds"
        ),
        "AMMO": _SOURCE_TOKEN,
    },
    "item_type": {
        "MATERIAL": _SOURCE_TOKEN,
        "CHIP": "class chips",
        "CARD_EXP": "EXP battle records",
        "RECRUIT_TAG": _SOURCE_TOKEN,
        "ACTIVITY_ITEM": "event currency",
        "FURN": "furniture",
        "TEMP": _SOURCE_TOKEN,
        "ARKPLANNER": _SOURCE_TOKEN,
        "LGG_SHD": _SOURCE_TOKEN,
    },
    "rule_type": dict.fromkeys(
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
        "the source's own pool-rule token",
    ),
}


#: The OPEN domains -- source-defined sets that no legend can close, so
#: a token outside :data:`ENUM_LEGENDS` must read as source-defined rather than an error.
#: The LIMITATION is the home for an open domain, so the openness rides the
#: envelope beside the legend it qualifies instead of the description.
#:
#: ``sp_type`` is the mixed-encoding case: the wire key carries BOTH the named
#: tokens and a BARE NUMERIC CODE (``8`` on 1145 rows of the promoted build), because the
#: source sends ``spType`` as an int on those rows and the importer stringifies it.
#:
#: Which of the two exits applies was settled against the pinned upstream rather than
#: from the schema. The pin ships names and the bare ``8`` in the SAME file, and
#: a second independent export of the same game data emits the same bare ``8`` on all 1352
#: skill ids the two share, 0 disagreements -- so no name for the code exists upstream to
#: import, and an import-time map could only have invented one. The code therefore stays
#: raw and this string carries the openness: still the FLOOR, not a resolution,
#: which is why it also names the neighbouring fields that ARE decidable for those skills.
#: Client-facing text, so no internal cites/jargon; short sentences.
OPEN_ENUM_LIMITATIONS: dict[str, str] = {
    "item_type": (
        "The item_type set is defined by the game data and may grow. Treat a token the "
        "response's enum_legend does not list as source-defined, not as an error."
    ),
    "rule_type": (
        "The rule_type set is defined by the game data and may grow. Treat a token the "
        "response's enum_legend does not list as source-defined, not as an error."
    ),
    "sp_type": (
        "A skill's sp_type is normally one of the named tokens the response's enum_legend "
        "lists, but some skills carry a bare number instead (8 in this data). The game "
        "data ships no name for that code, so it is emitted exactly as the source stores "
        "it and is never given a fabricated meaning -- read it as source-defined, not as "
        "an error. For those skills, skill_type and each level's sp_cost and initial_sp "
        "are named and complete."
    ),
}


#: The single home for WHICH enum fields each tool may decode. The
#: tool passes the subset it actually emitted (a legend for a field the response omits
#: would be noise), and this table is the full set it is allowed to draw from -- so
#: the real-corpus enum-domain guard can ask "does the tool that emits this column publish
#: its domain?" without re-deriving the mapping from each tool module and drifting from it.
TOOL_ENUM_LEGEND_FIELDS: dict[str, tuple[str, ...]] = {
    "get_stage": ("difficulty", "stage_type"),
    "search_stages": ("difficulty",),
    "search_entities": ("difficulty",),
    "get_enemy": ("enemy_class", "motion_type", "damage_types", "targeting", "immunities"),
    "analyze_stage": ("enemy_class", "damage_types", "targeting"),
    "get_operator": (
        "profession",
        "position",
        "skill_type",
        "sp_type",
        "duration_type",
        "applies_to",
    ),
    "compare_operator_modules": ("applies_to",),
    "get_stage_drops": ("item_type",),
    "get_item_drops": ("item_type",),
    "get_banners": ("rule_type",),
    "get_my_roster": ("profession",),
    "get_my_operator": ("profession",),
}


def attach_enum_legend(
    data: dict[str, object], fields: Sequence[str], limitations: tuple[str, ...]
) -> tuple[str, ...]:
    """Hoist the static ``enum_legend`` for ``fields`` + any open-domain caveat.

    The enum counterpart of :func:`attach_image_ref_disclosures`, and one home for
    both halves of the same predicate: a response that emits an enum-valued field hoists
    that field's WHOLE value vocabulary once onto ``data`` (never filtered to the values
    these rows carry) AND, when the domain is source-defined and cannot be
    closed, appends its openness limitation -- a legend without that caveat
    would read as an exhaustive partition and turn a future upstream token into an
    apparent error. Mutates ``data`` in place and returns the extended limitations tuple;
    an empty ``fields`` is a no-op, so a response that emitted none of them (a search with
    no stage locator, an operator fetched without ``include_skills``) ships neither key.
    """
    if not fields:
        return limitations
    data["enum_legend"] = {field: dict(ENUM_LEGENDS[field]) for field in fields}
    return (*limitations, *(OPEN_ENUM_LIMITATIONS[f] for f in fields if f in OPEN_ENUM_LIMITATIONS))
