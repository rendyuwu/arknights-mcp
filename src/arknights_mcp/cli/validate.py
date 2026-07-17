"""``validate`` command: run the validation gate against a database (§T23)."""

from __future__ import annotations

import argparse

from arknights_mcp.cli._shared import CliContext, _expected_schema_version, _out
from arknights_mcp.db.validate import format_report, validate_database


def _cmd_validate(args: argparse.Namespace, ctx: CliContext) -> int:
    report = validate_database(
        args.database,
        expected_schema_version=_expected_schema_version(),
        min_snapshots=0 if args.allow_empty else 1,
    )
    _out(format_report(report))
    return 0 if report.passed else 1
