"""T60 (M7): source-registry review + simulated takedown/purge drill review.

This is the M7 *review* layer, built on top of the T9 registry checks
(``test_source_registry.py``) and the T28 drill (``test_takedown_drill.py``) --
it does not re-implement either (§V37). It reuses the production builders
(``build_candidate``, ``promote_candidate``, ``purge_and_rebuild``) and the
``stage_4_4`` fixture rather than rebuilding the import pipeline from scratch.

It adds three review-level assertions the unit tests do not make:

1. A *completeness audit* over the real ``config/data_sources.toml``: every
   enabled source carries all §V27 static fields (not just the production
   ``missing_mandatory_fields`` subset -- domains/``fields_consumed`` too).
2. A *value-level leak scan* of the public projection (both the CLI
   ``public_registry`` view and the ``get_data_sources`` service): no secret,
   OAuth config, local filesystem path, or takedown correspondence escapes, and
   the emitted key set stays within the ``_PUBLIC_FIELDS`` allowlist (fail-closed).
3. One consolidated end-to-end takedown drill proving §V20/§V32 (a)-(e) hold
   *together*: current DB active until the candidate validates (a), only the
   purged source's rows removed while the other source/region stays live (b),
   the FTS index no longer surfaces the purged entity (c, B19), the registry
   ``enabled`` flag flips (d, B12), and no phantom purge is journaled on a failed
   rebuild (e, B11).
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from arknights_mcp.cli import main
from arknights_mcp.db.connection import read_only_connection
from arknights_mcp.db.policy_events import read_events
from arknights_mcp.db.promotion import promote_candidate, resolve_active_database
from arknights_mcp.db.purge import purge_and_rebuild
from arknights_mcp.db.validate import CheckResult, ValidationReport
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.services.search import search_entities
from arknights_mcp.services.source_status import get_data_sources
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import _PUBLIC_FIELDS, load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

_LOCAL = "local_snapshot"
_PRIMARY = "arknights_assets_gamedata"

# §V27 required static registry fields for an *enabled* source. This is stricter
# than the production ``_MANDATORY_FOR_ENABLED`` gate: it also requires the
# domains list (``fields_consumed``) so the audit is real, not a stub. "snapshot
# commit" is deliberately absent -- it is tracked at runtime in
# ``source_snapshots`` (see the registry docstring), so it is audited in the drill
# (Part B) against a live build, not in this static check.
_V27_REQUIRED_FIELDS = (
    "source_id",
    "owner_name",
    "canonical_url",
    "source_type",
    "purpose",
    "fields_consumed",
    "regions",
    "license_status",
    "permission_status",
    "attribution_text",
    "last_reviewed_at",
)

# Substrings that must never appear in a public-projection key (§V27): secrets,
# OAuth config, takedown correspondence. ``policy_notes`` (takedown correspondence
# home) is covered explicitly as well.
_FORBIDDEN_KEY_SUBSTRINGS = (
    "policy_note",
    "secret",
    "password",
    "token",
    "bearer",
    "issuer",
    "audience",
    "jwks",
    "oauth",
    "oidc",
    "takedown",
    "correspond",
)


# --- helpers ------------------------------------------------------------------


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Config + isolated writable registry copy (per-file convention, matches
    ``test_cli_source.py``/``test_takedown_drill.py``) so the drill never touches
    repo files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    registry = tmp_path / "data_sources.toml"
    shutil.copyfile(REGISTRY, registry)
    config = tmp_path / "config.toml"
    config.write_text(
        "[database]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'current_manifest = "{(data_dir / "current.json").as_posix()}"\n'
        "\n[source_registry]\n"
        f'machine_registry = "{registry.as_posix()}"\n',
        encoding="utf-8",
    )
    return config, data_dir, registry


def _build_two_source_active(tmp_path: Path, data_dir: Path) -> Path:
    """Promote an active build fed by two independent sources/regions (en via
    ``local_snapshot``, cn via the primary) and return the promoted build path."""
    build0 = tmp_path / "build0.sqlite"
    build_candidate(
        build0,
        [
            ServerImport("en", LocalSnapshotAdapter(FIXTURE_ROOT, "en", _LOCAL), _LOCAL),
            ServerImport("cn", LocalSnapshotAdapter(FIXTURE_ROOT, "cn", _PRIMARY), _PRIMARY),
        ],
        registry=load_source_registry(REGISTRY),
    )
    promote_candidate(build0, data_dir=data_dir, validation_passed=True)
    active = resolve_active_database(data_dir, data_dir / "current.json")
    assert active is not None
    return active


def _count(db: Path, sql: str) -> int:
    with read_only_connection(db) as conn:
        return int(conn.execute(sql).fetchone()[0])


def _iter_str_leaves(value: object) -> Iterator[str]:
    """Yield every string leaf inside a projection value (recurse dict/list)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for inner in value.values():
            yield from _iter_str_leaves(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from _iter_str_leaves(inner)


def _looks_like_fs_path(value: str) -> bool:
    """A local filesystem path (absolute POSIX/Windows or home-dir), which §V27
    forbids leaking. ``http(s)://`` and ``local://`` are intended-public URIs, not
    filesystem paths, so they are exempt."""
    if value.startswith(("http://", "https://", "local://")):
        return False
    return (
        value.startswith("/") or value.startswith("~") or ":\\" in value or value.startswith("\\\\")
    )


# --- Part A: registry completeness review (§V27) ------------------------------


def test_every_enabled_source_carries_all_v27_fields() -> None:
    # §V27: the real registry must be complete for every enabled source. This is a
    # full field-by-field audit, not the production subset check alone.
    reg = load_source_registry(REGISTRY)  # validate=True raises if incomplete
    enabled = reg.enabled()
    assert enabled, "expected at least one enabled source to audit"
    for entry in enabled:
        for field in _V27_REQUIRED_FIELDS:
            value = getattr(entry, field)
            if isinstance(value, str):
                assert value.strip(), f"{entry.source_id}: empty §V27 field {field!r}"
            else:  # regions / fields_consumed are lists
                assert value, f"{entry.source_id}: empty §V27 list field {field!r}"
        assert entry.enabled is True
        # The production completeness gate must agree (it is a subset of the above).
        assert entry.missing_mandatory_fields() == []


def test_public_projection_leaks_no_forbidden_content() -> None:
    # §V27: neither public surface may leak secrets, OAuth config, local fs paths,
    # or takedown correspondence. Both the CLI `source list --json` view
    # (public_registry) and the get_data_sources service are scanned at key AND
    # value level -- a check the unit tests (which only assert policy_notes-absent
    # + named-field presence) do not perform.
    reg = load_source_registry(REGISTRY)
    cli_entries = reg.public_registry()
    svc_entries = [s.to_dict() for s in get_data_sources(reg).sources]

    for entries, allowed_keys in (
        (cli_entries, _PUBLIC_FIELDS),
        (svc_entries, _PUBLIC_FIELDS | {"active_snapshots"}),
    ):
        assert entries, "expected a non-empty public projection"
        for entry in entries:
            keys = set(entry)
            # Fail-closed allowlist: emitted keys stay within the public set.
            assert keys <= allowed_keys, f"unexpected public keys: {keys - allowed_keys}"
            assert "policy_notes" not in entry
            for key in keys:
                low = key.lower()
                assert not any(bad in low for bad in _FORBIDDEN_KEY_SUBSTRINGS), (
                    f"forbidden key leaked: {key!r}"
                )
            for leaf in _iter_str_leaves(entry):
                low = leaf.lower()
                assert "client_secret" not in low, f"secret-like value leaked: {leaf!r}"
                assert "bearer " not in low, f"bearer token leaked: {leaf!r}"
                assert not _looks_like_fs_path(leaf), f"local fs path leaked: {leaf!r}"


# --- Part B: consolidated end-to-end takedown/purge drill (§V20/§V32) ---------


def test_takedown_drill_success_proves_v20_v32(tmp_path: Path) -> None:
    # Consolidated review drill: one successful takedown proving (a)-(d) together.
    config, data_dir, registry = _setup(tmp_path)
    active0 = _build_two_source_active(tmp_path, data_dir)
    active0_bytes = active0.read_bytes()
    current_before = (data_dir / "current.json").read_bytes()

    # Two independent sources/regions are live before the takedown (§V27 audit here
    # covers the runtime "snapshot commit" leg: each source has its own snapshots).
    assert _count(active0, "SELECT COUNT(*) FROM stages WHERE server = 'en'") > 0
    assert _count(active0, "SELECT COUNT(*) FROM stages WHERE server = 'cn'") > 0
    with read_only_connection(active0) as conn:
        live_sources = {
            r[0] for r in conn.execute("SELECT DISTINCT source_id FROM source_snapshots")
        }
    assert live_sources == {_LOCAL, _PRIMARY}

    # Full CLI takedown ceremony: disable + purge --rebuild for the en source.
    assert main(["--config", str(config), "source", "purge", _LOCAL, "--rebuild"]) == 0

    rebuilt = resolve_active_database(data_dir, data_dir / "current.json")
    assert rebuilt is not None

    # (a) current DB active until the candidate validates: the old build was never
    # mutated in place (it survives byte-identical) and the pointer only moved once
    # the rebuild validated (§V4 backstop of §V20).
    assert active0.is_file()
    assert active0.read_bytes() == active0_bytes
    assert rebuilt != active0
    assert (data_dir / "current.json").read_bytes() != current_before

    # (b) only the purged source's rows are removed; the other source/region stays
    # live (§V20/§V32).
    assert _count(rebuilt, "SELECT COUNT(*) FROM stages WHERE server = 'en'") == 0
    assert _count(rebuilt, "SELECT COUNT(*) FROM enemies WHERE server = 'en'") == 0
    assert _count(rebuilt, "SELECT COUNT(*) FROM stages WHERE server = 'cn'") > 0
    assert _count(rebuilt, "SELECT COUNT(*) FROM enemies WHERE server = 'cn'") > 0
    with read_only_connection(rebuilt) as conn:
        remaining = {r[0] for r in conn.execute("SELECT DISTINCT source_id FROM source_snapshots")}
    assert remaining == {_PRIMARY}

    # (c) the FTS index no longer surfaces the purged entity (B19): entity_fts is a
    # standalone FTS5 index with no triggers, so purge must rebuild it from
    # surviving rows or the taken-down entity keeps surfacing via search (§V16).
    assert _count(rebuilt, "SELECT COUNT(*) FROM entity_fts WHERE server = 'en'") == 0
    assert _count(rebuilt, "SELECT COUNT(*) FROM entity_fts WHERE server = 'cn'") > 0
    with read_only_connection(rebuilt) as conn:
        assert search_entities(conn, query="drone", server="en").hits == ()
        assert search_entities(conn, query="drone", server="cn").hits

    # (d) the machine-registry enabled flag flips (B12): a later `sync` cannot
    # repopulate the purged source.
    entry = load_source_registry(registry, validate=False).get(_LOCAL)
    assert entry is not None and entry.enabled is False

    # The successful ceremony journals both the disable and the (now real) purge.
    kinds = [(e.event_type, e.source_id) for e in read_events(data_dir)]
    assert ("disable", _LOCAL) in kinds
    assert ("purge", _LOCAL) in kinds


def test_takedown_failed_validation_no_phantom_purge_keeps_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (a) + (e): a rebuild that fails validation leaves the current DB active and
    # journals NO purge (B11 -- a phantom purge would materialize into the next
    # build while the source's rows are still present).
    config, data_dir, _ = _setup(tmp_path)
    active0 = _build_two_source_active(tmp_path, data_dir)
    active0_bytes = active0.read_bytes()
    current_before = (data_dir / "current.json").read_bytes()

    failing = ValidationReport(
        passed=False,
        schema_version="0001",
        checks=(CheckResult("forced", passed=False, detail="test"),),
    )
    monkeypatch.setattr("arknights_mcp.db.purge.validate_database", lambda *a, **k: failing)

    assert main(["--config", str(config), "source", "purge", _LOCAL, "--rebuild"]) == 1

    # (a) current DB stays active and byte-identical when the rebuild fails.
    assert (data_dir / "current.json").read_bytes() == current_before
    assert active0.read_bytes() == active0_bytes
    assert _count(active0, "SELECT COUNT(*) FROM stages WHERE server = 'en'") > 0
    assert _count(active0, "SELECT COUNT(*) FROM stages WHERE server = 'cn'") > 0

    # (e) no phantom purge journaled (the CLI still journals the truthful disable
    # applied before the rebuild, but never the purge that did not happen).
    kinds = [e.event_type for e in read_events(data_dir)]
    assert "purge" not in kinds


def test_purge_contract_current_active_until_candidate_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (a) at the function-contract level: purge_and_rebuild never promotes when the
    # candidate fails validation (promotion is None), so the current build stays
    # active (§V20). The active DB is only copied, never mutated in place (§V4).
    _, data_dir, _ = _setup(tmp_path)
    active0 = _build_two_source_active(tmp_path, data_dir)
    current_before = (data_dir / "current.json").read_bytes()

    failing = ValidationReport(
        passed=False,
        schema_version="0001",
        checks=(CheckResult("forced", passed=False, detail="test"),),
    )
    monkeypatch.setattr("arknights_mcp.db.purge.validate_database", lambda *a, **k: failing)

    result = purge_and_rebuild(active0, _LOCAL, data_dir=data_dir)
    assert result.validation_passed is False
    assert result.promotion is None
    # No promotion => the current pointer and the active build are untouched.
    assert (data_dir / "current.json").read_bytes() == current_before
    assert _count(active0, "SELECT COUNT(*) FROM source_snapshots") == 2
    assert _count(active0, "SELECT COUNT(*) FROM stages WHERE server = 'en'") > 0
