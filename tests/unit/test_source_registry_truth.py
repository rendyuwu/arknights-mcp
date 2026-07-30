"""T200: `fields_consumed` is a CLAIM about the code, so it must be TRUE (§V98/B132).

The primary source's registry entry ships verbatim to every MCP client through
``get_data_sources``. It declared ``range_table.json`` for six milestones while no sync
ever fetched that file and no importer ever opened it -- a client-visible lie about
scope, and worse, a lie that IMPLIED a resolution ``range_id`` did not have (B132). It
also declared ``uniequip_data.json``, a file that has never existed anywhere: the module
metadata is the ``equipDict`` key inside ``uniequip_table.json``, which is listed one
line above it.

§V41 already proves the other direction -- that every path an importer reads by default
is staged by a real sync. This proves the direction §V98 adds: that nothing is DECLARED
which is not staged and read. Together they pin the set from both sides, so the
declaration can neither over-promise (B132) nor quietly under-report the files this
server actually consumes (§V27 completeness).

Deliberately an EQUALITY, not a subset check. A subset in one direction is what let
``gacha_table``/``skin_table``/``activity_table`` be read for three milestones while
undeclared: a client reading the registry would have concluded the banner archive and
the skin gallery came from somewhere else. §V98 does allow a genuinely-planned-but-not-
yet-read entry, but it must be MARKED rather than listed flat; there is no marker
vocabulary yet precisely because nothing is in that state, and adding one entry without
a marker fails here.
"""

from __future__ import annotations

from pathlib import Path
from posixpath import basename

from arknights_mcp.sources.arknights_assets import CORE_FILES, SUPPLEMENTARY_FILES
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "config" / "data_sources.toml"
EXAMPLE_REGISTRY_PATH = REPO_ROOT / "config" / "data_sources.example.toml"

PRIMARY = "arknights_assets_gamedata"

#: The one non-file entry the primary source legitimately declares: the per-stage level
#: files, which are DISCOVERED from ``stage_table.levelId`` at sync time rather than
#: named in a static staged set, so they cannot appear in CORE_FILES.
LEVEL_GLOB = "levels/**"


def _declared(path: Path = REGISTRY_PATH) -> list[str]:
    return list(load_source_registry(path).entries[PRIMARY].fields_consumed)


def _staged_basenames() -> set[str]:
    """Every statically staged snapshot file, by basename.

    The registry names files by basename (``enemy_database.json``) while the staged set
    carries full snapshot paths (``gamedata/levels/enemydata/enemy_database.json``), so
    the comparison normalizes to the registry's own granularity.
    """
    return {basename(p) for p in (*CORE_FILES, *SUPPLEMENTARY_FILES)}


def test_declared_files_are_exactly_the_files_sync_stages() -> None:
    """§V98: declared == staged. Neither an unread promise nor an undisclosed read."""
    declared = _declared()
    globs = {entry for entry in declared if entry.endswith("/**")}
    files = set(declared) - globs

    staged = _staged_basenames()
    over_promised = files - staged
    under_reported = staged - files
    assert not over_promised, (
        f"{PRIMARY}.fields_consumed declares files no sync stages: {sorted(over_promised)} "
        "-- either stage+import them or drop the claim (§V98/B132)"
    )
    assert not under_reported, (
        f"{PRIMARY} reads files it does not declare: {sorted(under_reported)} "
        "-- the registry must disclose the full consumed set (§V27)"
    )
    assert globs == {LEVEL_GLOB}, f"unexpected glob entries: {sorted(globs)}"


def test_no_entry_names_a_sub_key_as_a_file() -> None:
    """B132's second breach: ``uniequip_data.json`` is a KEY, not a file.

    ``equipDict`` lives inside ``uniequip_table.json`` (``importers/modules.py``), so
    declaring it as a sibling file told a client this server fetches a resource that has
    never existed. The list is at FILE granularity; a sub-key is either not listed or
    marked as one, never spelled as a filename of its own.
    """
    declared = _declared()
    assert "uniequip_data.json" not in declared
    # Every non-glob entry must be a name a sync could actually request.
    staged = _staged_basenames()
    for entry in declared:
        if entry.endswith("/**"):
            continue
        assert entry in staged, f"{entry!r} is not a file this sync stages"


def test_every_importer_default_path_is_declared() -> None:
    """Ties §V98 to §V41's introspection: a new domain must reach the registry too.

    §V41 asserts an importer's default source path is STAGED. Without this, a new
    importer could be staged and read while the registry stayed silent about it --
    exactly how gacha/skin/activity went three milestones undeclared.
    """
    import inspect

    from arknights_mcp.importers.banners import import_banners
    from arknights_mcp.importers.enemies import import_enemies
    from arknights_mcp.importers.modules import import_modules
    from arknights_mcp.importers.operators import import_operators
    from arknights_mcp.importers.ranges import import_ranges
    from arknights_mcp.importers.skins import import_skins
    from arknights_mcp.importers.stages import import_stages

    declared = set(_declared())
    read: set[str] = set()
    for fn in (
        import_enemies,
        import_stages,
        import_operators,
        import_modules,
        import_banners,
        import_skins,
        import_ranges,
    ):
        for param in inspect.signature(fn).parameters.values():
            default = param.default
            if (
                isinstance(default, str)
                and default.startswith("gamedata/")
                and default.endswith(".json")
            ):
                read.add(basename(default))

    assert read, "no importer source paths discovered (introspection broke)"
    undeclared = read - declared
    assert not undeclared, f"importers read undeclared files: {sorted(undeclared)} (§V98/§V27)"


def test_range_table_is_declared_and_actually_reached() -> None:
    """B132's own row: the file is declared, staged, AND read -- all three (§V98)."""
    from arknights_mcp.importers.ranges import RANGE_TABLE_PATH

    assert "range_table.json" in _declared()
    assert RANGE_TABLE_PATH in SUPPLEMENTARY_FILES
    assert basename(RANGE_TABLE_PATH) == "range_table.json"


def test_example_registry_matches_the_live_one() -> None:
    """The shipped example is what an operator copies; a drift here re-opens B132."""
    assert _declared(EXAMPLE_REGISTRY_PATH) == _declared()
