"""§T120 image-ref WIRING tests (§V63/§V21/§V14/§V5/§V19; §I.tool).

T119 proved the derivation is pure + network-free; this task wires the additive
``image_refs`` list into the ``get_operator`` (portrait+avatar+skin) + ``get_enemy``
(enemy) envelopes and the ``get_banners`` resolved featured-op (portrait), gated on the
combined config + registry source-enabled gate. These tests drive the real tools end to
end against the production read-only path (§V2). One test group per cited invariant:

* **§V63** -- an enabled source makes ``get_operator``/``get_enemy`` carry the exact
  DERIVED urls, a resolved banner featured-op carry its portrait; DISABLED (default) emits
  no ``image_refs`` at all. The combined ``refs_enabled`` gate needs BOTH the config
  posture AND the registry ``enabled`` flag.
* **§V21** -- the field is ADDITIVE: absent by default (backward-compatible), and when on
  it only ADDS a key -- every pre-existing field stays.
* **§V14** -- the shared registry both transports dispatch threads the same gate, so the
  registry-dispatched result is identical to the tool's own spec.
* **§V5** -- a ref rides the entity's OWN region envelope; a wrong-region lookup is
  ``not_found`` with no ref (en/cn never mixed).
* **§V19** -- a ref is a bounded per-entity attach: a small fixed list, no catalog
  list/page/search key.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.banners import ParsedBanner, insert_banners
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.mcp.tools.banners import build_get_banners_spec
from arknights_mcp.mcp.tools.enemy import build_get_enemy_spec
from arknights_mcp.mcp.tools.operator import build_get_operator_spec
from arknights_mcp.services.image_refs import SOURCE_ID, refs_enabled
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import (
    SourceRegistry,
    SourceRegistryEntry,
    load_source_registry,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ENEMY_ROOT = FIXTURES / "stage_4_4"
OPERATOR_ROOT = FIXTURES / "operator" / "en"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

BASE = "https://raw.githubusercontent.com/yuanyan3060/ArknightsGameResource/main"
_AMIYA = "char_002_amiya"
_SLIME = "enemy_1007_slime"
_SOURCE_ID = "local_snapshot"


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """One candidate holding both the 4-4 enemies and operator Amiya (read-only)."""
    path = tmp_path / "cand.sqlite"
    build_candidate(
        path,
        [
            ServerImport("en", LocalSnapshotAdapter(ENEMY_ROOT, "en", _SOURCE_ID), _SOURCE_ID),
            ServerImport("en", LocalSnapshotAdapter(OPERATOR_ROOT, "en", _SOURCE_ID), _SOURCE_ID),
        ],
        registry=load_source_registry(REGISTRY),
    )
    return open_read_only(path)


def _seed_banner_db(tmp_path: Path) -> Path:
    """A candidate with an en LIMITED banner whose featured op resolves to Amiya."""
    path = tmp_path / "banners.sqlite"
    db = build_database(path)
    try:
        db.execute(
            "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
            "source_type, regions_json, adapter_version, license_status, permission_status, "
            "redistribution_status, attribution_text, enabled, last_reviewed_at) VALUES "
            "(?, 'gd', 'o', 'https://x/', 'game_data_repository', '[\"en\"]', '0', "
            "'reviewed', 'reviewed', 'derived', 'a', 1, '2026-07-21')",
            (_SOURCE_ID,),
        )
        db.execute(
            "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
            "manifest_hash, status, field_policy_version) VALUES "
            "('snap-en', ?, 'en', '2026-07-21T00:00:00+00:00', 'h', 'active', 'test')",
            (_SOURCE_ID,),
        )
        prov = db.execute(
            "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
            "record_hash, transform_version, field_policy_version) VALUES "
            "('snap-en', 'gamedata/excel/character_table.json', ?, 'rh', 'test', 'test')",
            (_AMIYA,),
        ).lastrowid
        db.execute(
            "INSERT INTO operators (server, game_id, display_name, provenance_id) "
            "VALUES ('en', ?, 'Amiya', ?)",
            (_AMIYA, prov),
        )
        insert_banners(
            db,
            [
                ParsedBanner(
                    game_id="LIMITED_1",
                    display_name="Limited",
                    open_time="2026-07-20T00:00:00+00:00",
                    end_time="2026-07-27T00:00:00+00:00",
                    rule_type="LIMITED",
                    featured_char_ids=[_AMIYA, "char_999_ghost"],
                    provenance_record={"gachaPoolId": "LIMITED_1"},
                )
            ],
            server="en",
            snapshot_id="snap-en",
            source_path="gamedata/excel/gacha_table.json",
        )
        db.commit()
    finally:
        db.close()
    return path


def _enabled_registry() -> SourceRegistry:
    """A registry with the image-ref source ENABLED (the emit-on case)."""
    return SourceRegistry(
        entries={SOURCE_ID: SourceRegistryEntry(source_id=SOURCE_ID, enabled=True)}
    )


# --- §V63: derived shape + emit only when enabled -----------------------------


def test_operator_carries_derived_refs_when_enabled(conn: sqlite3.Connection) -> None:
    handler = build_get_operator_spec(lambda: conn, image_refs_enabled=True).handler
    op = handler(server="en", game_id=_AMIYA).to_dict()["data"]["operator"]  # type: ignore[index]
    refs = op["image_refs"]  # type: ignore[index]
    assert all(r["source_id"] == SOURCE_ID for r in refs)
    by_cat: dict[str, list[str]] = {}
    for r in refs:
        by_cat.setdefault(r["category"], []).append(r["url"])
    # §V63 verified shape: portrait _1/_2, avatar base/_2, skin _1b/_2b.
    assert by_cat["portrait"] == [
        f"{BASE}/portrait/{_AMIYA}_1.png",
        f"{BASE}/portrait/{_AMIYA}_2.png",
    ]
    assert by_cat["avatar"] == [f"{BASE}/avatar/{_AMIYA}.png", f"{BASE}/avatar/{_AMIYA}_2.png"]
    assert by_cat["skin"] == [f"{BASE}/skin/{_AMIYA}_1b.png", f"{BASE}/skin/{_AMIYA}_2b.png"]


def test_enemy_carries_derived_ref_when_enabled(conn: sqlite3.Connection) -> None:
    handler = build_get_enemy_spec(lambda: conn, image_refs_enabled=True).handler
    enemy = handler(server="en", game_id=_SLIME).to_dict()["data"]["enemy"]  # type: ignore[index]
    assert enemy["image_refs"] == [  # type: ignore[index]
        {"category": "enemy", "url": f"{BASE}/enemy/{_SLIME}.png", "source_id": SOURCE_ID}
    ]


def test_banner_resolved_featured_op_carries_portrait_when_enabled(tmp_path: Path) -> None:
    conn = open_read_only(_seed_banner_db(tmp_path))
    handler = build_get_banners_spec(lambda: conn, image_refs_enabled=True).handler
    ops = handler(server="en").to_dict()["data"]["banners"][0]["featured_ops"]  # type: ignore[index]
    resolved = {o["char_id"]: o for o in ops}
    # §V63/§V62: the resolved featured op (char_id == operator game_id) carries portraits.
    assert resolved[_AMIYA]["image_refs"] == [
        {"category": "portrait", "url": f"{BASE}/portrait/{_AMIYA}_1.png", "source_id": SOURCE_ID},
        {"category": "portrait", "url": f"{BASE}/portrait/{_AMIYA}_2.png", "source_id": SOURCE_ID},
    ]
    # An UNRESOLVED featured op carries no ref (its raw char id may not name an operator).
    assert "image_refs" not in resolved["char_999_ghost"]


def test_disabled_emits_no_refs_anywhere(conn: sqlite3.Connection) -> None:
    # Default (gate off) -> the additive field is absent on every surface.
    op = build_get_operator_spec(lambda: conn).handler(server="en", game_id=_AMIYA)
    assert "image_refs" not in op.to_dict()["data"]["operator"]  # type: ignore[index]
    enemy = build_get_enemy_spec(lambda: conn).handler(server="en", game_id=_SLIME)
    assert "image_refs" not in enemy.to_dict()["data"]["enemy"]  # type: ignore[index]


def test_refs_enabled_gate_needs_both_config_and_registry() -> None:
    enabled = _enabled_registry()
    disabled = SourceRegistry(
        entries={SOURCE_ID: SourceRegistryEntry(source_id=SOURCE_ID, enabled=False)}
    )
    absent = SourceRegistry(entries={})
    # BOTH gates required: config posture AND the registry source enabled (§V63/§C/§V27).
    assert refs_enabled(config_enabled=True, registry=enabled) is True
    assert refs_enabled(config_enabled=False, registry=enabled) is False
    assert refs_enabled(config_enabled=True, registry=disabled) is False
    assert refs_enabled(config_enabled=True, registry=absent) is False
    # §T124: the shipped registry now ships the source ENABLED -> gate on when config on.
    assert refs_enabled(config_enabled=True, registry=load_source_registry(REGISTRY)) is True


# --- §V21: additive, backward-compatible --------------------------------------


def test_field_is_additive_only(conn: sqlite3.Connection) -> None:
    off = build_get_operator_spec(lambda: conn).handler(server="en", game_id=_AMIYA)
    on = build_get_operator_spec(lambda: conn, image_refs_enabled=True).handler(
        server="en", game_id=_AMIYA
    )
    off_op = off.to_dict()["data"]["operator"]  # type: ignore[index]
    on_op = on.to_dict()["data"]["operator"]  # type: ignore[index]
    # Enabling ADDS exactly one key and preserves every pre-existing field verbatim.
    assert set(on_op) - set(off_op) == {"image_refs"}
    for key in off_op:
        assert on_op[key] == off_op[key]


# --- §V14: same shared registry both transports -------------------------------


def test_shared_registry_threads_the_gate(conn: sqlite3.Connection) -> None:
    registry = build_tool_registry(
        lambda: conn, registry=_enabled_registry(), mode="local", image_refs_enabled=True
    )
    via_registry = registry.get("get_operator").handler(server="en", game_id=_AMIYA).to_dict()
    direct = (
        build_get_operator_spec(lambda: conn, image_refs_enabled=True)
        .handler(server="en", game_id=_AMIYA)
        .to_dict()
    )
    # §V14: the assembled registry adds no divergent logic -- identical to the direct spec.
    assert via_registry == direct
    assert "image_refs" in via_registry["data"]["operator"]  # type: ignore[index]


# --- §V5: ref rides the entity's OWN region envelope --------------------------


def test_ref_scoped_to_entity_region(conn: sqlite3.Connection) -> None:
    handler = build_get_operator_spec(lambda: conn, image_refs_enabled=True).handler
    env = handler(server="en", game_id=_AMIYA)
    # The refs ride an en-attributed envelope; en/cn are never mixed.
    assert [p["server"] for p in env.to_dict()["provenance"]] == ["en"]  # type: ignore[index]
    assert "image_refs" in env.to_dict()["data"]["operator"]  # type: ignore[index]
    # A wrong-region lookup is not_found with no data (no ref leaks across regions).
    cn = handler(server="cn", game_id=_AMIYA)
    assert cn.status == "not_found"
    assert "operator" not in cn.to_dict()["data"]  # type: ignore[operator]


# --- §V19: bounded per-entity attach, no catalog ------------------------------


def test_refs_are_bounded_single_entity_attach(conn: sqlite3.Connection) -> None:
    handler = build_get_operator_spec(lambda: conn, image_refs_enabled=True).handler
    data = handler(server="en", game_id=_AMIYA).to_dict()["data"]
    op = data["operator"]  # type: ignore[index]
    # A small fixed list (portrait 2 + avatar 2 + skin 2), attached to the one entity --
    # never a catalog list/page/search key (§V19 no bulk/enum).
    assert len(op["image_refs"]) == 6
    assert "page" not in data and "results" not in data  # type: ignore[operator]
