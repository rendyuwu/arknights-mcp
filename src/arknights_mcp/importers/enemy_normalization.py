"""Raw ``arknights_assets_gamedata`` ENEMY shapes → normalized importer shapes.

The enemy half of the §V30 bridge (§V29; T66), split out of
:mod:`~arknights_mcp.importers.normalization` when that module crossed the §V38
hard cap: the enemy handbook/database and the level files are two source domains
with two shapes, and only two helpers cross between them (the stage-scoped inline
variant in a level file carries a partial ``enemyData``, so it reads the same stat
extractor -- one home, §V37).

* ``enemy_database.json`` is a top-level *id-keyed dict → list* (no ``"enemies"``
  wrapper); each level's stats live under ``enemyData.attributes.<stat>.m_value``
  with different names (``maxHp``≠``hp``, ``magicResistance``≠``res``,
  ``baseAttackTime``≠``attackInterval``); enemy motion is at
  ``enemyData.motion.m_value`` (the handbook has no ``motionType``).

Every transform is **shape-gated and idempotent**: given data already in the
normalized shape (the minimal synthetic fixture, or inline parser tests) it returns
the input unchanged, so only genuinely-real snapshots take the transform branch.
Prose/unknown fields are dropped here and re-checked by the parsers' allowlist +
sanitize step (§V18) — normalization never widens the field policy.

The field mappings below (``massLevel``→``weight``,
``lifePointReduce``→``lifePointReduction``) are **verified against live upstream**,
not merely inferred from the fixture: the CI-only
``tests/contract/test_live_upstream.py`` (§T68) imports a pinned
``arknights_assets_gamedata`` commit and asserts real 4-4 yields non-null
``hp``/``res``/``attackInterval``/``weight``/``lifePointReduction``/``motion``
(§V29, §V30).

§V113/B160 is the counter-example that shaped the newest three
(``rangeRadius``→``attackRange``, ``applyWay``→``targeting``, the nine typed
``<x>Immune`` flags→``immunities``): the parsers' allowlist admitted all three
normalized keys and this bridge mapped NONE of them, so the columns were 100% NULL
on every build ever promoted while every fixture test stayed green -- the fixture
hands the parser the already-normalized key the bridge never emitted. What is
allowlisted downstream is therefore not evidence that anything upstream reaches it;
``tests/contract/test_enemy_substrate.py`` counts these three against the pinned
snapshot, and ``importers.field_policy.ENEMY_KEY_HOMES`` declares each key's home
here (§V113 (a)).
"""

from __future__ import annotations

from typing import Any

#: Real ``enemyData.attributes`` stat key → normalized level key (B6 (a)).
_ENEMY_STAT_MAP: dict[str, str] = {
    "maxHp": "hp",
    "atk": "atk",
    "def": "def",
    "magicResistance": "res",
    "moveSpeed": "moveSpeed",
    "baseAttackTime": "attackInterval",
    "massLevel": "weight",
}

#: Real ``enemyData.<key>.m_value`` (outside ``attributes``) → normalized level key.
#:
#: ``rangeRadius``/``applyWay`` were mapped by NOTHING until §T210 even though the
#: allowlist admitted their normalized keys, so ``enemy_levels.attack_range`` and
#: ``.targeting`` were NULL by construction on every build (B160 (a)). Both are
#: ``enemyData`` scalars (not ``attributes`` cells) and both carry ``m_defined``, so
#: they inherit level-0 through :data:`_INHERITED_LEVEL_KEYS` like every other mapped
#: key (§V44). ``rangeRadius``'s sentinel is resolved after that inheritance, in
#: :func:`_finalize_level` — see there for why the order is load-bearing.
_ENEMY_DATA_SCALAR_MAP: dict[str, str] = {
    "lifePointReduce": "lifePointReduction",
    "rangeRadius": "attackRange",
    "applyWay": "targeting",
}

#: Real ``enemyData.attributes.<x>Immune`` flag → the emitted immunity token (§T210).
#:
#: Nine typed booleans upstream, one list on the wire (§V67): the flags answer one
#: question ("which control effects does this enemy ignore?") and nine sibling keys
#: would put nine near-identical facts on every level row. The token is derived from
#: the source key MECHANICALLY (drop ``Immune``, UPPER_SNAKE) rather than invented, and
#: the map is written out rather than computed so the emitted domain is a closed,
#: greppable set (§V96) and the wire never carries a camelCase source KEY as a VALUE
#: (§V71 d). Counted @pinned ``413a81a3`` (en): ``silenceImmune`` true 653,
#: ``levitateImmune`` 273, ``stunImmune`` 264 — never a value the classifier guessed.
_ENEMY_IMMUNITY_MAP: dict[str, str] = {
    "stunImmune": "STUN",
    "silenceImmune": "SILENCE",
    "sleepImmune": "SLEEP",
    "frozenImmune": "FROZEN",
    "levitateImmune": "LEVITATE",
    "disarmedCombatImmune": "DISARMED_COMBAT",
    "fearedImmune": "FEARED",
    "palsyImmune": "PALSY",
    "attractImmune": "ATTRACT",
}

#: Private normalized key carrying the per-level ``{token: bool}`` immunity flags
#: between :func:`_normalize_enemy_level` and :func:`_finalize_level`. It never
#: reaches a parser: :func:`_finalize_level` replaces it with the ``immunities`` list,
#: and the field allowlist would drop it regardless (§V18).
_IMMUNITY_FLAGS_KEY = "_immunityFlags"


def _m_value(wrapped: Any) -> Any:
    """Unwrap a real ``{"m_defined": ..., "m_value": ...}`` cell to its value.

    Real attribute/scalar fields are wrapped; a plain value (already normalized)
    passes through. ``None`` for anything else.
    """
    if isinstance(wrapped, dict):
        return wrapped.get("m_value")
    return wrapped


def _defined_m_value(wrapped: Any) -> tuple[bool, Any]:
    """A real ``{"m_defined", "m_value"}`` cell → ``(defined, value)`` (§V44/B38).

    ``m_defined`` gates whether *this* level entry actually sets the attribute:
    enemy_database level entries are deltas over level 0, and a ``m_defined:false``
    cell carries a sentinel ``m_value`` (typically ``0``) that means "unset at this
    level, inherit the base" — it MUST NOT be written as a real stat (that is B38:
    a level-1 flyer with ``magicResistance.m_defined=false`` would report ``res=0``
    instead of the level-0 value). Only an *explicit* ``m_defined:false`` marks a
    cell unset; a cell missing the flag (a plain/shorthand value) is defined.
    """
    if isinstance(wrapped, dict):
        return bool(wrapped.get("m_defined", True)), wrapped.get("m_value")
    return True, wrapped


def _database_is_normalized(database_raw: Any) -> bool:
    """True iff ``database_raw`` is already in the ``{"enemies": {...}}`` shape."""
    return isinstance(database_raw, dict) and isinstance(database_raw.get("enemies"), dict)


def _normalize_enemy_data_stats(enemy_data: dict[str, Any]) -> dict[str, Any]:
    """Extract the §V29-verified stat set from an ``enemyData``-shaped dict.

    Reads ``attributes.<stat>.m_value`` (via :data:`_ENEMY_STAT_MAP`) and the
    non-attribute scalars (:data:`_ENEMY_DATA_SCALAR_MAP`), emitting only the
    *defined* cells (``m_defined``, §V44): an undefined stat is omitted so the
    consumer inherits the base value rather than a sentinel ``0``. The single home
    (§V37) shared by the enemy-database level normalizer and the stage-scoped
    inline-variant extractor (§T80), both of which read the same ``enemyData`` shape
    (``overwrittenData`` is a partial ``enemyData``).
    """
    out: dict[str, Any] = {}
    attributes = enemy_data.get("attributes")
    if isinstance(attributes, dict):
        for real_key, norm_key in _ENEMY_STAT_MAP.items():
            if real_key in attributes:
                defined, value = _defined_m_value(attributes[real_key])
                if defined and value is not None:
                    out[norm_key] = value
    for real_key, norm_key in _ENEMY_DATA_SCALAR_MAP.items():
        if real_key in enemy_data:
            defined, value = _defined_m_value(enemy_data[real_key])
            if defined and value is not None:
                out[norm_key] = value
    return out


def _defined_motion(raw: Any) -> str | None:
    """A defined ``{m_defined, m_value}`` motion cell → its string, else ``None``.

    An undefined (``m_defined:false``) or absent motion is dropped so a variant
    inherits the base enemy's motion (§V44 semantics extended to §T80 variants).
    """
    defined, value = _defined_m_value(raw)
    return value if defined and isinstance(value, str) and value else None


def _immunity_flags(enemy_data: dict[str, Any]) -> dict[str, bool]:
    """The level's DEFINED immunity flags as ``{token: bool}`` (§V44; §T210).

    Only cells this level actually defines are returned: an ``m_defined:false`` flag
    means "unset at this level", so the base level's answer for THAT flag stands
    (§V44). The distinction is per-CELL, not per-list — 7 real level entries redefine
    a subset of their base's flags, and one (``enemy_1562_cjtaot`` level 1) defines
    only ``stunImmune`` while its base also carries silence + sleep, so replacing the
    whole list would publish a false "confirmed not immune" for the two it never
    mentioned (the B38 class, one container up).
    """
    attributes = enemy_data.get("attributes")
    if not isinstance(attributes, dict):
        return {}
    flags: dict[str, bool] = {}
    for real_key, token in _ENEMY_IMMUNITY_MAP.items():
        if real_key not in attributes:
            continue
        defined, value = _defined_m_value(attributes[real_key])
        if defined and isinstance(value, bool):
            flags[token] = value
    return flags


def _normalize_enemy_level(raw_level: Any) -> dict[str, Any]:
    """One real level entry ``{level, enemyData:{attributes,...}}`` → normalized dict."""
    out: dict[str, Any] = {}
    if not isinstance(raw_level, dict):
        return out
    level = raw_level.get("level")
    if isinstance(level, bool | int | float):
        out["level"] = level
    enemy_data = raw_level.get("enemyData")
    if not isinstance(enemy_data, dict):
        return out
    out.update(_normalize_enemy_data_stats(enemy_data))
    flags = _immunity_flags(enemy_data)
    if flags:
        out[_IMMUNITY_FLAGS_KEY] = flags
    return out


#: Normalized level keys that inherit the base (level-0) value when a higher
#: level does not redefine them (§V44/B38). Kept in sync with the two stat maps.
_INHERITED_LEVEL_KEYS: tuple[str, ...] = (
    *_ENEMY_STAT_MAP.values(),
    *_ENEMY_DATA_SCALAR_MAP.values(),
)


def _apply_level_deltas(levels: list[dict[str, Any]]) -> None:
    """Backfill each higher level's unset mapped stats from level 0 (§V44/B38).

    Enemy_database level entries are deltas: a higher level (variant) only carries
    the attributes it changes, and ``_normalize_enemy_level`` now drops the ones it
    leaves ``m_defined:false``. Those inherit the base (``level == 0``) value in the
    real game, so we copy any missing mapped key from the base level. Mutates the
    list in place; a single-level enemy (no base to inherit past) is unchanged.
    """
    if not levels:
        return
    base = next((lvl for lvl in levels if lvl.get("level") == 0), levels[0])
    base_flags = base.get(_IMMUNITY_FLAGS_KEY)
    for lvl in levels:
        if lvl is base:
            continue
        for key in _INHERITED_LEVEL_KEYS:
            if key not in lvl and key in base:
                lvl[key] = base[key]
        # The immunity flags inherit per CELL, not per key: a level that redefines one
        # flag leaves the other eight at the base's answer (§V44), so this MERGES the
        # two dicts instead of copying whichever one is present.
        if isinstance(base_flags, dict):
            merged = {**base_flags, **lvl.get(_IMMUNITY_FLAGS_KEY, {})}
            lvl[_IMMUNITY_FLAGS_KEY] = merged


def _finalize_level(level: dict[str, Any]) -> None:
    """Resolve a normalized level's sentinel + list-shaped keys, in place (§T210).

    Runs AFTER :func:`_apply_level_deltas`, and the order is load-bearing in both
    directions:

    * ``attackRange`` -- upstream writes ``rangeRadius.m_value = -1.0`` (420 of the
      1170 DEFINED en cells, and never any other negative) for "this enemy has no
      attack radius". That is a mask sentinel, not a distance, so it must never be
      stored as one (§V103). It is stripped here rather than at extraction because a
      level that DEFINES the sentinel has answered the question: dropping it earlier
      would leave the key missing at the delta step, and the level would then inherit
      the base's real radius (§V44) -- turning "no range" into a fabricated reach.
    * ``immunities`` -- the merged ``{token: bool}`` flags become the list §V67
      describes: ``[]`` when the source DEFINED flags and every one is false
      (confirmed none), and the key is ABSENT when neither this level nor its base
      ever defined one (not-in-source). A sorted list keeps the build byte-
      deterministic (§V91).
    """
    attack_range = level.get("attackRange")
    if isinstance(attack_range, int | float) and not isinstance(attack_range, bool):
        if attack_range < 0:
            del level["attackRange"]
    elif "attackRange" in level:
        del level["attackRange"]  # a non-numeric radius is not a distance either

    flags = level.pop(_IMMUNITY_FLAGS_KEY, None)
    if isinstance(flags, dict) and flags:
        level["immunities"] = sorted(token for token, immune in flags.items() if immune)


def normalize_enemy_database(database_raw: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """Real id-keyed enemy DB → normalized ``{"enemies": {...}}`` + ``{id: motion}``.

    Returns the normalized database and a map of enemy id → motion string extracted
    from ``enemyData.motion.m_value`` (used to backfill the handbook, which has no
    ``motionType`` in the real schema). Idempotent: an already-normalized database
    passes through unchanged with an empty motion map (its motion already lives in
    the handbook).
    """
    if _database_is_normalized(database_raw):
        return database_raw, {}
    if not isinstance(database_raw, dict):
        return {"enemies": {}}, {}

    enemies: dict[str, Any] = {}
    motion_by_id: dict[str, str] = {}
    for game_id, raw_levels in database_raw.items():
        if not isinstance(game_id, str) or not isinstance(raw_levels, list):
            continue
        levels = [_normalize_enemy_level(rl) for rl in raw_levels]
        _apply_level_deltas(levels)  # §V44/B38: unset higher-level stats inherit level 0
        for level in levels:
            _finalize_level(level)  # §V103 sentinel + §V67 list shape, after inheritance
        enemies[game_id] = {"levels": levels}
        for rl in raw_levels:
            enemy_data = rl.get("enemyData") if isinstance(rl, dict) else None
            motion = _m_value(enemy_data.get("motion")) if isinstance(enemy_data, dict) else None
            if isinstance(motion, str) and motion:
                motion_by_id[game_id] = motion
                break  # motion is constant across a given enemy's level variants
    return {"enemies": enemies}, motion_by_id


def _inject_motion(handbook_raw: Any, motion_by_id: dict[str, str]) -> Any:
    """Backfill ``motionType`` into each handbook entry from the enemy DB motion.

    Real handbooks carry no ``motionType`` (§V29); the motion source of truth is
    the enemy database. Returns a new handbook mapping so the input is never
    mutated. When ``motion_by_id`` is empty (already-normalized input) the handbook
    is returned unchanged. An existing ``motionType`` is never overwritten.
    """
    if not motion_by_id or not isinstance(handbook_raw, dict):
        return handbook_raw
    entries = handbook_raw.get("enemyData")
    if not isinstance(entries, dict):
        return handbook_raw

    new_entries: dict[str, Any] = {}
    for game_id, entry in entries.items():
        new_entries[game_id] = entry
        if isinstance(entry, dict) and "motionType" not in entry and game_id in motion_by_id:
            merged = dict(entry)
            merged["motionType"] = motion_by_id[game_id]
            new_entries[game_id] = merged
    # An enemy present only in the stats DB (no handbook entry) still carries motion.
    for game_id, motion in motion_by_id.items():
        if game_id not in new_entries:
            new_entries[game_id] = {"enemyId": game_id, "motionType": motion}

    out = dict(handbook_raw)
    out["enemyData"] = new_entries
    return out


def normalize_enemy_sources(handbook_raw: Any, database_raw: Any) -> tuple[Any, Any]:
    """Bridge the real enemy handbook + database to the normalized parser shapes.

    Returns ``(handbook_norm, database_norm)`` ready for
    :func:`arknights_mcp.importers.enemies.parse_enemies`. Idempotent on
    already-normalized input (§V29, §V30).
    """
    database_norm, motion_by_id = normalize_enemy_database(database_raw)
    handbook_norm = _inject_motion(handbook_raw, motion_by_id)
    return handbook_norm, database_norm


def normalize_kengxxiao_enemy_database(
    database_raw: Any,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Kengxxiao CN enemy DB (KV-list shape) → normalized ``{"enemies": {...}}`` + motion.

    Kengxxiao's ``enemy_database.json`` wraps its enemies as a list of
    ``{"Key": <id>, "Value": [<levels>]}`` pairs (§T69), not the top-level id-keyed
    dict ``arknights_assets_gamedata`` uses (§V29). The *inner* level shape
    (``enemyData.attributes.<stat>.m_value``, ``enemyData.motion.m_value``) is the
    same, so this reshapes the KV list into the id-keyed dict and delegates to the
    shared per-level normalizer (§V30: the one raw→normalized bridge home). Pure
    JSON→JSON — never persisted into a build (§C: kengxxiao is CI-only, never a
    runtime dep, never overrides the primary source). Idempotent on
    already-normalized input.
    """
    if _database_is_normalized(database_raw):
        return database_raw, {}
    if not isinstance(database_raw, dict):
        return {"enemies": {}}, {}
    pairs = database_raw.get("enemies")
    if not isinstance(pairs, list):
        return {"enemies": {}}, {}
    id_keyed: dict[str, Any] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        game_id = pair.get("Key")
        raw_levels = pair.get("Value")
        if not isinstance(game_id, str) or not game_id or not isinstance(raw_levels, list):
            continue
        id_keyed[game_id] = raw_levels
    return normalize_enemy_database(id_keyed)


__all__ = [
    "normalize_enemy_sources",
    "normalize_enemy_database",
    "normalize_kengxxiao_enemy_database",
]
