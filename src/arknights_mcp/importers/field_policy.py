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
#: 6: T127/§V65 added ``description`` to SKILL_LEVEL_ALLOWLIST + TALENT_CANDIDATE_ALLOWLIST
#:    (effect-description TEMPLATE import, ADR 0010 ceiling check: mechanic text that
#:    references the blackboard keys is imported + emitted alongside the blackboard;
#:    operator/module lore, story, voice, and wiki/community prose stay excluded, §V16).
#:    Module trait/talent-change templates ride the field-by-field module parser
#:    (modules.py ``_trait_change``/``_talent_change``), not a new frozenset here.
#: 7: T182/§V88 added SKIN_ALLOWLIST + DISPLAY_SKIN sub-allowlist (ADR 0015: named
#:    skin gallery -- skin/char/tmpl/portrait ids + short outfit labels only; the
#:    displaySkin prose leaves content/dialog/usage/description/drawerList stay
#:    excluded, §V16/§V18 metadata-only ceiling).
#: 8: T179 review-fix added ``zoneNameSecond`` to ZONE_ALLOWLIST: the REAL zone_table
#:    shape (tests/fixtures/stage_4_4_real) carries ``zoneID``+``zoneNameSecond`` and
#:    no ``zoneName``, so the zone display name -- the §T179 stage search alias --
#:    imported NULL from a real snapshot. Name-only metadata, same §V18 ceiling.
#: 9: T193/§V97 (B130) -- no allowlist change; the SANITIZE TRANSFORM every kept value
#:    passes through changed, so the same allowlisted fields now store different bytes.
#:    A removed control char leaves a space instead of welding the words either side
#:    (`"...additional target\nUnlimited duration"` no longer stores as
#:    `"...targetUnlimited duration"`). Bumped beside TRANSFORM_VERSION so the repaired
#:    bytes actually promote over an unchanged snapshot (§V92).
#: 10: T204/§V109 (B154) -- no allowlist change either; §V65 (a) effect TEMPLATES now
#:    bypass this module's cap entirely and go through ``util.text.clean_template_text``
#:    (tags stripped BEFORE a 1024-char cap), so 349 EN templates that this allowlist
#:    silently cut mid-sentence at 512 now import whole. Same §V92 reasoning as 9.
#: 11: T205/§V110 (B155) added ACTIVITY_ALLOWLIST (event id + TITLE): ``zone_table``
#:    carries only the sub-zone SUBTITLE, so the event name a client searches by
#:    ("Lone Trail") was in no imported field at all -- 120 EN titles unreachable.
#:    New stored bytes (``zones.event_name``), so the bump is what makes §V92 promote
#:    the rebuilt content over an unchanged snapshot.
#: 12: T210/§V113 (B160) added ``damageType`` to ENEMY_HANDBOOK_ALLOWLIST: the enemy's
#:    damage kind moved upstream from ``attackType`` (present and ``null`` on 1585/1585
#:    real entries) to a typed LIST, and 42 enemies deal both ``PHYSIC`` and ``MAGIC``,
#:    so the retired scalar could not have carried it even when it was populated. Bumped
#:    beside TRANSFORM_VERSION: the same task also taught ``normalization.py`` to emit
#:    ``attackRange``/``targeting``/``immunities``, three keys this allowlist had already
#:    admitted while no bridge mapping filled them (the §V113 (a) shape).
FIELD_POLICY_VERSION = "12"

#: Fact region -> name/alias locale tag (§V57; B46/§V59). A region's canonical
#: strings are in that region's language: an en entity's name is English (locale
#: ``en``), a cn entity's name is Chinese (locale ``zh``). Two consumers share this
#: single home (§V37): ``penguin_drops`` picks ``name_i18n.<locale>`` for an item's
#: display name, and ``operators`` stamps the same tag on each locale alias (T98).
#: The locale tag is NOT a fact region -- an en/cn entity still returns its OWN
#: region facts (§V57). Migration 0011's SQL backfill mirrors this cn->zh coupling.
#: The extra-locale (jp/kr) alias axis that also consumed this file is RETIRED (§V57,
#: T156 -- founder 2026-07-23, EN+CN only); this map survives only for its two live
#: consumers: penguin item ``name_i18n`` display (§V59) and the operator self-alias
#: locale stamp (§T98). Both feed EN+CN name search, not a ja/ko filter.
REGION_TO_NAME_LOCALE: dict[str, str] = {"en": "en", "cn": "zh"}

# --- Allowlisted SOURCE fields per record type (V18) -------------------------
# Prose fields (e.g. "description") are intentionally absent and thus excluded.

ENEMY_HANDBOOK_ALLOWLIST: frozenset[str] = frozenset(
    {"enemyId", "name", "enemyLevel", "attackType", "damageType", "motionType"}
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


@dataclass(frozen=True)
class SourceKeyHome:
    """Where an allowlisted key's value really comes from upstream (§V113 (a); B160).

    An allowlist is a *claim* about what the importer reads (§V98). A key admitted
    here whose value no real snapshot ever produces is worse than a missing key: it
    creates a column that is NULL by construction, which every downstream consumer
    then reads through its own §V26 "field missing -> reduce confidence" arm and so
    reports **clean**. Nothing below the build can witness it; only a count over the
    built DB can. This declaration is that count, enumerated per key.

    ``status`` is one of:

    * ``live`` -- the §V30 bridge emits the key and the column is non-degenerate on
      the real build (the count is in ``counted``).
    * ``bridge_gap`` -- upstream carries ``home`` but ``normalization.py`` never maps
      it, so the column is 100% NULL (T210 (a) closes these).
    * ``retired`` -- the key still exists upstream and its VALUE is ``null``/absent on
      100% of real records; ``home`` names the field that replaced it (T210 (b)).
    * ``no_home`` -- DEAD BY DATA: no real source carries this at all, so every
      consumer owes a §V26 limitation rather than a silent NULL.
    * ``not_stored`` -- allowlisted and read, but feeds no column (kept for the
      provenance record only).

    Counts are @pinned ``413a81a3`` (en) + build ``2026-07-29T065116Z-en-cn`` -- the
    first build in which the six B160 columns are not all empty -- never assumed: a
    field that is populated today is populated by DATA, not by construction (§V96
    sibling). The "was 0/4343" figures are B160's own, on the last build before §T210.
    """

    home: str | None
    status: str
    counted: str


#: §V113 (a): every ENEMY_HANDBOOK_ALLOWLIST + ENEMY_LEVEL_ALLOWLIST key -> its real
#: upstream home. Pinned by ``tests/contract/test_column_liveness.py``, which fails
#: both ways: a key added to either allowlist without an entry here, and an entry
#: whose ``status`` disagrees with what the §V30 bridge actually emits.
ENEMY_KEY_HOMES: Mapping[str, SourceKeyHome] = {
    # -- handbook ------------------------------------------------------------
    "enemyId": SourceKeyHome(
        "enemyData.<id>.enemyId", "not_stored", "1585/1585 present; the id IS the dict key"
    ),
    "name": SourceKeyHome("enemyData.<id>.name", "live", "enemies.display_name 3294/3879"),
    "enemyLevel": SourceKeyHome(
        "enemyData.<id>.enemyLevel", "live", "enemies.enemy_class 3294/3879 NORMAL|ELITE|BOSS"
    ),
    "attackType": SourceKeyHome(
        "enemyData.<id>.damageType",  # the field that REPLACED it
        "retired",
        "attackType null 1585/1585; damageType [PHYSIC] 1022|[MAGIC] 338|[NO_DAMAGE] 180|both 42",
    ),
    "damageType": SourceKeyHome(
        "enemyData.<id>.damageType",
        "live",
        "enemies.damage_types_json 3294/3879 (en+cn): [PHYSIC] 2123|[MAGIC] 704|"
        "[NO_DAMAGE] 372|[PHYSIC,MAGIC] 89|[HEAL] 4|[MAGIC,HEAL] 2",
    ),
    "motionType": SourceKeyHome(
        "enemyData.motion.m_value (enemy_database; absent from the real handbook)",
        "live",
        "enemies.motion_type 3879/3879 WALK 3547|FLY 332",
    ),
    # -- enemy_database level entries ----------------------------------------
    "level": SourceKeyHome("<level>.level", "live", "enemy_levels 4343 rows"),
    "hp": SourceKeyHome("enemyData.attributes.maxHp.m_value", "live", "4303/4343"),
    "atk": SourceKeyHome("enemyData.attributes.atk.m_value", "live", "4227/4343"),
    "def": SourceKeyHome("enemyData.attributes.def.m_value", "live", "4230/4343"),
    "res": SourceKeyHome("enemyData.attributes.magicResistance.m_value", "live", "4184/4343"),
    "attackInterval": SourceKeyHome(
        "enemyData.attributes.baseAttackTime.m_value", "live", "4204/4343"
    ),
    "moveSpeed": SourceKeyHome("enemyData.attributes.moveSpeed.m_value", "live", "4287/4343"),
    "weight": SourceKeyHome("enemyData.attributes.massLevel.m_value", "live", "4192/4343"),
    "lifePointReduction": SourceKeyHome("enemyData.lifePointReduce.m_value", "live", "3998/4343"),
    "attackRange": SourceKeyHome(
        "enemyData.rangeRadius.m_value",
        "live",
        "enemy_levels.attack_range 1757/4343 (was 0/4343); upstream defines 1170/2036 en of "
        "which 420 are the -1.0 no-radius sentinel §V103 keeps out of a distance column, so "
        "the stored column carries 0 negatives",
    ),
    "targeting": SourceKeyHome(
        "enemyData.applyWay.m_value",
        "live",
        "enemy_levels.targeting 4249/4343 (was 0/4343): MELEE 2002|RANGED 1319|NONE 669|ALL 259",
    ),
    "immunities": SourceKeyHome(
        "enemyData.attributes.{stun,silence,sleep,frozen,levitate,disarmedCombat,feared,palsy,"
        "attract}Immune.m_value",
        "live",
        "enemy_levels.immunities_json 3018/4343 (was 0/4343), of which 1205 are [] = the "
        "source defined the flags and set none; all nine tokens occur",
    ),
    # The two below stay allowlisted with no home on purpose (§V113's declared-dead
    # arm): the keys are what a future upstream would arrive under, and this entry is
    # the counted reason nothing fills them today. Their three consumer rules were
    # RETIRED in T210 (c) rather than re-grounded on a guess -- see
    # ``analyzers.rules.RETIRED_RULES`` and ADR 0016.
    "blockBehavior": SourceKeyHome(
        None,
        "no_home",
        "attributes.blockCnt m_defined:false 2031/2036; the only unblockable statement upstream "
        'is prose abilityList[].text ("Cannot be blocked." x37), which §V26 forbids reading',
    ),
    "abilities": SourceKeyHome(
        None,
        "no_home",
        "no typed ability vocabulary upstream: abilityList[].text is prose (§V26) and "
        "skills[].prefabKey is free-form (626 distinct over 1378 rows, unpinned)",
    ),
}

#: The §V113 statuses that mean the column carries real values today. Any other
#: status is a column a consumer must treat as absent (§V26), never as "none".
LIVE_KEY_STATUSES: frozenset[str] = frozenset({"live", "not_stored"})

# ``zoneName`` is the synthetic-fixture key; the real zone_table names a zone via
# ``zoneNameSecond`` (see FIELD_POLICY_VERSION note 8). Both are NAME-only (§V18).
#
# §V110/B155: what that key holds is the sub-zone SUBTITLE, not the event title. The
# Lone Trail zones are named "The Coming of The Future" / "The Lingering of the Past" /
# "The Pursuing of the Present"; the string "Lone Trail" appears nowhere in zone_table
# (and ``zoneNameFirst`` holds 19 mainline "Episode N" labels, not event titles). The
# title comes from ACTIVITY_ALLOWLIST below.
ZONE_ALLOWLIST: frozenset[str] = frozenset({"zoneId", "zoneName", "zoneNameSecond", "type"})

#: Event/activity fields kept from ``activity_table.json`` ``basicInfo`` (§V110/T205).
#: The event TITLE is the name a client searches an event by, and it lives only here
#: (B155). Deliberately id + name ONLY: the same record carries schedule prose, shop /
#: medal ids, and display flags that nothing reads -- the §V18 metadata ceiling. The
#: ``zoneToActivity`` map that joins a zone to its activity is a flat id->id map, not a
#: record, so it has no allowlist of its own.
ACTIVITY_ALLOWLIST: frozenset[str] = frozenset({"id", "name"})

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

#: One ``skill_table`` level entry. ``description`` is the in-game skill effect
#: TEMPLATE -- mechanic text that references the sibling ``blackboard`` keys (e.g.
#: ``stuns for {stun} seconds``); it is imported + emitted alongside the blackboard so
#: the values are grounded (§V65 path (a), ADR 0010). It is NOT lore/story/wiki prose
#: (that stays excluded, §V16); it is sanitized + control-stripped + length-capped as
#: untrusted data (§V18). ``spData`` is a nested numeric block (SP_DATA_ALLOWLIST).
SKILL_LEVEL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "name",
        "rangeId",
        "skillType",
        "durationType",
        "duration",
        "spData",
        "blackboard",
        "description",
    }
)

#: The ``spData`` sub-block of a skill level (all numeric/enum).
SP_DATA_ALLOWLIST: frozenset[str] = frozenset(
    {"spType", "spCost", "initSp", "maxChargeTime", "increment"}
)

#: One talent ``candidates[]`` variant. ``description`` is the in-game talent effect
#: TEMPLATE -- mechanic text referencing the sibling ``blackboard`` keys -- imported +
#: emitted alongside the blackboard for grounding (§V65 path (a), ADR 0010); it is NOT
#: lore/story prose (that stays excluded, §V16) and is sanitized + capped (§V18). The
#: ``name`` is a short gameplay label (kept, like a skill/operator display name).
#: ``blackboard`` holds numeric params.
TALENT_CANDIDATE_ALLOWLIST: frozenset[str] = frozenset(
    {"name", "unlockCondition", "requiredPotentialRank", "prefabKey", "blackboard", "description"}
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

#: Scalar skin-gallery fields from a ``skin_table.json`` ``charSkins`` entry (§V18;
#: §T182; §V88 named gallery, ADR 0015). All structural: ``skinId`` is the skin's
#: stable game id (identity, §V17 record key), ``charId`` the owning operator's char
#: id (BASE operator for alt-form skins -- the soft-resolve key), ``tmplId`` the
#: alt-form discriminator (equals ``charId`` except on alternate playable forms, e.g.
#: the Amiya family), ``portraitId`` the art-asset stem the §V63 mirror URL derives
#: from (skin/<portraitId>b.png -- derived at query time, never stored), and
#: ``isBuySkin`` the paid-outfit flag. ``displaySkin`` is deliberately ABSENT here
#: (it carries the §V18-forbidden prose leaves ``content``/``dialog``/``usage``/
#: ``description``/``drawerList``, so the allowlist itself must fail closed on it);
#: the importer reads it separately through DISPLAY_SKIN_ALLOWLIST below via
#: sub-extraction, the same nested-parent pattern as ``limitParam``/``dynMeta`` --
#: which are likewise absent from BANNER_ALLOWLIST. (No policy-version bump: the
#: stored record bytes are unchanged -- the importer already replaced the parent
#: with its sub-allowlisted block before writing provenance.)
SKIN_ALLOWLIST: frozenset[str] = frozenset(
    {"skinId", "charId", "tmplId", "portraitId", "isBuySkin"}
)

#: The ``displaySkin`` sub-block of a ``charSkins`` entry. ``skinName`` is the outfit's
#: short display name (NULL on default E0/E1/E2 art; kept + sanitized + length-capped
#: like an operator/banner name), ``skinGroupId`` the structural group enum
#: (``ILLUST_0/1/2`` = default E0/E1/E2 art vs an outfit-series id), ``skinGroupName``
#: the short series display label ("Default Outfit" / a collection name). Everything
#: else -- outfit flavor ``content``/``dialog``/``usage``/``description``, artist
#: ``drawerList``, ``modelName`` -- is prose/credit and deliberately ABSENT (§V16/§V18
#: metadata-only ceiling; ADR 0015).
DISPLAY_SKIN_ALLOWLIST: frozenset[str] = frozenset({"skinName", "skinGroupId", "skinGroupName"})


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
