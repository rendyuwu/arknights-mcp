"""§T47 packaging: the wheel ships everything a fresh (non-editable) install needs.

Three guarantees the milestone owns:

* the ``arknights-mcp`` console script (``project.scripts``) is declared, so a
  fresh install exposes the admin CLI + ``serve``;
* the schema migrations ship *inside the package* -- the B16 fix (resolve
  ``migrations/*.sql`` via :mod:`importlib.resources`, not a repo-root path) whose
  enforcing test was explicitly deferred here. An editable checkout reads them
  from the source tree; the real regression is a *non-editable* install missing
  them, so :func:`test_wheel_bundles_migrations_and_py_typed` builds the wheel and
  inspects its members;
* ``py.typed`` ships, so downstream type-checkers see the package as typed.

The wheel is built offline (``build --no-isolation`` against the locked dev-env
hatchling), so this runs in the default gate with no network (§V16 fetch-free).
The build itself is the shared ``built_distributions`` session fixture (§V37) --
one ``python -m build`` for both this smoke and the §T49 release audit.
"""

from __future__ import annotations

import zipfile
from importlib import metadata, resources

from tests.support import BuiltDistributions

#: Migration stems that must ship in the package (§T12; §T19 domains). Kept in sync
#: with ``src/arknights_mcp/migrations`` -- a missing/renamed file trips this.
_EXPECTED_MIGRATIONS = frozenset(
    {
        "0001_core_metadata",
        "0002_enemy_domain",
        "0003_stage_domain",
        "0004_operator_domain",
        "0005_analysis_domain",
        "0006_provenance_backfill",
        "0007_search_index",
        "0008_stage_enemy_variants",
    }
)


def test_console_script_entry_point_declared() -> None:
    scripts = metadata.entry_points(group="console_scripts")
    entry = {ep.name: ep.value for ep in scripts}
    assert entry.get("arknights-mcp") == "arknights_mcp.cli:main"


def test_migrations_resolve_as_package_resources() -> None:
    # B16: migrations are found via importlib.resources (ship in the wheel), not a
    # repo-root path. Every expected migration must be a readable package resource.
    migrations = resources.files("arknights_mcp").joinpath("migrations")
    present = {r.name.removesuffix(".sql") for r in migrations.iterdir() if r.name.endswith(".sql")}
    assert present >= _EXPECTED_MIGRATIONS


def test_py_typed_ships_as_package_resource() -> None:
    assert resources.files("arknights_mcp").joinpath("py.typed").is_file()


def test_wheel_bundles_migrations_and_py_typed(built_distributions: BuiltDistributions) -> None:
    # Inspect the offline-built wheel's members -- the faithful non-editable-
    # install check that a source-tree resource lookup cannot make (B16).
    wheel = built_distributions.wheel
    names = set(built_distributions.wheel_sizes)

    for stem in _EXPECTED_MIGRATIONS:
        assert f"arknights_mcp/migrations/{stem}.sql" in names
    assert "arknights_mcp/py.typed" in names
    # The console script is recorded so the fresh install exposes ``arknights-mcp``.
    entry_points = next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
    with zipfile.ZipFile(wheel) as zf:
        entry_text = zf.read(entry_points).decode("utf-8")
    assert "arknights-mcp = arknights_mcp.cli:main" in entry_text
