"""Raw ``arknights_assets_gamedata`` → normalized importer shapes (§V29, §V30; T66).

The upstream schema differs *structurally* from the normalized shapes the
allowlisted parsers (:mod:`~arknights_mcp.importers.enemies`,
:mod:`~arknights_mcp.importers.levels`) consume (§V29). This module is the single
explicit bridge mandated by §V30: it reshapes raw JSON so the parsers stay stable
and unit-testable, and it performs **no** database or network I/O — pure
JSON→JSON. B6 records the concrete divergences this bridges:

* ``enemy_database.json`` is a top-level *id-keyed dict → list* (no ``"enemies"``
  wrapper); each level's stats live under ``enemyData.attributes.<stat>.m_value``
  with different names (``maxHp``≠``hp``, ``magicResistance``≠``res``,
  ``baseAttackTime``≠``attackInterval``); enemy motion is at
  ``enemyData.motion.m_value`` (the handbook has no ``motionType``).
* ``stage_table.levelId`` is a Title-case, extension-less reference
  (``Obt/Main/level_main_04-04``) that must be lowercased and rewritten to the
  actual snapshot path (``gamedata/levels/obt/main/level_main_04-04.json``).
* level ``mapData`` is a 2D ``map`` grid of indices into a flat ``tiles`` list
  (tiles carry no ``x``/``y``; ``passableMask``≠``passable``); ``routes``/``waves``
  carry no positional index; a wave action names its enemy under ``key`` (resolved
  via the level's ``enemyDbRefs``/``enemies``), not ``enemyId``.

Every transform is **shape-gated and idempotent**: given data already in the
normalized shape (the minimal synthetic fixture, or inline parser tests) it
returns the input unchanged, so only genuinely-real snapshots take the transform
branch. Prose/unknown fields are dropped here and re-checked by the parsers'
allowlist + sanitize step (§V18) — normalization never widens the field policy.

The field mappings below (``massLevel``→``weight``,
``lifePointReduce``→``lifePointReduction``, ``preDelay``→``spawnTime``,
``maxTimeWaitingForNextWave``→``maxTimeWaiting``, and the positional route/wave
index fallbacks) are **verified against live upstream**, not merely inferred from
the fixture: the CI-only ``tests/contract/test_live_upstream.py`` (§T68) imports a
pinned ``arknights_assets_gamedata`` commit and asserts real 4-4 yields non-null
``hp``/``res``/``attackInterval``/``weight``/``lifePointReduction``/``motion`` plus
non-empty tiles/spawns/``stage_enemies`` (§V29, §V30).
"""

from __future__ import annotations

from typing import Any

# --- enemy_database + handbook -------------------------------------------------

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
_ENEMY_DATA_SCALAR_MAP: dict[str, str] = {
    "lifePointReduce": "lifePointReduction",
}


def _m_value(wrapped: Any) -> Any:
    """Unwrap a real ``{"m_defined": ..., "m_value": ...}`` cell to its value.

    Real attribute/scalar fields are wrapped; a plain value (already normalized)
    passes through. ``None`` for anything else.
    """
    if isinstance(wrapped, dict):
        return wrapped.get("m_value")
    return wrapped


def _database_is_normalized(database_raw: Any) -> bool:
    """True iff ``database_raw`` is already in the ``{"enemies": {...}}`` shape."""
    return isinstance(database_raw, dict) and isinstance(database_raw.get("enemies"), dict)


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
    attributes = enemy_data.get("attributes")
    if isinstance(attributes, dict):
        for real_key, norm_key in _ENEMY_STAT_MAP.items():
            if real_key in attributes:
                value = _m_value(attributes[real_key])
                if value is not None:
                    out[norm_key] = value
    for real_key, norm_key in _ENEMY_DATA_SCALAR_MAP.items():
        if real_key in enemy_data:
            value = _m_value(enemy_data[real_key])
            if value is not None:
                out[norm_key] = value
    return out


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


# --- stage levelId → resolvable snapshot path ---------------------------------

_LEVEL_PREFIX = "gamedata/levels/"
_LEVEL_SUFFIX = ".json"


def normalize_level_id(level_id: str | None) -> str | None:
    """Real Title-case ``levelId`` → the actual snapshot file path (§V29; B6 (b)).

    ``Obt/Main/level_main_04-04`` → ``gamedata/levels/obt/main/level_main_04-04.json``
    (lowercase + ``gamedata/levels/`` prefix + ``.json``). A path already in the
    resolvable form is returned unchanged (idempotent). The result is always forced
    under ``gamedata/levels/`` so a stage cannot smuggle a reference to an excel
    table through this field (L8); traversal in the source id is left for the
    adapter's path validator to reject.
    """
    if level_id is None:
        return None
    stripped = level_id.strip()
    if not stripped:
        return None
    if stripped.startswith(_LEVEL_PREFIX) and stripped.endswith(_LEVEL_SUFFIX):
        return stripped
    body = stripped.lower().removeprefix(_LEVEL_PREFIX).removesuffix(_LEVEL_SUFFIX)
    return f"{_LEVEL_PREFIX}{body}{_LEVEL_SUFFIX}"


def is_clean_level_path(path: str) -> bool:
    """Whether a *normalized* levelId path is a safe level-file reference (§V36).

    ``normalize_level_id`` always forces the ``gamedata/levels/`` prefix, so after
    normalization a crafted ``levelId`` can neither point at an excel table nor
    escape the tree -- but a mangled reference (e.g. ``gamedata/excel/...`` folded
    back under the levels prefix via ``..``, or a traversal fragment) must still be
    dropped rather than read. A clean path is under ``gamedata/levels/`` with a
    ``.json`` suffix, a non-empty body, no traversal segment, and no nested
    ``gamedata``/``excel`` segment. Shared by the network discovery gate
    (:mod:`~arknights_mcp.sources.arknights_assets`) and the local import path
    (:func:`~arknights_mcp.importers.stages.import_stages`) so both confine the
    levels tree identically (§V37, §V36; B17).
    """
    if not path.startswith(_LEVEL_PREFIX) or not path.endswith(_LEVEL_SUFFIX):
        return False
    body = path[len(_LEVEL_PREFIX) : -len(_LEVEL_SUFFIX)]
    if not body:
        return False
    segments = body.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        return False
    return not any(seg in ("gamedata", "excel") for seg in segments)


# --- level file (map / tiles / routes / waves / spawns) -----------------------

#: ``passableMask`` string values that mean at least one mover can enter the tile.
_MASK_PASSABLE: frozenset[str] = frozenset({"ALL", "FLY_ONLY", "WALK_ONLY"})
#: ``passableMask`` string values that mean the tile is impassable to everything.
_MASK_IMPASSABLE: frozenset[str] = frozenset({"NONE", ""})


def _passable_from_mask(mask: Any) -> bool | None:
    """Map real ``passableMask`` → the normalized ``passable`` bool (§V29; B6 (c)).

    ``passable`` here means "traversable by at least one movement mode"; the
    single boolean cannot express fly-only vs walk-only, which is recorded as a
    limitation rather than fabricated. Unknown values yield ``None``.
    """
    if isinstance(mask, bool):
        return mask
    if isinstance(mask, str):
        upper = mask.strip().upper()
        if upper in _MASK_PASSABLE:
            return True
        if upper in _MASK_IMPASSABLE:
            return False
        return None
    if isinstance(mask, int):
        return mask != 0  # 0 == impassable; any positive mask admits some mover
    return None


def _level_is_grid(level_raw: Any) -> bool:
    """True iff the level uses the real 2D ``mapData.map`` grid (vs synthetic x/y)."""
    if not isinstance(level_raw, dict):
        return False
    map_data = level_raw.get("mapData")
    return isinstance(map_data, dict) and isinstance(map_data.get("map"), list)


def _inline_prefab_key(ref: dict[str, Any]) -> str | None:
    """A ``useDb:false`` ref's base enemy id from ``overwrittenData.prefabKey.m_value``.

    ``None`` when the ref carries no prefab base (leaves the spawn to fail closed at
    the level importer's cross-reference check — B37).
    """
    overwritten = ref.get("overwrittenData")
    if not isinstance(overwritten, dict):
        return None
    prefab = overwritten.get("prefabKey")
    if not isinstance(prefab, dict):
        return None
    value = prefab.get("m_value")
    return value if isinstance(value, str) and value else None


def _enemy_ref_map(level_raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build ``{action key → {id, variant_id, level}}`` from ``enemyDbRefs``/``enemies``.

    A wave action names its enemy under ``key``; the level declares which enemies
    (and their DB level variant) it uses in ``enemyDbRefs`` (or ``enemies``). Refs
    split two ways on the ``useDb`` flag (§V43, B37) — resolution keys on that flag
    per-ref, never on id-membership (the same id may be db-backed in one stage and
    inline in another; the same inline id may resolve to a different base across
    levels ∴ no global inline→enemy map exists):

    * ``useDb: true`` — the ref's ``id`` is a real ``enemy_database``/handbook
      enemy; the spawn resolves straight to it (``key`` normally equals that id).
    * ``useDb: false`` — a *level-inline* enemy variant (``enemy_..._a``/``_b``/
      ``_2a``…) whose stats live inline under ``overwrittenData`` and which is
      **never** in the enemy tables. Its ``overwrittenData.prefabKey`` names the
      base enemy it derives from (always a real enemy upstream). The spawn resolves
      to that base ``prefabKey`` so the cross-file FK holds; the original inline id
      is carried as ``variant_id`` for traceability (persisted via ``variantId`` in
      the allowlisted spawn ``source_fragment``). The inline ``overwrittenData``
      stats themselves are not modeled here (§V43 limitation; T80).
    """
    refs = level_raw.get("enemyDbRefs")
    if not isinstance(refs, list):
        refs = level_raw.get("enemies")
    if not isinstance(refs, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        rid = ref.get("id") or ref.get("key")
        if not isinstance(rid, str) or not rid:
            continue
        level = ref.get("level")
        enemy_id = rid
        variant_id: str | None = None
        if ref.get("useDb") is False:
            prefab = _inline_prefab_key(ref)
            if prefab is not None:
                enemy_id = prefab
                variant_id = rid
        out[rid] = {
            "id": enemy_id,
            "variant_id": variant_id,
            "level": level if isinstance(level, int) else 0,
        }
    return out


def _normalize_tiles(map_data: dict[str, Any]) -> tuple[list[dict[str, Any]], int, int]:
    """Grid ``map`` + flat ``tiles`` defs → normalized tiles with derived x/y."""
    grid = map_data.get("map")
    tile_defs = map_data.get("tiles")
    if not isinstance(grid, list) or not isinstance(tile_defs, list):
        return [], 0, 0
    height = len(grid)
    width = max((len(row) for row in grid if isinstance(row, list)), default=0)
    tiles: list[dict[str, Any]] = []
    for y, row in enumerate(grid):
        if not isinstance(row, list):
            continue
        for x, idx in enumerate(row):
            if not isinstance(idx, int) or isinstance(idx, bool):
                continue
            if not 0 <= idx < len(tile_defs):
                continue
            td = tile_defs[idx]
            if not isinstance(td, dict):
                continue
            tiles.append(
                {
                    "x": x,
                    "y": y,
                    "tileKey": td.get("tileKey"),
                    "heightType": td.get("heightType"),
                    "buildableType": td.get("buildableType"),
                    "passable": _passable_from_mask(td.get("passableMask")),
                }
            )
    return tiles, width, height


def _normalize_routes(level_raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Real routes (no ``routeIndex``) → normalized routes with a positional index."""
    routes: list[dict[str, Any]] = []
    raw_routes = level_raw.get("routes")
    if not isinstance(raw_routes, list):
        return routes
    for i, raw in enumerate(raw_routes):
        if not isinstance(raw, dict):
            continue
        route_index = raw.get("routeIndex")
        routes.append(
            {
                "routeIndex": route_index if isinstance(route_index, int) else i,
                "startPosition": raw.get("startPosition"),
                "endPosition": raw.get("endPosition"),
                "checkpoints": raw.get("checkpoints", []),
            }
        )
    return routes


def _normalize_action(
    action: dict[str, Any], ref_map: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """One real wave action → normalized spawn dict, or ``None`` for a non-spawn action.

    Only ``actionType == "SPAWN"`` actions describe an enemy entering the map. A
    level's waves interleave spawns with UI/scripting actions that *also* carry a
    ``key`` (B35): ``DISPLAY_ENEMY_INFO``/``PREVIEW_CURSOR`` name a real enemy (a
    codex/preview cue, not a spawn) and ``STORY``'s ``key`` is a story-asset path
    (e.g. ``activities/a001/tutorial_a001_01_a``), not an enemy id. Gating on the
    presence of ``key`` alone both fabricated phantom spawns from the enemy-info
    cues and leaked a ``STORY`` path as an enemy id that then failed the downstream
    cross-reference check. Verified against live upstream @``413a81a3``: ``SPAWN``
    is the sole enemy-spawning ``actionType`` (§V29).

    The enemy id comes from ``key`` (resolved via ``enemyDbRefs``; a spawn ``key``
    normally equals the enemy id); a spawn's level variant defaults to the ref's
    declared level. For a ``useDb:false`` inline variant the resolved ``enemyId`` is
    the ref's base ``prefabKey`` and the original inline id is emitted as
    ``variantId`` for traceability (§V43, B37).
    """
    if action.get("actionType") != "SPAWN":
        return None
    key = action.get("key")
    if not isinstance(key, str) or not key:
        return None
    ref = ref_map.get(key)
    enemy_id = ref["id"] if ref else key
    variant = ref["level"] if ref else action.get("level")
    variant_id = ref.get("variant_id") if ref else None
    spawn: dict[str, Any] = {
        "enemyId": enemy_id,
        "levelVariant": variant if isinstance(variant, int) else 0,
        "routeIndex": action.get("routeIndex"),
        "spawnTime": action.get("spawnTime", action.get("preDelay")),
        "count": action.get("count"),
        "interval": action.get("interval"),
        "spawnGroup": action.get("hiddenGroup") or action.get("randomSpawnGroupKey") or None,
        "hidden": bool(action.get("hiddenGroup")),
    }
    if variant_id is not None:
        spawn["variantId"] = variant_id
    return spawn


def _normalize_waves(
    level_raw: dict[str, Any], ref_map: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Real waves (no ``waveIndex``; ``key`` actions) → normalized waves."""
    waves: list[dict[str, Any]] = []
    raw_waves = level_raw.get("waves")
    if not isinstance(raw_waves, list):
        return waves
    for wi, raw in enumerate(raw_waves):
        if not isinstance(raw, dict):
            continue
        fragments_out: list[dict[str, Any]] = []
        raw_fragments = raw.get("fragments")
        for fragment in raw_fragments if isinstance(raw_fragments, list) else []:
            if not isinstance(fragment, dict):
                continue
            actions_out: list[dict[str, Any]] = []
            raw_actions = fragment.get("actions")
            for action in raw_actions if isinstance(raw_actions, list) else []:
                if not isinstance(action, dict):
                    continue
                normalized = _normalize_action(action, ref_map)
                if normalized is not None:
                    actions_out.append(normalized)
            fragments_out.append({"actions": actions_out})
        wave_index = raw.get("waveIndex")
        waves.append(
            {
                "waveIndex": wave_index if isinstance(wave_index, int) else wi,
                "preDelay": raw.get("preDelay"),
                "maxTimeWaiting": raw.get("maxTimeWaiting", raw.get("maxTimeWaitingForNextWave")),
                "fragments": fragments_out,
            }
        )
    return waves


def normalize_level(level_raw: Any) -> Any:
    """Bridge a real level file to the normalized shape :func:`parse_level` consumes.

    Idempotent: a synthetic level (tiles already carry x/y, no ``mapData.map`` grid)
    is returned unchanged. A real level (grid ``map``) is fully transformed —
    tiles gain derived x/y, ``passableMask``→``passable``, routes/waves gain
    positional indices, and wave ``key`` actions resolve to enemy ids (§V29; B6 (c)).
    """
    if not _level_is_grid(level_raw):
        return level_raw
    map_data = level_raw.get("mapData")
    map_data = map_data if isinstance(map_data, dict) else {}
    tiles, width, height = _normalize_tiles(map_data)
    ref_map = _enemy_ref_map(level_raw)
    return {
        "mapData": {
            "width": width,
            "height": height,
            "mapVersion": map_data.get("mapVersion"),
            "environment": map_data.get("environment", {}),
            "tiles": tiles,
        },
        "routes": _normalize_routes(level_raw),
        "waves": _normalize_waves(level_raw, ref_map),
    }


__all__ = [
    "normalize_enemy_sources",
    "normalize_enemy_database",
    "normalize_kengxxiao_enemy_database",
    "normalize_level_id",
    "normalize_level",
]
