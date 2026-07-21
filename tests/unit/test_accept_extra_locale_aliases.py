"""T101: the M10 acceptance test (§V5, §V57, §V16).

The milestone gate for M10 (extra-locale NAME aliases). Where the per-task unit
tests exercise the importer (T99) and the FTS rebuild + ``locale`` filter (T100)
in isolation, this one drives the whole M10 story end to end through the shared-core
services both transports call (§V14):

  build the en 4-4 fixture candidate (index populated in-pipeline) -> attach a jp
  katakana NAME alias to one enemy + a kr hangul NAME alias to another via the T99
  importer -> rebuild ``entity_fts`` (T100) -> reopen read-only -> resolve each alias
  through ``search_entities`` and then fetch that entity's facts through ``get_enemy``.

It asserts the three M10 invariants as one flow:

* **alias resolves entity -> its OWN region facts** (§V5/§V57): a jp/kr NAME search
  returns the en enemy, and ``get_enemy`` on that hit returns the entity's OWN en
  region facts + provenance + its own-region display name -- never the alias text,
  never a jp/kr "region".
* **alias locale != fact region** (§V57): the stored aliases carry locale ``ja``/``ko``
  while the entities' fact region stays ``en`` -- the locale tag is a NAME axis, not a
  region, and never widens/relabels region availability (a cn-scoped search of the jp
  alias is ``data_stale``, never a cn-labeled hit).
* **no translated prose stored** (§V16/§V18): the source carried a machine-translated
  description on a non-allowlisted key; the NAME-only allowlist drops it, so it reaches
  no table -- while the katakana/hangul NAMES themselves survive as aliases.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.db.migrations import open_writable
from arknights_mcp.importers.extra_locale_aliases import import_locale_aliases
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.importers.search_index import rebuild_search_index
from arknights_mcp.services.enemies import get_enemy
from arknights_mcp.services.search import search_entities
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: Pinned so the game-data provenance (snapshot_id + imported_at) is byte-stable.
PINNED_IMPORTED_AT = "2026-07-21T00:00:00+00:00"

#: The two extra-locale NAMES attached to the fixture enemies. Katakana on the drone
#: (jp), hangul on the slug (kr) -- both tokenize under FTS5 ``unicode61``.
DRONE_JP = "ドローン"  # "Drone" (enemy_1105_drone, en display "Recon Drone")
SLUG_KR = "광석충"  # "Originium Slug" (enemy_1007_slime, en display "Originium Slug")

#: A machine-translated prose sentinel: the kind of description §V16/§V57 forbid an
#: extra-locale source from ever storing (NAME-only). It rides a NON-allowlisted key
#: in the source, so a correct pipeline drops it. ASCII-only so JSON/DB escaping never
#: masks a leak.
LOCALE_PROSE = "LOCALEPROSE machine-translated blurb that must never ship to a client - see V16."


class _FakeFetcher:
    """In-memory extra-locale fetcher (no network, §V1)."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def fetch(self) -> dict[str, Any]:
        return self._payload


def _jp_fetcher() -> _FakeFetcher:
    # The katakana NAME rides the allowlisted `name`; the machine-translated blurb
    # rides the NON-allowlisted `description`/`appellation` so §V16/§V18 must drop it.
    return _FakeFetcher(
        {
            "character_table": {},
            "enemy_handbook": {
                "enemyData": {
                    "enemy_1105_drone": {
                        "name": DRONE_JP,
                        "description": LOCALE_PROSE,
                        "appellation": "Drone",
                    }
                }
            },
        }
    )


def _kr_fetcher() -> _FakeFetcher:
    return _FakeFetcher(
        {
            "character_table": {},
            "enemy_handbook": {
                "enemyData": {"enemy_1007_slime": {"name": SLUG_KR, "description": LOCALE_PROSE}}
            },
        }
    )


def _build(tmp_path: Path) -> Path:
    """Build the en 4-4 candidate, attach jp + kr NAME aliases, rebuild the index.

    The candidate is written writable (as a CLI sync would, before promotion +
    read-only reopen); the two extra-locale imports run over the same file and the
    index is rebuilt from the surviving rows so the locale-tagged aliases become
    searchable (§T100/§V37). The caller reopens read-only for the query path (§V2).
    """
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
        imported_at=PINNED_IMPORTED_AT,
    )
    writer = open_writable(path)
    try:
        jp = import_locale_aliases(writer, _jp_fetcher(), region="jp")
        kr = import_locale_aliases(writer, _kr_fetcher(), region="kr")
        # One enemy alias each; the jp NAME tagged `ja`, the kr NAME tagged `ko` (§V57).
        assert (jp.locale, jp.enemy_aliases_inserted) == ("ja", 1)
        assert (kr.locale, kr.enemy_aliases_inserted) == ("ko", 1)
        rebuild_search_index(writer)
        writer.commit()
    finally:
        writer.close()
    return path


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return open_read_only(_build(tmp_path))


# --- alias resolves entity -> its OWN region facts (§V5/§V57) ------------------


def test_accept_jp_alias_resolves_entity_to_own_region_facts(conn: sqlite3.Connection) -> None:
    # §V57: the jp katakana NAME resolves the en enemy through search; the hit carries
    # the entity's OWN region (en), not the jp locale (§V5).
    hits = search_entities(conn, query=DRONE_JP).hits
    drone = next(h for h in hits if h.game_id == "enemy_1105_drone")
    assert drone.entity_type == "enemy"
    assert drone.server == "en"

    # Fed straight into get_enemy, the SAME (server, game_id) returns the entity's OWN
    # en region facts + provenance -- the display name is the en name, never the jp alias.
    detail = get_enemy(conn, server=drone.server, game_id=drone.game_id)
    assert detail.status == "ok"
    assert detail.enemy is not None
    assert detail.enemy.server == "en"
    assert detail.enemy.display_name == "Recon Drone"
    assert detail.enemy.display_name != DRONE_JP
    # §V5: the fact carries its region-tagged, pinned game-data provenance.
    assert detail.enemy.provenance.snapshot_id.startswith("en:")
    assert detail.enemy.provenance.imported_at == PINNED_IMPORTED_AT


def test_accept_kr_alias_resolves_entity_to_own_region_facts(conn: sqlite3.Connection) -> None:
    # §V57: the kr hangul NAME resolves the en enemy the same way -- own-region facts.
    hits = search_entities(conn, query=SLUG_KR).hits
    slug = next(h for h in hits if h.game_id == "enemy_1007_slime")
    assert slug.server == "en"

    detail = get_enemy(conn, server=slug.server, game_id=slug.game_id)
    assert detail.status == "ok"
    assert detail.enemy is not None
    assert detail.enemy.server == "en"
    assert detail.enemy.display_name == "Originium Slug"
    assert detail.enemy.display_name != SLUG_KR
    assert detail.enemy.provenance.snapshot_id.startswith("en:")


# --- alias locale != fact region (§V57) ---------------------------------------


def test_accept_alias_locale_is_not_the_fact_region(conn: sqlite3.Connection) -> None:
    # §V57: the stored aliases carry locale ja/ko while the entities' FACT region is en.
    # The locale tag is a NAME axis, never a fact region (which stays en/cn).
    rows = conn.execute(
        "SELECT e.server, a.locale, a.alias FROM enemy_aliases a "
        "JOIN enemies e ON e.enemy_pk = a.enemy_pk "
        "WHERE a.alias_type = 'locale_name' ORDER BY a.locale"
    ).fetchall()
    assert rows == [("en", "ja", DRONE_JP), ("en", "ko", SLUG_KR)]
    # The locale tags are the extra-locale ones, never the fact regions.
    assert {locale for _, locale, _ in rows} == {"ja", "ko"}
    assert {server for server, _, _ in rows} == {"en"}


def test_accept_alias_never_mixes_or_widens_region(conn: sqlite3.Connection) -> None:
    # §V5/§V57: the enemy is en-only. A cn-scoped fetch of its game_id is not_found, and
    # a cn-scoped search of the jp alias is data_stale (no cn snapshot) -- the alias
    # never relabels the entity to cn nor makes the absent cn region look present (§V50).
    assert get_enemy(conn, server="cn", game_id="enemy_1105_drone").status == "not_found"
    cn_search = search_entities(conn, query=DRONE_JP, server="cn")
    assert cn_search.status == "data_stale"
    assert cn_search.hits == ()

    # Scoped to en (the region that DOES hold the snapshot), the alias resolves and the
    # hit stays en -- the alias match returns the entity's OWN region (§V57).
    en_search = search_entities(conn, query=DRONE_JP, server="en")
    assert en_search.status == "ok"
    assert en_search.hits and all(h.server == "en" for h in en_search.hits)


# --- no translated prose stored (§V16/§V18) -----------------------------------


def test_accept_no_translated_prose_stored(conn: sqlite3.Connection) -> None:
    # §V16/§V57: the extra-locale source carried a machine-translated description on a
    # non-allowlisted key; the NAME-only allowlist drops it, so it reaches no table --
    # while the katakana/hangul NAMES themselves DID survive as aliases (so this is not
    # vacuously passing on an all-stripped build).
    tables = [
        name
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    db_dump = "\n".join(
        str(row)
        for table in tables
        for row in conn.execute(f"SELECT * FROM {table}")  # noqa: S608
    )
    assert LOCALE_PROSE not in db_dump
    assert DRONE_JP in db_dump and SLUG_KR in db_dump
