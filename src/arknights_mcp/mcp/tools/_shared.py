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

#: Supplies the process-wide read-only connection to the promoted build. The
#: app/transport layer owns the connection's lifecycle (opened once, reused); a
#: handler only reads through it and never opens or closes it.
ConnectionProvider = Callable[[], sqlite3.Connection]

#: Fixed, safe copy for the DB-unavailable envelope (§V23 -- no query echo, no
#: stack trace, no local path). Shared: one failure mode, one home (§V37).
DB_UNAVAILABLE_MESSAGE = "the active database is unavailable"
DB_UNAVAILABLE_ACTION = "run `arknights-mcp status` to check the active build"


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
