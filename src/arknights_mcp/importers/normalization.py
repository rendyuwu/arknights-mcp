"""Raw ``arknights_assets_gamedata`` LEVEL shapes → normalized importer shapes.

The stage/level half of the normalization bridge; the enemy handbook + database half
lives in :mod:`~arknights_mcp.importers.enemy_normalization` (split at the file-size
hard cap). This module reshapes raw level JSON so the parsers stay stable and
unit-testable, and it performs **no** database or network I/O — pure JSON→JSON. The
concrete divergences it bridges:

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
allowlist + sanitize step — normalization never widens the field policy.

The field mappings below (``preDelay``→``spawnTime``,
``maxTimeWaitingForNextWave``→``maxTimeWaiting``, and the positional route/wave
index fallbacks) are **verified against live upstream**, not merely inferred from
the fixture: the CI-only ``tests/contract/test_live_upstream.py`` imports a
pinned ``arknights_assets_gamedata`` commit and asserts real 4-4 yields non-empty
tiles/spawns/``stage_enemies``.
"""

from __future__ import annotations

from typing import Any

from arknights_mcp.importers.enemy_normalization import (
    _defined_motion,
    _normalize_enemy_data_stats,
)
from arknights_mcp.importers.field_policy import OVERWRITTEN_DATA_ALLOWLIST, apply_allowlist

# --- stage levelId → resolvable snapshot path ---------------------------------

_LEVEL_PREFIX = "gamedata/levels/"
_LEVEL_SUFFIX = ".json"


def normalize_level_id(level_id: str | None) -> str | None:
    """Real Title-case ``levelId`` → the actual snapshot file path.

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
    """Whether a *normalized* levelId path is a safe level-file reference.

    ``normalize_level_id`` always forces the ``gamedata/levels/`` prefix, so after
    normalization a crafted ``levelId`` can neither point at an excel table nor
    escape the tree -- but a mangled reference (e.g. ``gamedata/excel/...`` folded
    back under the levels prefix via ``..``, or a traversal fragment) must still be
    dropped rather than read. A clean path is under ``gamedata/levels/`` with a
    ``.json`` suffix, a non-empty body, no traversal segment, and no nested
    ``gamedata``/``excel`` segment. Shared by the network discovery gate
    (:mod:`~arknights_mcp.sources.arknights_assets`) and the local import path
    (:func:`~arknights_mcp.importers.stages.import_stages`) so both confine the
    levels tree identically.
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
    """Map real ``passableMask`` → the normalized ``passable`` bool.

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
    the level importer's cross-reference check).
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
    split two ways on the ``useDb`` flag — resolution keys on that flag
    per-ref, never on id-membership (the same id may be db-backed in one stage and
    inline in another; the same inline id may resolve to a different base across
    levels, so no global inline→enemy map exists):

    * ``useDb: true`` — the ref's ``id`` is a real ``enemy_database``/handbook
      enemy; the spawn resolves straight to it (``key`` normally equals that id).
    * ``useDb: false`` — a *level-inline* enemy variant (``enemy_..._a``/``_b``/
      ``_2a``…) whose stats live inline under ``overwrittenData`` and which is
      **never** in the enemy tables. Its ``overwrittenData.prefabKey`` names the
      base enemy it derives from (always a real enemy upstream). The spawn resolves
      to that base ``prefabKey`` so the cross-file FK holds; the original inline id
      is carried as ``variant_id`` for traceability (persisted via ``variantId`` in
      the allowlisted spawn ``source_fragment``). The inline ``overwrittenData``
      stats themselves are not modeled here.
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


def _collect_variants(level_raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Stage-scoped inline enemy variants from ``useDb:false`` refs.

    Each ``useDb:false`` ref carrying an ``overwrittenData.prefabKey`` is a
    level-inline enemy variant whose real stats differ from the base prefab.
    Emit one variant per such ref: its inline id (``variantId``), the base enemy id
    (``prefabKey``), and the verified stat overrides that ``overwrittenData``
    actually *defines* (an undefined stat is omitted so the base is inherited at
    read). Only the :data:`OVERWRITTEN_DATA_ALLOWLIST` structural keys are read via
    the shared stat extractor, so prose (``name``/``description``) never enters.
    A ref without a ``prefabKey`` yields no variant — the spawn keeps
    the unresolved inline id and fails closed downstream (no fabricated base).

    The ref *source* (``enemyDbRefs`` falling back to ``enemies``) and the per-id
    last-wins dedup MUST mirror :func:`_enemy_ref_map` exactly. A spawn the
    ref map resolves to a ``variantId`` with no matching variant row here would
    dangle — ``variant_pk`` NULL, def/res/motion overrides silently dropped back to
    the limitation with no error. A duplicate inline id emitted twice would
    collide on ``UNIQUE(stage_pk, variant_id)`` and abort the whole candidate build;
    the ref map already collapses duplicates last-wins, so this must too.
    """
    refs = level_raw.get("enemyDbRefs")
    if not isinstance(refs, list):
        refs = level_raw.get("enemies")
    if not isinstance(refs, list):
        return []
    variants: dict[str, dict[str, Any]] = {}
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("useDb") is not False:
            continue
        rid = ref.get("id") or ref.get("key")
        if not isinstance(rid, str) or not rid:
            continue
        prefab = _inline_prefab_key(ref)
        if prefab is None:
            continue
        overwritten = ref.get("overwrittenData")
        overwritten = overwritten if isinstance(overwritten, dict) else {}
        # Drop prose (name/description) via the field allowlist before extracting
        # stats: only the OVERWRITTEN_DATA_ALLOWLIST structural keys survive, and
        # their nested string leaves are sanitized. Stats then come from
        # the verified maps over the kept subset.
        kept = apply_allowlist(overwritten, OVERWRITTEN_DATA_ALLOWLIST).kept
        variant: dict[str, Any] = {"variantId": rid, "prefabKey": prefab}
        variant.update(_normalize_enemy_data_stats(kept))
        motion = _defined_motion(kept.get("motion"))
        if motion is not None:
            variant["motion"] = motion
        variants[rid] = variant  # last-wins dedup, mirroring _enemy_ref_map
    return list(variants.values())


def _normalize_tiles(map_data: dict[str, Any]) -> tuple[list[dict[str, Any]], int, int]:
    """Grid ``map`` + flat ``tiles`` defs → normalized tiles with derived x/y.

    ``y`` is the ``map`` ROW INDEX, and ``map[0]`` is the board's TOP row on screen,
    so the derived frame is y-DOWN: ``y == 0`` is the top, ``y == height - 1`` the
    bottom. This is the canonical grid frame every consumer reads -- no
    consumer may re-flip it, and the route frame is converted INTO it below."""
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


def _to_grid_frame(position: Any, height: int) -> Any:
    """Convert one raw route ``{row, col}`` into the canonical tile frame.

    Upstream route positions count ``row`` from the BOTTOM of the board while the
    tile ``y`` derived by :func:`_normalize_tiles` counts from the TOP, so storing the
    raw value puts two opposite frames in one database: the rendered overlay then
    draws route markers on the wrong tiles, and a client cross-referencing a position
    against ``tile_grid`` reads a mirrored board. ``row`` is rebased to
    ``height - 1 - row`` so a position indexes the same board as tile ``(x, y)``.

    ``col`` is untouched (columns share an origin). A non-dict, a missing/non-int
    ``row``, or a non-positive ``height`` passes through unchanged -- a malformed
    position is preserved as-is rather than fabricated into a plausible one."""
    if height <= 0 or not isinstance(position, dict):
        return position
    row = position.get("row")
    if not isinstance(row, int) or isinstance(row, bool):
        return position
    return {**position, "row": height - 1 - row}


def _normalize_checkpoint(checkpoint: Any, height: int) -> Any:
    """One raw checkpoint with its nested ``position`` rebased to the tile frame.

    Only ``position`` is a grid coordinate. ``reachOffset`` is a sub-tile world offset,
    not a board position, so it is left untouched -- rebasing it would invent a
    meaning the source does not carry."""
    if not isinstance(checkpoint, dict) or "position" not in checkpoint:
        return _to_grid_frame(checkpoint, height)  # bare {row, col} fallback shape
    return {**checkpoint, "position": _to_grid_frame(checkpoint["position"], height)}


def _normalize_routes(level_raw: dict[str, Any], height: int) -> list[dict[str, Any]]:
    """Real routes (no ``routeIndex``) → normalized routes with a positional index.

    Positions are rebased from the upstream bottom-origin ``row`` into the canonical
    top-origin tile frame (:func:`_to_grid_frame`) so routes and tiles index
    one board."""
    routes: list[dict[str, Any]] = []
    raw_routes = level_raw.get("routes")
    if not isinstance(raw_routes, list):
        return routes
    for i, raw in enumerate(raw_routes):
        if not isinstance(raw, dict):
            continue
        route_index = raw.get("routeIndex")
        checkpoints = raw.get("checkpoints", [])
        routes.append(
            {
                "routeIndex": route_index if isinstance(route_index, int) else i,
                "startPosition": _to_grid_frame(raw.get("startPosition"), height),
                "endPosition": _to_grid_frame(raw.get("endPosition"), height),
                "checkpoints": (
                    [_normalize_checkpoint(c, height) for c in checkpoints]
                    if isinstance(checkpoints, list)
                    else checkpoints
                ),
            }
        )
    return routes


def _normalize_action(
    action: dict[str, Any], ref_map: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """One real wave action → normalized spawn dict, or ``None`` for a non-spawn action.

    Only ``actionType == "SPAWN"`` actions describe an enemy entering the map. A
    level's waves interleave spawns with UI/scripting actions that *also* carry a
    ``key``: ``DISPLAY_ENEMY_INFO``/``PREVIEW_CURSOR`` name a real enemy (a
    codex/preview cue, not a spawn) and ``STORY``'s ``key`` is a story-asset path
    (e.g. ``activities/a001/tutorial_a001_01_a``), not an enemy id. Gating on the
    presence of ``key`` alone both fabricated phantom spawns from the enemy-info
    cues and leaked a ``STORY`` path as an enemy id that then failed the downstream
    cross-reference check. Verified against live upstream @``413a81a3``: ``SPAWN``
    is the sole enemy-spawning ``actionType``.

    The enemy id comes from ``key`` (resolved via ``enemyDbRefs``; a spawn ``key``
    normally equals the enemy id); a spawn's level variant defaults to the ref's
    declared level. For a ``useDb:false`` inline variant the resolved ``enemyId`` is
    the ref's base ``prefabKey`` and the original inline id is emitted as
    ``variantId`` for traceability.
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
    positional indices, route positions are rebased from the upstream bottom-origin
    ``row`` into the tiles' top-origin frame, and wave ``key`` actions
    resolve to enemy ids.
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
        "routes": _normalize_routes(level_raw, height),
        "waves": _normalize_waves(level_raw, ref_map),
        "variants": _collect_variants(level_raw),
    }


__all__ = [
    "normalize_level_id",
    "normalize_level",
    "is_clean_level_path",
]
