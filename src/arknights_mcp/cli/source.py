"""``source`` command group: list/enable/disable/purge data sources (§T26).

Source management is CLI-only and never an MCP tool (§V28). ``enable``/``disable``
touch only the registry kill switch + the operational journal (§V20); ``purge
--rebuild`` rebuilds fail-closed, keeping current data active until the rebuilt
candidate validates and promotes (§V4/§V20/§V32).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from arknights_mcp.cli._shared import (
    CliContext,
    _active_database,
    _err,
    _expected_schema_version,
    _load,
    _out,
)
from arknights_mcp.db.policy_events import PolicyEvent, append_event, read_events
from arknights_mcp.db.purge import purge_and_rebuild
from arknights_mcp.db.validate import format_report
from arknights_mcp.sources.registry import set_source_enabled


def _cmd_source_list(args: argparse.Namespace, ctx: CliContext) -> int:
    _, registry = _load(args)
    if args.json:
        # Public-safe projection only (no policy notes / private hosting, §V27).
        _out(json.dumps(registry.public_registry(), indent=2, sort_keys=True))
        return 0
    _out("sources:")
    for source_id in sorted(registry.entries):
        entry = registry.entries[source_id]
        flag = "enabled " if entry.enabled else "disabled"
        regions = ",".join(entry.regions) or "-"
        _out(
            f"  [{flag}] {source_id:<28} {regions:<7} "
            f"{entry.license_status or '-'}/{entry.permission_status or '-'}"
        )
    return 0


def _toggle_source(args: argparse.Namespace, *, enabled: bool) -> int:
    """Flip a source's registry kill switch and journal the policy event (§V20).

    ``enable``/``disable`` only touch the registry (the mutable kill switch) and
    the operational journal -- they never rebuild or mutate the active database,
    so current data stays served until the next explicit build (§V4/§V20).
    """
    config, registry = _load(args)
    source_id = args.source_id
    if registry.get(source_id) is None:
        _err(f"source {source_id!r} not in registry")
        return 1
    event_type = "enable" if enabled else "disable"
    changed = set_source_enabled(config.source_registry.machine_registry, source_id, enabled)
    if not changed:
        _out(f"source {source_id!r} already {event_type}d")
        return 0
    append_event(
        config.database.data_dir,
        source_id=source_id,
        event_type=event_type,
        reason=args.reason,
    )
    if enabled:
        _out(f"enabled {source_id!r}: sync resumes on next `sync`")
    else:
        _out(f"disabled {source_id!r}: new sync stopped; current data stays active (§V20)")
    return 0


def _cmd_source_enable(args: argparse.Namespace, ctx: CliContext) -> int:
    return _toggle_source(args, enabled=True)


def _cmd_source_disable(args: argparse.Namespace, ctx: CliContext) -> int:
    return _toggle_source(args, enabled=False)


def _cmd_source_purge(args: argparse.Namespace, ctx: CliContext) -> int:
    """Rebuild the active DB with a source's rows removed, promote iff valid (§V20).

    Fail-closed: the current build stays active until the rebuilt candidate passes
    validation and is promoted atomically; a failing rebuild leaves it untouched.
    """
    config, registry = _load(args)
    if not args.rebuild:
        _err("purge requires --rebuild in v0.1 (§I.cmd/§V20)")
        return 1
    source_id = args.source_id
    if registry.get(source_id) is None:
        _err(f"source {source_id!r} not in registry")
        return 1
    active = _active_database(config)
    if active is None:
        _err("no active database to purge from — nothing to rebuild")
        return 1

    data_dir = config.database.data_dir
    # Takedown: also flip the registry kill switch so a later `sync` cannot
    # repopulate the purged source (M6). Disabling keeps current data (§V20) and
    # is truthful even if the rebuild below fails, so it is journaled immediately.
    if set_source_enabled(config.source_registry.machine_registry, source_id, False):
        append_event(data_dir, source_id=source_id, event_type="disable", reason=args.reason)

    # Build the purge event in memory so the rebuild materializes it, but journal
    # it durably only after the rebuild validates + promotes (M5): a rebuild that
    # fails validation must not leave a phantom purge in the operational journal.
    purge_event = PolicyEvent(
        source_id=source_id,
        event_type="purge",
        created_at=datetime.now(tz=UTC).isoformat(),
        reason=args.reason,
    )
    _out(
        f"purge: rebuilding without {source_id!r}; "
        f"current database stays active until validated (§V20)"
    )
    result = purge_and_rebuild(
        active,
        source_id,
        data_dir=data_dir,
        servers=("en", "cn"),
        retain_versions=config.sync.retain_versions,
        current_manifest_path=config.database.current_manifest,
        policy_events=[*read_events(data_dir), purge_event],
        expected_schema_version=_expected_schema_version(),
    )
    affected = result.affected
    _out(
        f"  removed {affected['snapshots']} snapshot(s), "
        f"{affected['enemies']} enemies, {affected['stages']} stages"
    )
    if not result.validation_passed:
        _err("rebuilt candidate failed validation; current database left active (§V20)")
        print(format_report(result.report), file=sys.stderr)
        return 1
    # Rebuild validated + promoted: the purge really happened, so record it now.
    append_event(
        data_dir,
        source_id=source_id,
        event_type="purge",
        reason=args.reason,
        created_at=purge_event.created_at,
    )
    promotion = result.promotion
    assert promotion is not None  # validation passed -> promotion attempted
    if promotion.status == "noop":
        _out(f"unchanged: active build stays {promotion.manifest.database_filename} (no-op)")
    else:
        _out(f"promoted: {promotion.manifest.database_filename}")
        if promotion.pruned:
            _out(f"  pruned {len(promotion.pruned)} old build(s)")
    return 0
