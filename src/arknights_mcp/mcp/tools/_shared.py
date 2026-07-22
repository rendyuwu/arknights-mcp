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
from collections.abc import Callable

from arknights_mcp.analyzers import EvidenceItem, Observation
from arknights_mcp.db.connection import DatabaseUnavailable
from arknights_mcp.mcp.envelopes import ResponseEnvelope, error, internal_error
from arknights_mcp.services.stages import SectionPage

#: Supplies the process-wide read-only connection to the promoted build. The
#: app/transport layer owns the connection's lifecycle (opened once, reused); a
#: handler only reads through it and never opens or closes it.
ConnectionProvider = Callable[[], sqlite3.Connection]

#: Fixed, safe copy for the DB-unavailable envelope (§V23 -- no query echo, no
#: stack trace, no local path). Shared: one failure mode, one home (§V37).
DB_UNAVAILABLE_MESSAGE = "the active database is unavailable"
DB_UNAVAILABLE_ACTION = "run `arknights-mcp status` to check the active build"

#: §V65 grounding, path (b): the standing limitation attached to every tool response
#: that emits blackboard effect data -- ``get_operator`` skills/talents/modules and
#: ``compare_operator_modules`` trait/talent/stat changes. Path (a) (T127/ADR 0010) now
#: imports the in-game effect description template and emits it alongside the blackboard,
#: but a template may be absent for some effects, so this limitation still rides every
#: emit: it tells the client to read the template when present and never to infer
#: mechanics from a raw key name (some are counterintuitive), which is the exact
#: fabrication the server instructions forbid (§V26 "absent -> say so"). Shared: one
#: wording, one home (§V37). Client-facing string, so no internal cites/jargon (§V71) --
#: the cites live in this comment, never the emitted text.
BLACKBOARD_LIMITATION = (
    "Effect text is the in-game description template, included alongside the blackboard "
    "when present in the source (it may be absent for some effects). Skill, talent, and "
    "module effects are otherwise raw blackboard key-value data straight from the game "
    "files. Read the template to interpret the values; do not infer mechanics from a "
    "key name alone: a key's meaning is context-dependent and some are counterintuitive."
)

#: §V65 grounding FLOOR, path (c): a short glossary of common blackboard keys folded
#: into the description of every tool that emits bare blackboard data, so an MCP client
#: has a grounded reference instead of guessing. These are common interpretations only
#: -- the exact meaning of a key is set by the specific effect (hence the §V65 (b)
#: limitation still rides every emit). Shared: one glossary, one home (§V37). Client-
#: facing text, so no internal cites/jargon (§V71).
BLACKBOARD_KEY_GLOSSARY = (
    "Common blackboard keys (interpretation depends on the specific effect): "
    "atk / atk_scale = ATK modifier or multiplier; def / def_scale = DEF modifier; "
    "max_hp = max HP modifier; magic_resistance = RES modifier; "
    "attack@atk_scale = ATK multiplier for that hit; attack@times / times = hit count; "
    "damage = flat damage; damage_scale = damage-taken multiplier; "
    "heal_scale = healing multiplier; sp / sp_cost = skill point cost; "
    "sp_recovery_per_sec = SP gained per second; duration = effect length in seconds; "
    "interval = interval in seconds; prob = trigger chance (0 to 1); stun = stun seconds; "
    "sleep = sleep seconds; freeze = freeze seconds; "
    "attack_speed = attack-speed (ASPD) modifier; "
    "base_attack_time = attack interval in seconds; move_speed = move-speed modifier; "
    "cost = deploy cost modifier; respawn_time = redeploy seconds; "
    "max_target = maximum targets hit; block_cnt = block-count modifier; "
    "range_extend = added range; charge = charge or stack state; "
    "hp_ratio = HP as a fraction; value = generic magnitude."
)


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


def evidence_to_dict(item: EvidenceItem) -> dict[str, object]:
    """One typed datum that drove an observation (§V6 evidence).

    Shared §V37 home: both ``analyze_stage`` and ``compare_operator_modules``
    surface analyzer observations, so the evidence/observation wire mapping lives
    here once rather than in each tool module.
    """
    return {"ref": item.ref, "field": item.field, "value": item.value, "note": item.note}


def observation_to_dict(obs: Observation) -> dict[str, object]:
    """One evidence-backed observation with every §V6 field intact (§V37 single home).

    A surfaced inference always carries its ``rule_id`` + evidence + confidence +
    limitations + ``analyzer_version`` -- never a bare verdict (§V6).
    """
    return {
        "rule_id": obs.rule_id,
        "category": obs.category,
        "tag": obs.tag,
        "title": obs.title,
        "summary": obs.summary,
        "confidence": obs.confidence,
        "evidence": [evidence_to_dict(e) for e in obs.evidence],
        "limitations": list(obs.limitations),
        "analyzer_version": obs.analyzer_version,
    }
