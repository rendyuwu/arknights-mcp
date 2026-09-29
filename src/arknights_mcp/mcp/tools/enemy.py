"""``get_enemy`` MCP tool.

Bridges the bounded :class:`~arknights_mcp.models.enemies.GetEnemyInput` to
the shared :func:`~arknights_mcp.services.enemies.get_enemy` service and
wraps the outcome in the typed
:class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope`. The tool owns no
query logic -- only the model -> service -> envelope mapping -- so both transports
dispatch identical read-only behaviour from the single registry.

Two invariants are load-bearing here:

* ``server`` is required, so every ``ok`` result is region-attributed +
  carries provenance (snapshot_id + imported_at); an ``en`` enemy is never
  surfaced under a ``cn`` query (the service resolves by the unique
  ``(server, game_id)`` key), so en/cn are never silently mixed.
* Every result is a typed-status envelope (``ok``/``not_found``); a
  database failure or any unexpected error fails closed to a fixed, path/trace-free
  envelope via the shared :func:`~arknights_mcp.mcp.tools._shared.run_guarded`
  guard (``database_unavailable``/``internal_error``).
"""

from __future__ import annotations

from arknights_mcp.mcp.envelopes import Provenance, ResponseEnvelope, error, ok
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._enum_legend import (
    TOOL_ENUM_LEGEND_FIELDS,
    attach_enum_legend,
)
from arknights_mcp.mcp.tools._shared import (
    ATTACK_RANGE_DENIED_NOTE,
    ENEMY_CLASS_NOTE,
    ENEMY_DEAD_FIELD_NOTE,
    ENEMY_STAT_SCALE_NOTE,
    IMAGE_REFS_PATH_NOTE,
    LEVEL_VARIANT_NOTE,
    LIST_FIELD_CONVENTION,
    RETIRED_ATTACK_TYPE_NOTE,
    ConnectionProvider,
    absent_field_limitation,
    attach_image_ref_disclosures,
    run_guarded,
)
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.enemies import GetEnemyInput
from arknights_mcp.services.enemies import (
    EnemyDetailResult,
    EnemyFacts,
    EnemyLevelFacts,
    get_enemy,
)
from arknights_mcp.services.image_refs import enemy_image_refs, image_ref_to_dict

_TOOL_NAME = "get_enemy"
_TOOL_TITLE = "Get enemy"
_TOOL_DESCRIPTION = (
    "Fetch one Arknights enemy's facts by region + game_id: class, boss/elite "
    "flags, damage kinds, motion type, and the per-level stat block (hp, atk, def, res, "
    "attack interval in seconds, attack range, move speed, weight, life-point "
    "reduction) with what the enemy targets and which control effects it ignores. "
    + ENEMY_CLASS_NOTE
    + " The response's enum_legend gives the values of enemy_class, motion_type, "
    "damage_types, targeting and immunities. "
    + LEVEL_VARIANT_NOTE
    + " When the image-reference source is "
    "enabled, an additional image_refs list with the derived enemy sprite reference is "
    "included, decoded by the image_refs_legend it carries. "
    + IMAGE_REFS_PATH_NOTE
    + " en/cn are never mixed. "
    + LIST_FIELD_CONVENTION
)

_NOT_FOUND_MESSAGE = "no enemy matched the given region and game_id"
_NOT_FOUND_ACTION = (
    "verify the server and game_id (use search_entities to find it), or ask the server "
    "admin to run `arknights-mcp status` to check the active build"
)


#: The per-level optional fields (scalars + lists), keyed exactly as
#: emitted (wire key == ``EnemyLevelFacts`` attribute). ONE table drives BOTH the
#: per-level key omission (:func:`_level_to_dict`) and the absent-field limitation
#: (:func:`_enemy_absent_field_limitations`), so the omit set and the naming set can
#: never drift apart.
_LEVEL_OPTIONAL_FIELDS: tuple[str, ...] = (
    "attack_range",
    "block_behavior",
    "targeting",
    "immunities",
    "abilities",
)

#: The flag that says an absent ``attack_range`` is an ANSWER, not a gap.
#: Deliberately NOT in the omit table above: that table drives the absent-field limitation
#: too, and this field's absence is never a gap to name -- a level without it either
#: carries a radius or was never asked. It is emitted only when true, for the same
#: reason the table exists (a ``false`` on every one of 4343 level rows would be bytes
#: that say nothing), and its presence is what re-routes ``attack_range``'s own absence
#: away from the "not present in source" sentence.
_RANGE_DENIED_FIELD = "attack_range_declared_none"

#: The two per-level fields NO real source fills. They stay in the
#: omit table above -- the day a source carries one it must reach the wire -- but their
#: absence is reported by :data:`ENEMY_DEAD_FIELD_NOTE`, which names the true (corpus-
#: wide) scope, instead of the per-entity absent-field sentence that would imply some
#: other enemy has them.
_DEAD_BY_DATA_LEVEL_FIELDS: frozenset[str] = frozenset({"block_behavior", "abilities"})


def _level_to_dict(level: EnemyLevelFacts) -> dict[str, object]:
    """One level variant's typed stat block (structural JSON already vetted).

    List discipline: ``targeting`` / ``immunities`` / ``abilities`` are emitted as
    ``[]`` when the source confirms none and OMITTED when the source carried no such
    data (decoded ``None``) -- never ``null``, so a client need not decide "none vs
    unknown". The absent-in-source scalars ``attack_range`` /
    ``block_behavior`` follow the same rule -- key omitted, never null. A field absent
    from EVERY level variant is named in the response limitations (the sole absence
    signal); a mixed case -- present on one variant, decoded ``None`` on another --
    omits the key on the absent variants only, with no limitation (matching the
    pre-existing list convention).
    """
    out: dict[str, object] = {
        "level_variant": level.level_variant,
        "hp": level.hp,
        "atk": level.atk,
        "def": level.def_,
        "res": level.res,
        "attack_interval": level.attack_interval,
        "move_speed": level.move_speed,
        "weight": level.weight,
        "life_point_reduction": level.life_point_reduction,
    }
    for key in _LEVEL_OPTIONAL_FIELDS:
        value = getattr(level, key)
        if value is not None:
            out[key] = value
    # Emitted only on the variants where the source answered "no radius";
    # a false flag is not a fact about this level, it is the absence of one.
    if level.attack_range_declared_none:
        out[_RANGE_DENIED_FIELD] = True
    return out


#: Expected enemy fields a client reasonably looks for; when the source omits
#: one, it is named in a "not present in source" limitation rather than emitted as null.
def _enemy_absent_field_limitations(enemy: EnemyFacts) -> tuple[str, ...]:
    """Name the expected enemy fields absent from the source.

    ``damage_types`` is absent when the enemy carried no handbook entry; a per-level
    field (the :data:`_LEVEL_OPTIONAL_FIELDS` scalars + lists) is absent when NO
    level variant carries a value (every variant decoded ``None``) -- a variant with
    ``[]`` is present-but-empty (confirmed none), not absent, and a MIXED enemy
    (present on one variant only) is not named here (its absent variants just omit
    the key, the standing convention). Every named field's key is omitted
    from the payload, so the returned limitation is the sole absence signal. Returns
    the single standing limitation naming them (empty when nothing expected is
    absent)."""
    absent: list[str] = []
    standing: list[str] = []
    # The retired scalar is not a gap in THIS enemy's data -- upstream
    # stopped filling it for every enemy -- so it gets the routing sentence, not a place
    # in the per-entity absence list. Only when the live field is missing too is there
    # an entity-level gap worth naming.
    if enemy.damage_types is None:
        absent.append("damage_types")
    elif enemy.attack_type is None:
        standing.append(RETIRED_ATTACK_TYPE_NOTE)
    dead_absent = False
    for name in _LEVEL_OPTIONAL_FIELDS:
        if not all(getattr(lv, name) is None for lv in enemy.levels):
            continue
        if name in _DEAD_BY_DATA_LEVEL_FIELDS:
            dead_absent = True
        elif name == "attack_range" and any(lv.attack_range_declared_none for lv in enemy.levels):
            # The source FILLED this cell -- with its "no attack radius" answer,
            # which the sentinel rule keeps out of a distance column. Calling it "not
            # present in this entity's source data" would be false, so it takes the note
            # that says what the source actually did instead of joining the absent list.
            standing.append(ATTACK_RANGE_DENIED_NOTE)
        else:
            absent.append(name)
    if dead_absent:
        standing.append(ENEMY_DEAD_FIELD_NOTE)
    return (*absent_field_limitation(absent), *standing)


def _enemy_to_dict(enemy: EnemyFacts, *, image_refs_enabled: bool) -> dict[str, object]:
    """The typed enemy facts + ordered level variants (no prose).

    When ``image_refs_enabled`` (the combined config + registry gate), an additive
    ``image_refs`` list with the DERIVED enemy sprite ref rides along -- a
    relative path under the ``data``-level ``image_refs_base_url`` the shaper hoists once;
    when the gate is off the field is absent entirely.
    """
    data: dict[str, object] = {
        "server": enemy.server,
        "game_id": enemy.game_id,
        "display_name": enemy.display_name,
        "enemy_class": enemy.enemy_class,
        "is_boss": enemy.is_boss,
        "is_elite": enemy.is_elite,
        "motion_type": enemy.motion_type,
        "levels": [_level_to_dict(level) for level in enemy.levels],
    }
    # An absent-in-source scalar is omitted, never null -- the standing
    # absent-field limitation is the sole signal, not a null+limitation duplicate.
    if enemy.attack_type is not None:
        data["attack_type"] = enemy.attack_type
    # [] = the source confirms this enemy deals no damage kind; key absent =
    # the enemy has no handbook entry at all (it exists only in the stats database).
    if enemy.damage_types is not None:
        data["damage_types"] = list(enemy.damage_types)
    if image_refs_enabled:
        # DERIVED from the enemy's already-stored game_id -- no byte, no url stored,
        # no fetch. Rides this enemy's OWN region envelope (game_id is region-scoped)
        # so en/cn never mix. A bounded single-entity attach, never a catalog.
        data["image_refs"] = [image_ref_to_dict(r) for r in enemy_image_refs(enemy.game_id)]
    return data


def _shape(result: EnemyDetailResult, *, image_refs_enabled: bool) -> ResponseEnvelope:
    """Map the domain result to a typed envelope (region + provenance).

    A standing limitation names any expected field
    (``attack_type`` + the :data:`_LEVEL_OPTIONAL_FIELDS`) the source omitted, so an
    absent field is called out rather than silently dropped.
    """
    if result.status == "not_found" or result.enemy is None:
        return error("not_found", _NOT_FOUND_MESSAGE, suggested_action=_NOT_FOUND_ACTION)

    prov = result.enemy.provenance
    # Name any expected field the source omitted.
    # When the image_refs list is emitted (the combined gate), the standing
    # derived-unverified limitation rides along too -- the sprite URL is derived + never
    # validated by the server, so a dead link is never presented as a fact.
    limitations = _enemy_absent_field_limitations(result.enemy)
    data: dict[str, object] = {
        "enemy": _enemy_to_dict(result.enemy, image_refs_enabled=image_refs_enabled)
    }
    # ADR 0014: the shared attach (one home) hoists the mirror
    # base ONCE onto data and appends the derived-unverified limitation, exactly when
    # the sprite ref is emitted (get_enemy always emits one when the gate is on).
    limitations = attach_image_ref_disclosures(data, limitations, emits_refs=image_refs_enabled)
    # The STATIC domains of the two enums every enemy row carries, hoisted
    # beside the values instead of spelled out in the description.
    limitations = attach_enum_legend(data, TOOL_ENUM_LEGEND_FIELDS[_TOOL_NAME], limitations)
    # The stat block always rides this tool, so its scales ride with it --
    # res/move_speed/weight are read AS numbers, and "res: 80" is undecidable without them.
    limitations = (*limitations, ENEMY_STAT_SCALE_NOTE)
    return ok(
        data,
        provenance=[
            Provenance(
                server=result.enemy.server,
                snapshot_id=prov.snapshot_id,
                imported_at=prov.imported_at,
            )
        ],
        limitations=limitations,
    )


def build_get_enemy_spec(
    get_conn: ConnectionProvider, *, image_refs_enabled: bool = False
) -> ToolSpec:
    """Build the ``get_enemy`` :class:`ToolSpec`.

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build. ``image_refs_enabled`` is the combined emission gate (config
    private-only posture AND the ``arknights_game_resource`` source enabled, computed
    once at wiring time via :func:`~arknights_mcp.services.image_refs.refs_enabled`); it
    defaults ``False`` so the additive ``image_refs`` field is absent unless the source
    is enabled. The returned spec is read-only for the single shared
    registry both transports dispatch from; its ``input_schema`` is the bounded
    model's JSON Schema, so the required ``server`` + the ``game_id`` cap land on
    the wire exactly as validated.
    """

    def handler(**params: object) -> ResponseEnvelope:
        # The bounded model requires a region, caps the game_id
        # length, and rejects an unknown parameter *before* any query runs -- a
        # ValidationError propagates as a protocol-level rejection.
        parsed = GetEnemyInput.model_validate(params)
        return run_guarded(
            get_conn,
            lambda conn: get_enemy(conn, server=parsed.server, game_id=parsed.game_id),
            lambda result: _shape(result, image_refs_enabled=image_refs_enabled),
        )

    return ToolSpec(
        name=_TOOL_NAME,
        title=_TOOL_TITLE,
        description=_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(GetEnemyInput),
    )
