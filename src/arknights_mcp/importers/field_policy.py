"""Explicit field allowlist for imported gameplay data (SPEC §V18; PRD 10.2).

The importer parses *only* allowlisted source fields; unused prose and unknown
fields are dropped. Kept string values are sanitized (control chars stripped,
length capped). Each allowlist is versioned via ``FIELD_POLICY_VERSION`` so a
policy change is recorded on every snapshot and provenance row.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from arknights_mcp.util.text import DEFAULT_MAX_TEXT_LENGTH, sanitize_text

#: Bump when any allowlist below changes; stored on snapshots + provenance.
#: 2: B46/§V59 added ``name_i18n`` to ITEM_ALLOWLIST (region-locale item names).
#: 3: T107/§V61 added ``day``/``month``/``webUrl``/``group`` to ANNOUNCEMENT_ALLOWLIST
#:    (real official feed field-map: day+month->date, webUrl->url, group->category).
#: 4: T99/§V57 added LOCALE_NAME_ALLOWLIST (extra-locale jp/kr canonical NAMES only).
#: 5: T111/§V62 added BANNER_ALLOWLIST + LIMIT_PARAM/DYN_META sub-allowlists (banner
#:    archive: typed schedule facts + typed featured-op ids only, no gacha prose).
FIELD_POLICY_VERSION = "5"

#: Fact region -> name/alias locale tag (§V57; B46/§V59). A region's canonical
#: strings are in that region's language: an en entity's name is English (locale
#: ``en``), a cn entity's name is Chinese (locale ``zh``). Two consumers share this
#: single home (§V37): ``penguin_drops`` picks ``name_i18n.<locale>`` for an item's
#: display name, and ``operators`` stamps the same tag on each locale alias (T98).
#: The locale tag is NOT a fact region -- an en/cn entity still returns its OWN
#: region facts (§V57). Migration 0011's SQL backfill mirrors this cn->zh coupling.
REGION_TO_NAME_LOCALE: dict[str, str] = {"en": "en", "cn": "zh"}

#: Extra-locale alias region -> locale tag (§V57/T99). The v0.2 extra-locale alias
#: import adds jp/kr canonical NAMES as locale-tagged aliases on the existing en/cn
#: entities (matched by ``game_id``). The alias-region argument the importer takes
#: (``jp``/``kr``) maps to the stored locale tag (``ja``/``ko``), the same locale
#: codes penguin's ``name_i18n`` uses. This is NOT a fact region: a jp/kr NAME is
#: only an extra searchable alias on an en/cn entity, and an alias match still
#: returns the entity's OWN region facts (§V57 -- region availability never widens).
EXTRA_LOCALE_FOR_REGION: dict[str, str] = {"jp": "ja", "kr": "ko"}

# --- Allowlisted SOURCE fields per record type (V18) -------------------------
# Prose fields (e.g. "description") are intentionally absent and thus excluded.

ENEMY_HANDBOOK_ALLOWLIST: frozenset[str] = frozenset(
    {"enemyId", "name", "enemyLevel", "attackType", "motionType"}
)

ENEMY_LEVEL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "level",
        "hp",
        "atk",
        "def",
        "res",
        "attackInterval",
        "attackRange",
        "moveSpeed",
        "weight",
        "lifePointReduction",
        "blockBehavior",
        "targeting",
        "immunities",
        "abilities",
    }
)

ZONE_ALLOWLIST: frozenset[str] = frozenset({"zoneId", "zoneName", "type"})

STAGE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "stageId",
        "code",
        "name",
        "zoneId",
        "stageType",
        "difficulty",
        "apCost",
        "recommendedLevel",
        "maxLifePoints",
        "levelId",
    }
)

#: Structural keys the normalizer may read from a ``useDb:false`` ref's
#: ``overwrittenData`` when modelling a stage-scoped inline enemy variant (§T80).
#: ``overwrittenData`` is an ``enemyData``-shaped partial that also carries prose
#: (``name``/``description``); only these known structural keys are read, so the
#: variant is built from typed stats (attributes/motion/lifePointReduce) + its
#: base ``prefabKey`` and never a prose leaf (§V18/§V16). The extracted stat maps
#: themselves are the §V29-verified enemy-level maps (single home, §V37). The
#: variant's *level* is the ref's own ``level`` field (resolved in ``_enemy_ref_map``),
#: never ``overwrittenData.level`` -- so ``level`` is deliberately absent here.
OVERWRITTEN_DATA_ALLOWLIST: frozenset[str] = frozenset(
    {"prefabKey", "attributes", "motion", "lifePointReduce"}
)

#: Structural spawn-action fields kept in ``stage_spawns.source_fragment_json``.
#: The raw wave action is untrusted and may carry prose/injection fields; only
#: these known structural keys are retained (§V18 "known keys only, no prose").
SPAWN_ACTION_ALLOWLIST: frozenset[str] = frozenset(
    {
        "enemyId",
        # B37: for a ``useDb:false`` inline enemy variant, ``enemyId`` is resolved
        # to the base prefab; ``variantId`` preserves the original inline id (an
        # id-charset string, not prose) for traceability of which spawn was a
        # level-inline variant of the base enemy.
        "variantId",
        "levelVariant",
        "routeIndex",
        "spawnTime",
        "count",
        "interval",
        "spawnGroup",
        "hidden",
    }
)


#: Operator scalar fields from ``character_table`` (§V18). Prose (``description``,
#: ``itemUsage``, ``itemDesc``) is intentionally absent and thus excluded. The
#: nested ``phases``/``skills``/``talents`` lists are *not* kept here -- each is
#: parsed field-by-field with its own allowlist below, so no prose rides in via a
#: raw nested structure (§V31).
CHARACTER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "name",
        "appellation",
        "rarity",
        "profession",
        "subProfessionId",
        "position",
        "tagList",
        "isNotObtainable",
    }
)

#: One elite ``phases[]`` entry (structural stats only; no prose).
PHASE_ALLOWLIST: frozenset[str] = frozenset({"rangeId", "maxLevel"})

#: A phase attribute keyframe ``data`` block (all numeric).
PHASE_ATTR_ALLOWLIST: frozenset[str] = frozenset(
    {"maxHp", "atk", "def", "magicResistance", "cost", "blockCnt", "baseAttackTime", "respawnTime"}
)

#: An operator's per-skill link (``character_table.skills[]``); the skill's own
#: record lives in ``skill_table`` (SKILL_LEVEL_ALLOWLIST). ``unlockCond`` is a
#: nested ``{phase, level}`` dict kept structurally (both numeric/enum).
SKILL_LINK_ALLOWLIST: frozenset[str] = frozenset({"skillId", "unlockCond"})

#: One ``skill_table`` level entry. ``description`` (prose) is excluded (§V16);
#: ``spData`` is a nested numeric block (SP_DATA_ALLOWLIST).
SKILL_LEVEL_ALLOWLIST: frozenset[str] = frozenset(
    {"name", "rangeId", "skillType", "durationType", "duration", "spData", "blackboard"}
)

#: The ``spData`` sub-block of a skill level (all numeric/enum).
SP_DATA_ALLOWLIST: frozenset[str] = frozenset(
    {"spType", "spCost", "initSp", "maxChargeTime", "increment"}
)

#: One talent ``candidates[]`` variant. ``description``/``upgradeDescription`` are
#: prose and excluded (§V16); the ``name`` is a short gameplay label (kept, like a
#: skill/operator display name). ``blackboard`` holds numeric params.
TALENT_CANDIDATE_ALLOWLIST: frozenset[str] = frozenset(
    {"name", "unlockCondition", "requiredPotentialRank", "prefabKey", "blackboard"}
)

#: One ``blackboard`` parameter entry shared by skills + talents + modules (§V31:
#: keep the structural key/value, drop anything else).
BLACKBOARD_ALLOWLIST: frozenset[str] = frozenset({"key", "value", "valueStr"})

#: Scalar module fields from ``uniequip_table.equipDict[]`` (§V18). Prose
#: (``uniEquipDesc``) and icon/color/mission fields are intentionally absent and
#: thus excluded; ``uniEquipName`` is a short display label (kept, like an operator
#: name). ``itemCost`` is *not* kept whole here -- it is a nested per-level dict of
#: item lists, extracted level-by-level with ITEM_COST_ALLOWLIST (§V31).
UNIEQUIP_ALLOWLIST: frozenset[str] = frozenset(
    {
        "uniEquipId",
        "uniEquipName",
        "charId",
        "type",
        "typeName1",
        "typeName2",
        "showEvolvePhase",
        "unlockEvolvePhase",
        "showLevel",
        "unlockLevel",
    }
)

#: One ``itemCost`` entry for a module upgrade level (all id/count/enum; no prose).
ITEM_COST_ALLOWLIST: frozenset[str] = frozenset({"id", "count", "type"})

#: One Penguin Statistics ``items`` entry (§V18; §T89). ``itemId`` is the item's
#: game id (== arknights item id), ``name`` a short display label (kept, like an
#: operator/enemy name), ``rarity``/``itemType`` structural enums. ``name_i18n`` is
#: the per-locale display name (``en``/``zh``/``ja``/``ko``) -- also name-only, not
#: prose -- kept so the en build surfaces the English label instead of the canonical
#: Chinese ``name`` (B46/§V59). Prose (icons, descriptions, sort/existence metadata)
#: is intentionally absent and thus excluded.
ITEM_ALLOWLIST: frozenset[str] = frozenset({"itemId", "name", "name_i18n", "rarity", "itemType"})

#: One Penguin Statistics ``result/matrix`` drop entry (§V18; §T89). All structural:
#: ``stageId``/``itemId`` are game ids joined to the internal stage/item rows;
#: ``quantity``/``times`` are the sample counts a drop rate derives from;
#: ``start``/``end`` bound the sample window. No prose leaf.
PENGUIN_MATRIX_ALLOWLIST: frozenset[str] = frozenset(
    {"stageId", "itemId", "quantity", "times", "start", "end"}
)

#: One official-announcement feed entry (§V18; §T95; §T107; §V56 metadata-ONLY). The
#: scope is the maximum permitted by D14/§V56: an ``announceId`` (the feed's stable id),
#: a ``title`` (a short name string, kept + sanitized + length-capped like an
#: operator/enemy name), a publication ``date``, a canonical ``url``, and a
#: ``category`` enum. The real official feed (verified 2026-07-21, §V61) names three of
#: these differently, so the source keys the field-map reads are ALSO allowlisted:
#: ``day``/``month`` (ints -> normalized to an ISO ``date`` in ``parse_announcements``),
#: ``webUrl`` (-> ``url``), and ``group`` (an enum/name string -> ``category``). Both the
#: canonical and the source key names are kept so a feed carrying either shape maps
#: cleanly; each is an id/int/enum/name string, never prose (§V18). The article BODY /
#: HTML / prose / promotional image / image-url are deliberately ABSENT and thus dropped
#: -- the full announcement body is never stored (§V16 extends to the announcement
#: domain, §V56). The 0010 schema likewise has no column for any of them, so a body
#: cannot be persisted even if a future allowlist regressed.
ANNOUNCEMENT_ALLOWLIST: frozenset[str] = frozenset(
    {"announceId", "title", "date", "url", "category", "day", "month", "webUrl", "group"}
)

#: Scalar banner-archive fields from a ``gacha_table.json`` ``gachaPoolClient`` entry
#: (§V18; §T111; §V62 metadata-ONLY). All structural: ``gachaPoolId`` is the pool's
#: game id, ``gachaPoolName`` a short display label (kept + sanitized + length-capped
#: like an operator/enemy name), ``openTime``/``endTime`` unix-epoch ints (normalized
#: to ISO in the importer), ``gachaRuleType`` an enum. The prose/promotional fields
#: ``gachaPoolSummary``/``gachaPoolDetail``/``dynMeta`` html/image are deliberately
#: ABSENT and thus dropped -- the banner archive is a metadata-only historical FACT,
#: never gacha prose (§V16 release-artifact + runtime store extends to the banner
#: domain, §V56 ceiling class). The typed featured-op ids live under the nested
#: ``limitParam``/``dynMeta`` parents, which are NOT kept whole here (``dynMeta`` also
#: carries prose): each is sub-extracted with its own allowlist below, so no prose
#: leaf can ride in via a raw nested structure (§V31), the same pattern as
#: ``uniequip_table.itemCost`` (ITEM_COST_ALLOWLIST) and ``overwrittenData``.
BANNER_ALLOWLIST: frozenset[str] = frozenset(
    {"gachaPoolId", "gachaPoolName", "openTime", "endTime", "gachaRuleType"}
)

#: The ``limitParam`` sub-block of a ``LIMITED`` banner. ``limitedCharId`` is the
#: single featured limited operator's char id (an id-charset string, not prose);
#: everything else (event/mission metadata) is dropped (§V18; §V62 typed featured-op).
LIMIT_PARAM_ALLOWLIST: frozenset[str] = frozenset({"limitedCharId"})

#: The ``dynMeta`` sub-block of a CLASSIC-family banner. ``attainRare6CharList`` is the
#: array of featured 6-star char ids (id-charset strings). ``dynMeta`` ALSO carries
#: prose/html/image (``gachaPoolSummary``-style rate-up copy), so it is NEVER kept
#: whole -- only this one typed array survives (§V18/§V16 metadata-only; §V62).
DYN_META_ALLOWLIST: frozenset[str] = frozenset({"attainRare6CharList"})


@dataclass(frozen=True)
class AllowlistResult:
    """Outcome of applying an allowlist: kept (sanitized) fields + dropped keys."""

    kept: dict[str, Any]
    dropped: list[str]


def sanitize_value(value: Any, *, max_length: int = DEFAULT_MAX_TEXT_LENGTH) -> Any:
    """Recursively sanitize every string leaf (and key) of a kept value (§V18).

    A kept value may be a structured dict/list (e.g. ``abilities``, ``immunities``,
    ``specialProperties``, route ``checkpoints``) whose nested strings are just as
    untrusted as top-level ones: control/format/bidi chars must be stripped and
    every string length-capped before the value is JSON-encoded into a ``*_json``
    column and later surfaced to a client. Non-string scalars pass through.
    """
    if isinstance(value, str):
        return sanitize_text(value, max_length=max_length)
    if isinstance(value, Mapping):
        return {
            sanitize_text(str(k), max_length=max_length): sanitize_value(v, max_length=max_length)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [sanitize_value(item, max_length=max_length) for item in value]
    return value


def apply_allowlist(
    raw: Mapping[str, Any],
    allowed: Collection[str],
    *,
    max_length: int = DEFAULT_MAX_TEXT_LENGTH,
) -> AllowlistResult:
    """Keep only ``allowed`` keys from ``raw``; sanitize kept values (§V18).

    Every kept value is sanitized recursively (:func:`sanitize_value`): string
    leaves nested inside kept dict/list values are stripped of control chars and
    length-capped, not just top-level strings. Keys outside the allowlist are
    dropped and reported (§21.2 unknown-field logging; §V18 exclusion).
    """
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in raw.items():
        if key not in allowed:
            dropped.append(key)
        else:
            kept[key] = sanitize_value(value, max_length=max_length)
    return AllowlistResult(kept=kept, dropped=sorted(dropped))


def allowlist_blackboard(raw: Any) -> list[dict[str, Any]] | None:
    """Strictly allowlist a ``blackboard`` list to ``{key, value, valueStr}`` items.

    Read from the raw source (not a broadly-kept parent), so no unallowlisted
    parameter key or prose leaf is stored (§V31). ``None`` for an absent/empty list.
    The single home (§V37) for the blackboard projection shared by the skill/talent
    (operator) and module importers.
    """
    if not isinstance(raw, list):
        return None
    out = [
        apply_allowlist(item, BLACKBOARD_ALLOWLIST).kept for item in raw if isinstance(item, dict)
    ]
    return out or None
