"""Shared plumbing for the MCP tool handlers (§V37 single home).

Every ``get_*`` / ``search_*`` tool follows the same read-only shape: acquire the
process-wide connection, run a domain service, and map the outcome to a typed
:class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope` (§V23). The
acquisition + fail-closed error handling is identical across tools, so it lives
here once rather than being copy-pasted into each tool module (§V37):

* a :class:`~arknights_mcp.db.connection.DatabaseUnavailable` fails closed to a
  fixed ``database_unavailable`` envelope;
* any other exception fails closed to ``internal_error`` -- never a leaked
  exception text, stack trace, or local path (§V23).

Only the per-tool *shaping* of a successful domain result differs; that stays in
the owning tool module and is passed in as ``shape``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence

from arknights_mcp.analyzers import EvidenceItem, Observation, RankedObservation
from arknights_mcp.analyzers.base import dedupe_evidence
from arknights_mcp.db.connection import DatabaseUnavailable
from arknights_mcp.mcp.envelopes import ResponseEnvelope, error, internal_error
from arknights_mcp.services.image_refs import IMAGE_REFS_BASE_URL, IMAGE_REFS_LEGEND
from arknights_mcp.services.operators import cost_item_id
from arknights_mcp.services.stages import SectionPage

#: Supplies the process-wide read-only connection to the promoted build. The
#: app/transport layer owns the connection's lifecycle (opened once, reused); a
#: handler only reads through it and never opens or closes it.
ConnectionProvider = Callable[[], sqlite3.Connection]

#: Fixed, safe copy for the DB-unavailable envelope (§V23 -- no query echo, no
#: stack trace, no local path). Shared: one failure mode, one home (§V37).
DB_UNAVAILABLE_MESSAGE = "the active database is unavailable"
DB_UNAVAILABLE_ACTION = (
    "ask the server admin to run `arknights-mcp status` to check the active build"
)

#: §V65 grounding, path (b): the ONE standing limitation attached (once) to every tool
#: response that emits blackboard effect data -- ``get_operator`` skills/talents/modules
#: and ``compare_operator_modules`` trait/talent/stat changes. It rides the envelope a
#: single time regardless of how many skills/modules/sections the response carries (§V66
#: economy -- never a per-section repeat), and its PRESENCE stays mandatory whenever any
#: blackboard is emitted. Path (a) (T127/ADR 0010) imports the in-game effect description
#: template and emits it alongside the blackboard, but a template may be absent for some
#: effects, so this limitation still rides every emit: read the template when present and
#: never infer mechanics from a raw key name (some are counterintuitive), the exact
#: fabrication the server instructions forbid (§V26 "absent -> say so"). Shortened for
#: §V66 economy (T149) while keeping the grounding intact. Shared: one wording, one home
#: (§V37). Client-facing string, so no internal cites/jargon (§V71) -- the cites live in
#: this comment, never the emitted text.
BLACKBOARD_LIMITATION = (
    "Skill, talent, and module effects are raw blackboard key-value data, with the "
    "in-game description template included when the source has one (some effects lack "
    "it). Read the template for meaning, and do not infer mechanics from a key name "
    "alone, as some are counterintuitive."
)

#: §V65 grounding FLOOR path (c) + §V84/§T169: the one-line pointer folded into the
#: description of every tool that emits bare blackboard data (``get_operator`` +
#: ``compare_operator_modules``). The glossary itself is ~1KB, so embedding it in each
#: description made a client pay for it twice every session (B89); it lives once in
#: :data:`arknights_mcp.instructions.BLACKBOARD_KEY_ENTRIES` and each description carries
#: only this pointer (§V84 one home, no >=500-char block duplicated across descriptions;
#: §V37 one pointer home). The pointer names the ``arknights://glossary/blackboard``
#: RESOURCE, not the server instructions: ``instructions`` is an optional ``initialize``
#: field a client may drop, and for such a client the old pointer named a surface that did
#: not exist (B144). A resource is reachable from the tool call itself. Client-facing text,
#: so no internal cites/jargon (§V71 b); short sentences (§V71 f).
BLACKBOARD_GLOSSARY_POINTER = (
    "A glossary of common blackboard keys is available from this server as the resource "
    "arknights://glossary/blackboard."
)


#: §V69/§V26 (§T132): the standing limitation attached to any operator/module response
#: whose emitted upgrade cost carries an item id that has no paired display name in this
#: build. Each ``{id, count, type}`` upgrade-cost entry is paired with its item's
#: display name when that name is present (from the item data source); when it is not,
#: the id is emitted exactly as stored and is never given a fabricated name -- this
#: limitation tells the client so, instead of leaving a bare id that invites a guessed
#: name or a second lookup. Shared: one wording, one home (§V37). Client-facing text, so
#: no internal cites/jargon (§V71) -- the cites live in this comment, never the string.
COST_ITEM_NAME_LIMITATION = (
    "Some upgrade-cost items show only their item id because that item's display name "
    "is not present in this build. The id is shown exactly as stored and is never given "
    "a fabricated name."
)


#: §V72/§V26 (§T135, B61): the ONE standing limitation attached to EVERY response that
#: emits an ``image_refs`` list -- ``get_operator`` / ``get_enemy`` / ``get_banners``. It
#: rides the envelope a SINGLE time no matter how many refs the response carries (a full
#: operator emits 4 identity refs + one per imported skin row, a banner page many; §V66
#: economy -- never a per-ref repeat of a ~300-char disclaimer), and its PRESENCE stays
#: mandatory whenever any ref is emitted. Each link is DERIVED at response-build time
#: from a stored identifier (the entity's game id; a skin ref's imported portrait id,
#: §T182) and is never fetched or validated by the server (§V63 never-fetch), so a link
#: can be dead when the upstream mirror lacks that asset -- the disclosure keeps a
#: derived link from being presented as a verified fact (§V26 "uncertain -> say so",
#: the exact fabrication B61 flagged). The mirror's portrait tree lags newer operators,
#: so the avatar category has the widest coverage and is the most reliable fallback
#: when a portrait/skin URL 404s. Shortened for §V66 economy (T149). Shared: one
#: wording, one home (§V37). Client-facing text, so no internal cites/jargon (§V71) --
#: the cites live in this comment, never the string.
IMAGE_REFS_LIMITATION = (
    "Image links are derived from stored identifiers and are never fetched or validated "
    "by the server, so a link may 404 if the mirror lacks that asset. The avatar "
    "category has the widest mirror coverage and is the most reliable fallback."
)


#: §T183/§V66 (ADR 0014): the shared description sentence for the base-url hoist, folded
#: into every image-ref-emitting tool description (``get_operator`` / ``get_enemy`` /
#: ``get_banners``). Each ref carries a RELATIVE ``path``; the shared base rides the
#: response ONCE as ``image_refs_base_url`` -- the client joins them for the full URL
#: instead of reading a repeated absolute ``url`` per ref. Shared: one wording, one home
#: (§V37/§V84). Client-facing text, so no internal cites/jargon (§V71); short sentences
#: (§V71 f).
IMAGE_REFS_PATH_NOTE = (
    "Each ref carries a path relative to the response's shared image_refs_base_url "
    "field. Join base_url, a slash, and path for the full image URL."
)


def attach_image_ref_disclosures(
    data: dict[str, object], limitations: tuple[str, ...], *, emits_refs: bool
) -> tuple[str, ...]:
    """Attach the two coupled image-ref envelope fields atomically (§T183/§V66, §V72).

    Every ref-emitting tool (``get_operator`` / ``get_enemy`` / ``get_banners``) must,
    exactly when the response actually emits refs, hoist the shared mirror base onto
    ``data`` as ``image_refs_base_url`` (once -- each ref carries only its relative
    path), hoist the ``image_refs_legend`` decoding the emitted ``category`` /
    ``variant`` labels (§V104/B145 -- the legend rides WITH the values it decodes
    instead of bloating a tool description), AND append
    :data:`IMAGE_REFS_LIMITATION`. The three are one predicate (§V63: "0 refs emitted
    -> base key absent", §V67), so they live in one §V37 home: a surface can never ship
    un-joinable relative paths (base forgotten), undecodable labels (legend forgotten),
    or an undisclosed derived link (limitation forgotten). Mutates ``data`` in place and
    returns the extended limitations tuple; a no-op when ``emits_refs`` is False.
    """
    if not emits_refs:
        return limitations
    data["image_refs_base_url"] = IMAGE_REFS_BASE_URL
    data["image_refs_legend"] = {axis: dict(labels) for axis, labels in IMAGE_REFS_LEGEND.items()}
    return (*limitations, IMAGE_REFS_LIMITATION)


#: §V88/§V26 (§T181 floor, scoped by §T182): the standing partial-gallery limitation
#: for the FALLBACK skin path only -- an operator emit with NO imported skin rows
#: (pre-0014 build / combat-only snapshot), where the derived ``_1b``/``_2b`` refs
#: are the whole gallery and cover just the BASE outfit's E0/E2 art. On the §T182
#: named-gallery path (``operator_skins`` imported from ``skin_table.json``) the
#: outfit list is complete and this limitation must NOT ride; what stays partial
#: there is the alt-form axis, disclosed by :data:`SKIN_ALT_FORM_NOTE` below. The
#: deferral must be VISIBLE, never a silently partial gallery a client would present
#: as complete (the exact wrong hedge B99 reported). One shared block per envelope
#: (§V66/§V72 pattern), never a per-ref repeat. Shared: one wording, one home (§V37).
#: Client-facing text, so no internal cites/jargon (§V71) -- the cites live in this
#: comment, never the string; short sentences (§V71 f).
SKIN_GALLERY_PARTIAL_LIMITATION = (
    "Skin image URLs cover only the base outfit's E0 and E2 art. The outfit list is "
    "incomplete: skin names and paid or event outfits are not present in this build. "
    "Alternate playable forms of an operator are separate characters and are also not "
    "present in this build."
)


#: §V88/§V26 (§T182, ADR 0015): the residual alt-form note for the NAMED gallery path.
#: With ``skin_table`` imported the outfit list is complete and the partial-gallery
#: limitation above no longer applies -- it stays only on the fallback (pre-0014 /
#: skin-domain-empty) path. What remains partial is the alt-form axis: an alternate
#: playable form's skins fold under the BASE operator's gallery (its skin rows carry the
#: base charId) and are labeled ``alt_form``, but the alt form itself is not a separately
#: fetchable/searchable operator in this build -- so the base emit must not read as "the
#: base operator wears these" without that gloss (§V88 "never imply base covers them").
#: Attached only when the emitted gallery actually carries an alt-form ref. One shared
#: block per envelope (§V66/§V72 pattern). Client-facing text, so no internal cites/jargon
#: (§V71) -- the cites live in this comment, never the string; short sentences (§V71 f).
SKIN_ALT_FORM_NOTE = (
    "Skins marked alt_form belong to an alternate playable form of this operator, not "
    "the base form. On those refs the e0/e1/e2 variant labels name the alternate "
    "form's art. Alternate forms are not separately searchable in this build."
)


#: §V83/§V66 (T168, B88): the client-facing note describing how a module's trait/talent
#: change bundles are deduped + labelled, attached to every response of both tools that emit
#: modules (``get_operator`` include_modules + ``compare_operator_modules``). Duplicate/subset
#: rows for one change are collapsed to a single row; a bundle identical at every level rides
#: the module once (``trait_changes`` / ``talent_changes``) and is omitted from each level; a
#: talent change tagged ``applies_to: "token"`` affects the operator's summon/token, not the
#: operator. Shared: one wording, one home (§V37). §T207/§V111 (a): the home moved from the
#: two tool DESCRIPTIONS to a standing LIMITATION -- it describes what the emitted payload
#: OMITTED (the §V26 absent-field class), which is read post-call beside the payload, and as
#: description text it was a 366-char block billed to both tools' §V71 (f) budget every
#: session. Client-facing text, so no internal cites/jargon (§V71); short sentences (§V71 f).
MODULE_CHANGE_DEDUP_NOTE = (
    "For a module's trait_changes and talent_changes: repeated identical entries for one "
    "change are collapsed into a single row. A bundle that is the same at every level is "
    "listed once on the module (as trait_changes or talent_changes) and omitted from each "
    'level. A talent change tagged applies_to "token" affects the operator\'s summon or '
    "token rather than the operator."
)


#: §V104 (B142): the enemy classification fields' cross-field RELATION -- not a value
#: domain, so it stays in the description while the domains themselves ride
#: :data:`ENUM_LEGENDS` (§V104 b). ``enemy_class`` sits beside the redundant ``is_boss`` /
#: ``is_elite`` booleans, so say how they relate instead of leaving the client to guess
#: which one wins, and say that the key is omitted rather than defaulted when the source
#: states no class (§V67). Emitted by ``get_enemy`` and by ``analyze_stage(depth=
#: "detailed")``, hence one shared home (§V37). Client-facing text, so no internal
#: cites/jargon (§V71 b); short sentences (§V71 f).
ENEMY_CLASS_NOTE = (
    "enemy_class is omitted when the source states none; is_boss and is_elite are the "
    "same fact as booleans. damage_types lists every damage kind the enemy deals, so an "
    "enemy that deals both physical and arts carries both tokens."
)


#: §V113/§V26 (B160), §V108 routing: the handbook's ``attackType`` is PRESENT and
#: ``null`` on all 1585 real entries -- a RETIRED value domain, not a per-enemy gap --
#: and the fact moved to the typed ``damageType`` list. Both emitters (``get_enemy`` +
#: ``analyze_stage``) owe the same sentence, so it has one home (§V37). It states the
#: TRUE scope of the absence ("for any enemy") rather than letting a per-entity phrasing
#: suggest some other enemy might carry it (§V108), and it names the field that answers
#: the question instead. Client-facing text, so no internal cites/jargon (§V71 b).
RETIRED_ATTACK_TYPE_NOTE = (
    "The game data no longer fills the older attack_type field for any enemy, so it is "
    "never emitted; damage_types states an enemy's damage kind."
)


#: §V113/§V26 (B160 (c)): ``block_behavior`` and ``abilities`` are absent from EVERY
#: enemy on every build because no source carries them at all -- the unblockable flag
#: upstream is an undefined stat on 2031/2036 entries and the only ability statements are
#: prose, which §V26 forbids reading. Naming the scope is the §V113 declaration: without
#: it the generic per-entity absence note implies some other enemy has the data. The
#: three threat rules that decided from these two fields were retired (§T210 c), so this
#: is also why an analysis never reports unblockable / crowd-control / support-aura
#: enemies. Client-facing text, so no internal cites/jargon (§V71 b).
ENEMY_DEAD_FIELD_NOTE = (
    "The game data carries no unblockable flag and no typed ability list for any enemy, "
    "so block_behavior and abilities are absent for every enemy rather than just this "
    "one, and an analysis never reports block-bypass, crowd-control or support-aura "
    "threats."
)


#: §V104/§V71 (e) (B142): the scale-bearing enemy stats. ``attack_interval`` already
#: stated its unit while ``res`` / ``move_speed`` / ``weight`` -- read on the SAME stat
#: block -- stated none, so a client could only guess whether ``res: 80`` was a percentage
#: or a flat value and whether ``weight: 3`` was kilograms. Shared by ``get_enemy`` +
#: ``analyze_stage`` (§V37). Ranges verified against the shipped build (res 0-1000,
#: move_speed 0.0-5.0, weight -2..13). §T207/§V111 (a): the home moved from the two tool
#: DESCRIPTIONS to a standing LIMITATION, the same call §V104's ``confidence`` scale got
#: and for the same reason -- a scale is read POST-call, beside the number it decodes, and
#: as description text it billed the §V71 (f) budget of both emitters every session.
#: Client-facing text, no internal cites (§V71 b).
ENEMY_STAT_SCALE_NOTE = (
    "Stat scales: res is arts damage reduction in percent (100 or more means arts damage "
    "is fully resisted), move_speed is in tiles per second, and weight is the "
    "shift-resistance rank (a rank, not a mass) where a higher rank resists push and pull "
    "more."
)


#: §V104/§V69 (B142): ``level_variant`` is the enemy's difficulty tier within the SAME
#: enemy record (0 is the base tier), and it is the join key a stage spawn references as
#: ``enemy_level_variant`` -- unnamed anywhere before, so a client reading a spawn had no
#: way to know which stat block applied. Shared by ``get_enemy`` / ``get_stage`` /
#: ``analyze_stage`` (§V37). Client-facing text, no internal cites (§V71 b).
LEVEL_VARIANT_NOTE = (
    "An enemy's stats are listed per level_variant, that enemy's difficulty tier (0 is "
    "the base tier). A stage spawn names its tier as enemy_level_variant; join the two on "
    "that value to read the right stat block."
)


#: §V104/§V6 (B142): the ``confidence`` scale, stated once per observation-emitting tool
#: (``analyze_stage`` / ``compare_operator_modules`` / both drop tools). Every observation
#: carries a confidence and NOTHING said what the number meant, so 0.8 read as "80%
#: likely" -- a calibrated probability this server never computes (§V8 gates a
#: recommendation at 0.5 on the same heuristic scale). One wording, one home (§V37).
#: §T207/§V111 (a): the home moved from the tool DESCRIPTION to a standing LIMITATION --
#: §V104 requires the scale be stated ONCE, not that it be stated pre-call, and the scale
#: is read beside the number it decodes. Four descriptions carried it and each paid the
#: §V71 (f) budget for text no caller needs before choosing the tool. Client-facing text,
#: so no internal cites/jargon (§V71 b); short sentences (§V71 f).
CONFIDENCE_SCALE_NOTE = (
    "confidence is a 0 to 1 heuristic tier set by the rule that produced the observation: "
    "higher means more of the fields the rule wanted were present and unambiguous. It is "
    "not a calibrated probability and is never a measured frequency."
)


#: §V84/§V111 (a) (§T207, B156): the two static reading guides that outgrew the tool
#: description surface, each projected to a CLIENT-FETCHABLE MCP resource with a one-line
#: pointer in the description that names it -- the same home §V84/B144 gave the blackboard
#: glossary, and for the same reason: a bounded surface may not hold an unbounded block,
#: and the alternative home has to be reachable from the tool call itself.
#:
#: ``stage-map``: the tile-grid/route/checkpoint/spawn reading rules were ~970 chars of
#: POST-call instruction inside ``get_stage``'s description, which at 2881 chars was the
#: longest on the server and the successor to the truncation B145 reported (B156).
#:
#: ``search-coverage``: the zone/event matching rules were an IDENTICAL 788-char block in
#: BOTH search descriptions -- the ">=500-char block duplicated across >=2 descriptions"
#: §V84 forbids outright. What stays inline in each description is the PRE-call half (which
#: languages index, how matching works, that a zone or event title matches its stages),
#: because those change how a caller forms the query (§V111 c); what moved is the ordering,
#: attribution, and coverage-gap detail, which is read against a result.
#:
#: Entry tuples rather than one prose blob so the resource body is structured JSON (the
#: :data:`~arknights_mcp.instructions.BLACKBOARD_KEY_ENTRIES` pattern) and each rule stays
#: individually addressable. Client-facing text, so no internal cites/jargon (§V71 b).
STAGE_MAP_GUIDE_URI = "arknights://glossary/stage-map"
STAGE_MAP_GUIDE_ENTRIES: tuple[tuple[str, str], ...] = (
    (
        "tile_grid",
        "The tile grid comes as tile_grid: one string per grid row (top row first) plus a "
        "legend mapping each character to its tile fields. absent_symbol marks a cell "
        "with no tile.",
    ),
    (
        "orientation",
        "Grid rows run top to bottom and each string runs left to right, matching the "
        "board on screen: rows[0] is the top edge of the map, rows[-1] the bottom, "
        "character 0 the left edge. Route and checkpoint positions use the same board -- "
        "col is the character index within a row, row is the index into rows -- so a "
        "spawn tile at rows[0] is at the TOP of the map.",
    ),
    (
        "routes",
        "Enemy routes are collapsed to distinct geometry: each entry carries an "
        "occurrence_count and the raw route_indices that share it.",
    ),
    (
        "checkpoints",
        "A checkpoint always carries type and position. Its optional time, "
        "reach_distance, reach_offset and randomize_reach_offset fields are omitted when "
        "they sit at their zero/false default, so an omitted field means at default.",
    ),
    (
        "spawns",
        "A spawn's variant_id (an inline enemy variant) is present only when the spawn is "
        "one; a base-enemy spawn omits the key.",
    ),
)
STAGE_MAP_GUIDE_POINTER = (
    "How to read tile_grid, enemy routes, checkpoints, and spawns is available from this "
    f"server as the resource {STAGE_MAP_GUIDE_URI}."
)

SEARCH_COVERAGE_URI = "arknights://glossary/search-coverage"
SEARCH_COVERAGE_ENTRIES: tuple[tuple[str, str], ...] = (
    (
        "zone_and_event_ranking",
        "A stage matched through its zone name or event title is listed after every "
        "entity that matched on its own name, code, or id, so an event never crowds out "
        "the operator it is named after.",
    ),
    (
        "zone_and_event_attribution",
        "A stage that matched that way carries the zone name in zone_display_name and the "
        "event title in event_name, so it is visible why an unrelated-looking stage came "
        "back.",
    ),
    (
        "modes_with_no_event",
        "Main story chapters and the permanent modes (annihilation, Stationary Security "
        "Service, Integrated Strategies) belong to no event, so they carry no event title "
        "and are found by chapter or zone name, stage name, or stage code.",
    ),
)
SEARCH_COVERAGE_POINTER = (
    "How zone and event matches are ranked and attributed, and which modes carry no event "
    f"title, is available from this server as the resource {SEARCH_COVERAGE_URI}."
)


def has_unnamed_cost_item(cost_lists: Iterable[object]) -> bool:
    """True when any emitted upgrade-cost entry carries an item id but no display name (§V69).

    The service pairs each ``{id, count, type}`` upgrade-cost entry with its item's
    display name when the name is present in this build (§T132); an entry left with an
    ``id`` and no ``display_name`` had no imported name, so the tool records the standing
    :data:`COST_ITEM_NAME_LIMITATION` rather than fabricating one (§V26). An entry is
    "un-named" only when it is a candidate for pairing in the first place: nameability is
    decided by the shared :func:`~arknights_mcp.services.operators.cost_item_id` predicate
    (§V37), so the detector and the pairer never disagree -- an entry whose id is not a
    non-empty string is not pairable and is therefore never flagged as un-named. A cost
    value that is not a list (the source carried none) contributes nothing. The single
    §V37 home for the detection shared by ``get_operator`` + ``compare_operator_modules``.
    """
    for cost in cost_lists:
        if not isinstance(cost, list):
            continue
        for entry in cost:
            if (
                isinstance(entry, dict)
                and cost_item_id(entry) is not None
                and "display_name" not in entry
            ):
                return True
    return False


def run_guarded[Result](
    get_conn: ConnectionProvider,
    run: Callable[[sqlite3.Connection], Result],
    shape: Callable[[Result], ResponseEnvelope],
) -> ResponseEnvelope:
    """Acquire a connection, run ``run``, and shape its result to an envelope.

    The single §V37 home for the fail-closed §V23 guard shared by every tool: a
    :class:`DatabaseUnavailable` maps to a fixed ``database_unavailable`` envelope
    and any other exception to ``internal_error`` -- the detail belongs in the
    redacted server log, never the client-facing envelope. Only ``shape`` (the
    per-tool ``ok``/``not_found`` mapping) varies between tools.
    """
    try:
        conn = get_conn()
        result = run(conn)
    except DatabaseUnavailable:
        return error(
            "database_unavailable",
            DB_UNAVAILABLE_MESSAGE,
            suggested_action=DB_UNAVAILABLE_ACTION,
        )
    except Exception:
        return internal_error()
    return shape(result)


def run_registry_guarded[Result](
    get_conn: ConnectionProvider,
    run: Callable[[sqlite3.Connection | None], Result],
    shape: Callable[[Result], ResponseEnvelope],
) -> ResponseEnvelope:
    """Like :func:`run_guarded`, but the DB only *enriches* an in-memory result.

    For a tool whose payload lives in memory (the source registry) and for which the
    active build is optional enrichment (the active snapshot per source), a missing
    build must not withhold the payload: ``get_data_sources`` reports the sources +
    their license/attribution posture (PRD §10.7/§13.10) even before any build is
    promoted. So a :class:`DatabaseUnavailable` degrades to ``run(None)`` -- the
    registry-only projection -- rather than a ``database_unavailable`` envelope. Any
    *other* exception still fails closed to ``internal_error`` (§V23), and the shaped
    result is size-capped like every envelope. Entity tools keep :func:`run_guarded`:
    for them the DB *is* the payload, so a missing build correctly fails closed.
    """
    try:
        try:
            conn: sqlite3.Connection | None = get_conn()
        except DatabaseUnavailable:
            conn = None
        result = run(conn)
    except Exception:
        return internal_error()
    return shape(result)


def page_to_dict(page: SectionPage) -> dict[str, object]:
    """The §V19 page descriptor -- ``has_more`` signals another bounded page.

    Shared §V37 home: both ``get_stage`` (map/routes/spawns) and ``get_item_drops``
    (stages/efficiency) return bounded sections, so the page-descriptor wire mapping
    lives here once rather than in each tool module.
    """
    return {
        "page": page.page,
        "page_size": page.page_size,
        "total": page.total,
        "has_more": page.has_more,
    }


#: §V67/B58 convention sentence folded into the description of every tool that emits
#: list-typed fields with the confirmed-none/absent distinction (``get_enemy`` /
#: ``get_stage``). ``[]`` = the source confirms none; an omitted key = the source
#: carried no such data; a value is never ``null``, so a client need not decide
#: "none vs unknown". Shared: one wording, one home (§V37). Client-facing text, so no
#: internal cites/jargon (§V71) -- the cites live in this comment, never the emitted
#: string. §T196/B135 made the promise unconditional -- see
#: :mod:`arknights_mcp.mcp.payload_hygiene`, which now enforces it for every tool.
LIST_FIELD_CONVENTION = (
    "Field conventions: no field is ever null. A list field is [] when the source "
    "confirms none, and is omitted entirely when the source carries no such data; an "
    "optional scalar is likewise omitted when the source carried no value. A field the "
    "response would normally include but the source omits is named in limitations, as is "
    "a name or description the source fills with placeholder characters."
)


def hoist_drop_provenance(
    prov_rows: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Hoist the shared drop-provenance block; return ``(shared, per_row_deviations)``.

    §V66.2 payload dedup: when every drop row repeats an identical provenance block
    (``snapshot_id`` / ``fetched_at`` / ``expires_at`` [/ ``imported_at``]), hoist it
    once to a shared block and leave each row carrying only the fields where it
    deviates (a different snapshot), so a deviant row stays *visible* instead of buried
    among identical repeats. The shared block is the most common provenance row (ties
    broken by row CONTENT, never input position), so the hoist is deterministic and
    identical no matter how the caller orders its rows -- ``get_stage_drops`` feeds
    repository order while efficiency mode reorders the fold, and the two must never
    disagree on which block is "shared"; a row whose provenance matches the shared
    block yields an empty deviation dict. An empty input yields an empty shared block
    + no rows. The single §V37 home for the hoist shared by ``get_stage_drops`` +
    ``get_item_drops``.
    """
    if not prov_rows:
        return {}, []
    # Tally identical provenance rows; every value here is a provenance string, so the
    # sorted-items key is hashable AND totally ordered -- the content tie-break below
    # needs that ordering.
    counts: dict[tuple[tuple[str, object], ...], int] = {}
    order: list[tuple[tuple[str, object], ...]] = []
    for row in prov_rows:
        key = tuple(sorted(row.items(), key=lambda kv: kv[0]))
        if key not in counts:
            order.append(key)
            counts[key] = 0
        counts[key] += 1
    shared_key = max(order, key=lambda k: (counts[k], k))
    shared = dict(shared_key)
    deviations = [{k: v for k, v in row.items() if v != shared.get(k)} for row in prov_rows]
    return shared, deviations


def absent_field_limitation(absent: Sequence[str]) -> tuple[str, ...]:
    """§V67/§V26 (B58): one standing limitation naming the expected fields the source
    omitted for this entity, so a client can tell "the source carried no such data"
    apart from a confirmed-empty value -- the executable form of "absent field -> say
    so". §V67 (B98): the named field's key is OMITTED from the payload, so this
    limitation is the SOLE absence signal -- never a null+limitation duplicate.
    Returns an empty tuple when nothing expected is absent (no limitation
    emitted). Shared §V37 home for ``get_enemy`` + ``get_stage``. Client-facing text,
    so no internal cites/jargon (§V71)."""
    if not absent:
        return ()
    return (
        "These expected fields are not present in this entity's source data: "
        + ", ".join(absent)
        + ". An absent field means the source carried no such data, not that the value "
        "is confirmed empty.",
    )


def evidence_to_dict(item: EvidenceItem) -> dict[str, object]:
    """One typed datum that drove an observation (§V6 evidence).

    Shared §V37 home: both ``analyze_stage`` and ``compare_operator_modules``
    surface analyzer observations, so the evidence/observation wire mapping lives
    here once rather than in each tool module. ``count`` (how many byte-identical
    source rows a §V85-deduped row stands for) is additive-optional (§V21) and
    omitted for a unique row rather than emitted as null (§V67).
    """
    out: dict[str, object] = {
        "ref": item.ref,
        "field": item.field,
        "value": item.value,
        "note": item.note,
    }
    if item.count is not None:
        out["count"] = item.count
    return out


def observation_to_dict(obs: Observation) -> dict[str, object]:
    """One evidence-backed observation with every §V6 field intact (§V37 single home).

    A surfaced inference always carries its ``rule_id`` + evidence + confidence +
    limitations + ``analyzer_version`` -- never a bare verdict (§V6). The §V85
    evidence dedup (byte-identical rows collapse to one attributed row + ``count``)
    is applied HERE, at the one emit surface every analyzer's observations flow
    through, so no rule has to remember to call it and a per-level-variant rule
    (e.g. a flyer identical at two variants) can never ship N verbatim repeats.
    """
    return {
        "rule_id": obs.rule_id,
        "category": obs.category,
        "tag": obs.tag,
        "title": obs.title,
        "summary": obs.summary,
        "confidence": obs.confidence,
        "evidence": [evidence_to_dict(e) for e in dedupe_evidence(obs.evidence)],
        "limitations": list(obs.limitations),
        "analyzer_version": obs.analyzer_version,
    }


def ranked_observation_to_dict(
    obs: RankedObservation,
    *,
    ranking: list[dict[str, object]],
) -> dict[str, object]:
    """A compacted ranked farming observation with the §V6 fields stated once (§V66.1/§V6).

    The §V37 single home for the ranked-observation wire mapping shared by
    ``get_stage_drops`` and ``get_item_drops``. ``rule_id`` / ``confidence`` /
    ``analyzer_version`` (the §V6 identity + baseline confidence) appear once at the
    observation level; the per-entity data lives in ``ranking`` rows whose ``id``
    references the sibling facts list (evidence by reference, never a re-copied number,
    §V66.1). Observation-level ``limitations`` are the caveats that apply to the whole
    ranking (e.g. the §V60 comparison caveats).

    ``ranking`` is required: both drop tools fold each entity's raw drop facts INTO
    its ranking row (§T161/B82 for ``get_item_drops``, §T176/B95 for
    ``get_stage_drops`` -- the ranking subsumes the facts rows), so the caller always
    builds the merged rows. There is no slim-row default -- the one this function
    used to carry was dead in production and had drifted from the live emitters on
    the ``expired`` marker semantics, so it was removed rather than left to diverge.
    """
    return {
        "rule_id": obs.rule_id,
        "category": obs.category,
        "tag": obs.tag,
        "title": obs.title,
        "summary": obs.summary,
        "confidence": obs.confidence,
        "ranking": ranking,
        "limitations": list(obs.limitations),
        "analyzer_version": obs.analyzer_version,
    }
