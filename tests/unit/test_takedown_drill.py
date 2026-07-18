"""T28: the takedown drill — disable + purge + rebuild, current DB stays (§V20).

A takedown walks a fixed ceremony: ``source disable`` flips the registry kill
switch and keeps serving the current data, then ``source purge --rebuild`` rebuilds
a candidate with the source's rows removed and promotes it **only after** it
validates. The current database stays active the whole time; the active build file
is never mutated in place (§V4 backstop), so "stays until validate" is provable by
the old build surviving byte-identical while ``current.json`` swaps atomically to a
new, validated build.

The granular pieces (disable-journals, purge-removes-rows, failed-validation-keeps
-current) are unit-tested in ``test_cli_source.py``; this drill asserts the ordered
end-to-end guarantees those units do not: the current pointer never moves off the
old build until a validated rebuild is promoted, and a purge removes *only* the
target source's rows while other sources stay live.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from arknights_mcp.cli import main
from arknights_mcp.db.connection import read_only_connection
from arknights_mcp.db.policy_events import read_events
from arknights_mcp.db.promotion import promote_candidate, resolve_active_database
from arknights_mcp.db.purge import purge_and_rebuild
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.services.search import search_entities
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

_LOCAL = "local_snapshot"
_PRIMARY = "arknights_assets_gamedata"


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Config + isolated writable registry copy so the drill never touches repo files."""
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


def _active_build(data_dir: Path) -> Path:
    db = resolve_active_database(data_dir, data_dir / "current.json")
    assert db is not None
    return db


def _count(db: Path, sql: str) -> int:
    with read_only_connection(db) as conn:
        return int(conn.execute(sql).fetchone()[0])


# --- drill 1: full CLI ceremony, current stays until validate -----------------


def test_takedown_drill_current_stays_until_validate(tmp_path: Path) -> None:
    config, data_dir, registry = _setup(tmp_path)
    import_argv = ["import", "--server", "en", "--source-path", str(FIXTURE_ROOT)]
    assert main(["--config", str(config), *import_argv]) == 0

    # Snapshot the active build the drill starts from (§V20 "current data").
    build0 = _active_build(data_dir)
    build0_bytes = build0.read_bytes()
    current_before = (data_dir / "current.json").read_bytes()
    assert _count(build0, "SELECT COUNT(*) FROM stages") > 0

    # Step 1 — disable: kill switch off, but current data keeps being served (§V20).
    assert main(["--config", str(config), "source", "disable", _LOCAL, "--reason", "takedown"]) == 0
    entry = load_source_registry(registry, validate=False).get(_LOCAL)
    assert entry is not None and entry.enabled is False
    # No rebuild ran: the active build and the current pointer are untouched.
    assert (data_dir / "current.json").read_bytes() == current_before
    assert _active_build(data_dir) == build0
    assert _count(build0, "SELECT COUNT(*) FROM stages") > 0

    # Step 2 — purge --rebuild: promote a rebuilt candidate only after it validates.
    assert main(["--config", str(config), "source", "purge", _LOCAL, "--rebuild"]) == 0

    # The current pointer moved to a *new* validated build; the purged source's
    # rows are gone and the purge event is materialized into the immutable build.
    build1 = _active_build(data_dir)
    assert build1 != build0
    assert _count(build1, "SELECT COUNT(*) FROM stages") == 0
    assert _count(build1, "SELECT COUNT(*) FROM source_snapshots") == 0
    with read_only_connection(build1) as conn:
        events = list(conn.execute("SELECT event_type, source_id FROM source_policy_events"))
    assert ("purge", _LOCAL) in events

    # "Stays until validate" (§V20/§V4): the old build was never mutated in place —
    # it still exists byte-identical, so the current pointer served it unchanged
    # right up to the atomic swap onto the validated rebuild.
    assert build0.is_file()
    assert build0.read_bytes() == build0_bytes

    # The operational journal records the ceremony: the explicit disable, then the
    # purge (the purge does not re-journal the already-applied disable).
    assert [(e.event_type, e.source_id) for e in read_events(data_dir)] == [
        ("disable", _LOCAL),
        ("purge", _LOCAL),
    ]


# --- drill 2: purge removes only the target source, others stay live ----------


def test_takedown_drill_purges_only_target_keeps_others_live(tmp_path: Path) -> None:
    _, data_dir, _ = _setup(tmp_path)
    registry = load_source_registry(REGISTRY)

    # An active build fed by two independent sources (en via local_snapshot, cn via
    # the primary), then promoted so it is the live current build.
    build0 = tmp_path / "build0.sqlite"
    build_candidate(
        build0,
        [
            ServerImport("en", LocalSnapshotAdapter(FIXTURE_ROOT, "en", _LOCAL), _LOCAL),
            ServerImport("cn", LocalSnapshotAdapter(FIXTURE_ROOT, "cn", _PRIMARY), _PRIMARY),
        ],
        registry=registry,
    )
    promote_candidate(build0, data_dir=data_dir, validation_passed=True)
    active = _active_build(data_dir)
    assert _count(active, "SELECT COUNT(*) FROM stages WHERE server = 'en'") > 0
    assert _count(active, "SELECT COUNT(*) FROM stages WHERE server = 'cn'") > 0
    build0_bytes = build0.read_bytes()

    # Take down only the en source: the rebuild removes rows attributable to it and
    # promotes iff valid (§V20).
    result = purge_and_rebuild(active, _LOCAL, data_dir=data_dir)
    assert result.validation_passed

    # Only the target source's rows are gone; the other source stays live (§V20).
    rebuilt = _active_build(data_dir)
    assert _count(rebuilt, "SELECT COUNT(*) FROM stages WHERE server = 'en'") == 0
    assert _count(rebuilt, "SELECT COUNT(*) FROM enemies WHERE server = 'en'") == 0
    assert _count(rebuilt, "SELECT COUNT(*) FROM stages WHERE server = 'cn'") > 0
    assert _count(rebuilt, "SELECT COUNT(*) FROM enemies WHERE server = 'cn'") > 0
    with read_only_connection(rebuilt) as conn:
        remaining = {row[0] for row in conn.execute("SELECT source_id FROM source_snapshots")}
    assert remaining == {_PRIMARY}

    # The FTS index was rebuilt with the purge (§V16/§V20/§V32): entity_fts is a
    # standalone FTS5 index with no triggers, so the purge must clear + rebuild it
    # or the taken-down source's documents linger and keep surfacing in search.
    assert _count(rebuilt, "SELECT COUNT(*) FROM entity_fts WHERE server = 'en'") == 0
    assert _count(rebuilt, "SELECT COUNT(*) FROM entity_fts WHERE server = 'cn'") > 0
    with read_only_connection(rebuilt) as conn:
        # The purged en entity no longer surfaces; the live cn source still does.
        assert search_entities(conn, query="drone", server="en").hits == ()
        assert search_entities(conn, query="drone", server="cn").hits

    # The pre-purge build stayed valid until the rebuild validated: it was copied,
    # never mutated in place, so it survives byte-identical (§V4 backstop of §V20).
    assert build0.read_bytes() == build0_bytes
