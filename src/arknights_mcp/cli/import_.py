"""``import`` command: build a candidate from a local snapshot directory (§T22).

Module is named ``import_`` because ``import`` is a Python keyword; the CLI verb
is still ``import`` (wired in :func:`arknights_mcp.cli._build_parser`).
"""

from __future__ import annotations

import argparse

from arknights_mcp.cli._shared import (
    _LOCAL_SOURCE_ID,
    CliContext,
    _build_validate_promote,
    _err,
    _load,
    _out,
)
from arknights_mcp.importers.pipeline import ServerImport
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter


def _cmd_import(args: argparse.Namespace, ctx: CliContext) -> int:
    config, registry = _load(args)
    entry = registry.get(_LOCAL_SOURCE_ID)
    if entry is None or not entry.enabled:
        _err(f"source {_LOCAL_SOURCE_ID!r} is disabled; enable it before importing (§V20)")
        return 1

    server = args.server
    # NOTE (L9/§V5): local import trusts --server; it stamps the region on every
    # row without verifying the snapshot *is* that region (validate only checks
    # server ∈ {en,cn} + cross-region join consistency). An operator pointing a CN
    # snapshot at `--server en` silently mislabels it — an inherent limitation of
    # user-supplied local snapshots (B1's guard is scoped to `sync`).
    # Raises SourceAdapterError (caught -> exit 1) if the path is not a directory.
    adapter = LocalSnapshotAdapter(args.source_path, server, source_id=_LOCAL_SOURCE_ID)
    _out(f"import: {_LOCAL_SOURCE_ID} server={server} from local snapshot")
    job = ServerImport(server=server, adapter=adapter, source_id=_LOCAL_SOURCE_ID)
    return _build_validate_promote(config, registry, [job], servers=[server])
