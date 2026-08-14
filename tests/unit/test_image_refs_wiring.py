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

import json
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.banners import ParsedBanner, insert_banners
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.mcp.tools._shared import (
    IMAGE_REFS_HOISTED_KEYS,
    IMAGE_REFS_LIMITATION,
    SKIN_ALT_FORM_NOTE,
    SKIN_GALLERY_PARTIAL_LIMITATION,
)
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


def _seed_banner_db(tmp_path: Path, *, with_operator: bool = True) -> Path:
    """A candidate with an en LIMITED banner whose featured op resolves to Amiya.

    ``with_operator=False`` omits the operator row, so BOTH featured char ids stay
    unresolved -- the page then emits no ``image_refs`` list at all (the §V72 "no ref ->
    no caveat" case).
    """
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
        if with_operator:
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
    data = handler(server="en", game_id=_AMIYA).to_dict()["data"]
    # §T183/§V66: the shared base is hoisted ONCE at the data level; each ref carries a
    # RELATIVE path the client joins onto it.
    assert data["image_refs_base_url"] == BASE  # type: ignore[index]
    # §T219/§V66 (4): the registry attribution is hoisted the same way, ONCE beside the
    # base, and no ref carries its own copy.
    assert data["image_refs_source_id"] == SOURCE_ID  # type: ignore[index]
    op = data["operator"]  # type: ignore[index]
    refs = op["image_refs"]  # type: ignore[index]
    assert all("source_id" not in r for r in refs)
    by_cat: dict[str, list[str]] = {}
    for r in refs:
        by_cat.setdefault(r["category"], []).append(r["path"])
    # §V63 verified shape: portrait _1/_2, avatar base/_2.
    assert by_cat["portrait"] == [
        f"portrait/{_AMIYA}_1.png",
        f"portrait/{_AMIYA}_2.png",
    ]
    assert by_cat["avatar"] == [f"avatar/{_AMIYA}.png", f"avatar/{_AMIYA}_2.png"]
    # §T182/§V88: the fixture snapshot carries skin_table.json, so the skin category is
    # the NAMED gallery -- one ref per imported operator_skins row (skin/<portraitId>b),
    # ordered by (skin_group_id, skin_id) -- not the derived _1b/_2b fallback pair.
    assert by_cat["skin"] == [
        f"skin/{_AMIYA}_epoque%234b.png",
        f"skin/{_AMIYA}_1b.png",
        "skin/char_1001_amiya2_2b.png",
    ]
    # §V78/B80: each ref carries the E0/E1/E2/skin/base variant label on the wire; a
    # default-art skin row (ILLUST_0/2) maps to e0/e2, a named outfit stays "skin".
    assert [(r["category"], r["variant"]) for r in refs] == [
        ("portrait", "e0"),
        ("portrait", "e2"),
        ("avatar", "base"),
        ("avatar", "e2"),
        ("skin", "skin"),
        ("skin", "e0"),
        ("skin", "e2"),
    ]


def test_named_gallery_ref_fields(conn: sqlite3.Connection) -> None:
    # §T182/§V88/§V67: a named skin ref carries skin_id always; skin_name/skin_group only
    # when imported; alt_form/paid only when TRUE (absent = default art / not applicable).
    handler = build_get_operator_spec(lambda: conn, image_refs_enabled=True).handler
    op = handler(server="en", game_id=_AMIYA).to_dict()["data"]["operator"]  # type: ignore[index]
    skins = {r["skin_id"]: r for r in op["image_refs"] if r["category"] == "skin"}  # type: ignore[index]

    outfit = skins["char_002_amiya@epoque#4"]
    assert outfit["skin_name"] == "Fresh Fastener"
    assert outfit["skin_group"] == "Test Collection/II"
    assert outfit["paid"] is True
    assert "alt_form" not in outfit

    default = skins["char_002_amiya#1"]
    assert "skin_name" not in default  # default art has no outfit name (§V67 omit)
    assert "paid" not in default and "alt_form" not in default

    alt = skins["char_1001_amiya2#2"]
    assert alt["alt_form"] is True  # ADR 0015: labeled, never silently folded
    assert alt["path"] == "skin/char_1001_amiya2_2b.png"


def test_enemy_carries_derived_ref_when_enabled(conn: sqlite3.Connection) -> None:
    handler = build_get_enemy_spec(lambda: conn, image_refs_enabled=True).handler
    data = handler(server="en", game_id=_SLIME).to_dict()["data"]
    assert data["image_refs_base_url"] == BASE  # type: ignore[index]
    # §T219/§V67: this tool emits exactly ONE ref, so the hoist COSTS it 22 bytes -- a
    # response-level key is wider than the single per-ref constant it replaces. Taken
    # anyway, and pinned here: a hoisted key living per-ROW on one tool and per-RESPONSE
    # on another would make one key mean two things (B168 iv).
    assert data["image_refs_source_id"] == SOURCE_ID  # type: ignore[index]
    enemy = data["enemy"]  # type: ignore[index]
    assert enemy["image_refs"] == [  # type: ignore[index]
        {
            "category": "enemy",
            "path": f"enemy/{_SLIME}.png",
            "variant": "base",
        }
    ]


def test_banner_resolved_featured_op_carries_portrait_and_avatar_when_enabled(
    tmp_path: Path,
) -> None:
    conn = open_read_only(_seed_banner_db(tmp_path))
    handler = build_get_banners_spec(lambda: conn, image_refs_enabled=True).handler
    data = handler(server="en").to_dict()["data"]
    # §T183/§V66: the base is hoisted ONCE at the data level for the WHOLE page -- never
    # repeated per featured op or per ref.
    assert data["image_refs_base_url"] == BASE  # type: ignore[index]
    # §T219/§V66 (4): so is the attribution -- once for the WHOLE page. This is the surface
    # the hoist was counted on: at page_size=100 the per-ref copy was 576 duplicates of one
    # 23-char constant, 21.4% of the whole result frame (B168 i).
    assert data["image_refs_source_id"] == SOURCE_ID  # type: ignore[index]
    ops = data["banners"][0]["featured_ops"]  # type: ignore[index]
    resolved = {o["char_id"]: o for o in ops}
    # §V72/§V63/§V62: the resolved featured op (char_id == operator game_id) carries BOTH
    # portrait (E0/E2) AND avatar (base/E2) -- the avatar rides ALONGSIDE the portrait so
    # the mirror's lagging portrait tree never leaves a portrait-only (possibly dead) ref.
    assert resolved[_AMIYA]["image_refs"] == [
        {"category": "portrait", "path": f"portrait/{_AMIYA}_1.png", "variant": "e0"},
        {"category": "portrait", "path": f"portrait/{_AMIYA}_2.png", "variant": "e2"},
        {"category": "avatar", "path": f"avatar/{_AMIYA}.png", "variant": "base"},
        {"category": "avatar", "path": f"avatar/{_AMIYA}_2.png", "variant": "e2"},
    ]
    # An UNRESOLVED featured op carries no ref (its raw char id may not name an operator).
    assert "image_refs" not in resolved["char_999_ghost"]


def test_disabled_emits_no_refs_anywhere(conn: sqlite3.Connection) -> None:
    # Default (gate off) -> the additive field is absent on every surface, and so is the
    # hoisted base (§V67: no refs -> no base key).
    op = build_get_operator_spec(lambda: conn).handler(server="en", game_id=_AMIYA)
    assert "image_refs" not in op.to_dict()["data"]["operator"]  # type: ignore[index]
    assert "image_refs_base_url" not in op.to_dict()["data"]  # type: ignore[operator]
    enemy = build_get_enemy_spec(lambda: conn).handler(server="en", game_id=_SLIME)
    assert "image_refs" not in enemy.to_dict()["data"]["enemy"]  # type: ignore[index]
    assert "image_refs_base_url" not in enemy.to_dict()["data"]  # type: ignore[operator]


def test_base_url_hoisted_once_and_join_rebuilds_verified_urls(
    conn: sqlite3.Connection,
) -> None:
    # §T183/§V66 (ADR 0014): ONE base key per response; every ref path is RELATIVE
    # (scheme-free), and base + "/" + path reconstructs the exact §V63-verified absolute
    # URL -- the hoist changes bytes, never the resolvable link.
    env = build_get_operator_spec(lambda: conn, image_refs_enabled=True).handler(
        server="en", game_id=_AMIYA
    )
    payload = env.to_dict()
    data = payload["data"]
    base = data["image_refs_base_url"]  # type: ignore[index]
    refs = data["operator"]["image_refs"]  # type: ignore[index]
    assert json.dumps(payload).count("raw.githubusercontent.com") == 1  # base appears ONCE
    for r in refs:
        assert "url" not in r  # the absolute-url field is gone (fold, not a dup)
        assert "://" not in r["path"] and not r["path"].startswith("/")
    joined = [f"{base}/{r['path']}" for r in refs]
    assert joined[0] == f"{BASE}/portrait/{_AMIYA}_1.png"
    assert f"{BASE}/skin/{_AMIYA}_epoque%234b.png" in joined


def test_no_base_url_when_banner_page_emits_no_ref(tmp_path: Path) -> None:
    # §V67/§T183: a page that emits no image_refs list emits no base key either --
    # the hoisted base tracks actual refs exactly like the §V72 caveat. §T219: and neither
    # does the attribution -- an unconditional source_id would claim a source for
    # references the response does not carry.
    conn = open_read_only(_seed_banner_db(tmp_path, with_operator=False))
    env = build_get_banners_spec(lambda: conn, image_refs_enabled=True).handler(server="en")
    data = env.to_dict()["data"]
    for key in IMAGE_REFS_HOISTED_KEYS:
        assert key not in data  # type: ignore[operator]


def test_the_ref_source_id_rides_the_response_once(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # §T219/§V66 (4) on all three emitting surfaces at once: the attribution appears
    # EXACTLY once in the serialized envelope, and no ref carries it. Counted over the
    # serialized payload rather than asserted per key, because the defect B168 filed was a
    # COUNT -- 576 copies of one constant -- and a per-key assertion passes just as well
    # while every row still repeats it.
    banner_conn = open_read_only(_seed_banner_db(tmp_path))
    envelopes = (
        build_get_operator_spec(lambda: conn, image_refs_enabled=True).handler(
            server="en", game_id=_AMIYA
        ),
        build_get_enemy_spec(lambda: conn, image_refs_enabled=True).handler(
            server="en", game_id=_SLIME
        ),
        build_get_banners_spec(lambda: banner_conn, image_refs_enabled=True).handler(server="en"),
    )
    for env in envelopes:
        payload = env.to_dict()
        assert json.dumps(payload).count(SOURCE_ID) == 1, payload["status"]
        assert payload["data"]["image_refs_source_id"] == SOURCE_ID  # type: ignore[index]


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


# --- §V72/§V26: the standing derived-unverified limitation rides every emit -----------


def test_image_refs_limitation_rides_every_emitting_surface(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # §V72/§V26 (§T135, B61): every response that emits an image_refs list carries the
    # standing derived-unverified caveat -- get_operator, get_enemy, AND a get_banners
    # page with a resolved featured op. Disclosure keeps a derived link from being
    # presented as a verified fact.
    op_env = build_get_operator_spec(lambda: conn, image_refs_enabled=True).handler(
        server="en", game_id=_AMIYA
    )
    assert "image_refs" in op_env.to_dict()["data"]["operator"]  # type: ignore[index]
    assert IMAGE_REFS_LIMITATION in op_env.limitations

    enemy_env = build_get_enemy_spec(lambda: conn, image_refs_enabled=True).handler(
        server="en", game_id=_SLIME
    )
    assert "image_refs" in enemy_env.to_dict()["data"]["enemy"]  # type: ignore[index]
    assert IMAGE_REFS_LIMITATION in enemy_env.limitations

    banner_conn = open_read_only(_seed_banner_db(tmp_path))
    banner_env = build_get_banners_spec(lambda: banner_conn, image_refs_enabled=True).handler(
        server="en"
    )
    assert IMAGE_REFS_LIMITATION in banner_env.limitations


def test_n_image_refs_yield_exactly_one_disclaimer(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # §V72/§V66 (T149): the standing derived-unverified disclaimer is ONE shared block per
    # envelope, never repeated per ref. A full operator emits 6 refs and a resolved banner
    # op emits 4, yet the caveat rides the envelope exactly ONCE -- a ~300-char disclaimer
    # copied per ref would be a §V66 economy breach. Presence stays mandatory (count == 1).
    op_env = build_get_operator_spec(lambda: conn, image_refs_enabled=True).handler(
        server="en", game_id=_AMIYA
    )
    op_refs = op_env.to_dict()["data"]["operator"]["image_refs"]  # type: ignore[index]
    assert len(op_refs) == 7  # portrait 2 + avatar 2 + named skins 3 -> more than one ref
    assert op_env.limitations.count(IMAGE_REFS_LIMITATION) == 1

    banner_conn = open_read_only(_seed_banner_db(tmp_path))
    banner_env = build_get_banners_spec(lambda: banner_conn, image_refs_enabled=True).handler(
        server="en"
    )
    ops = banner_env.to_dict()["data"]["banners"][0]["featured_ops"]  # type: ignore[index]
    resolved_refs = next(o["image_refs"] for o in ops if o["char_id"] == _AMIYA)
    assert len(resolved_refs) == 4  # portrait 2 + avatar 2 -> more than one ref
    assert banner_env.limitations.count(IMAGE_REFS_LIMITATION) == 1


def test_no_image_refs_limitation_when_gate_off(conn: sqlite3.Connection, tmp_path: Path) -> None:
    # §V72: no ref emitted -> no caveat. With the gate OFF no surface emits image_refs, so
    # the standing limitation never appears (it rides exactly when a link is present).
    op_env = build_get_operator_spec(lambda: conn).handler(server="en", game_id=_AMIYA)
    assert IMAGE_REFS_LIMITATION not in op_env.limitations
    enemy_env = build_get_enemy_spec(lambda: conn).handler(server="en", game_id=_SLIME)
    assert IMAGE_REFS_LIMITATION not in enemy_env.limitations
    banner_conn = open_read_only(_seed_banner_db(tmp_path))
    banner_env = build_get_banners_spec(lambda: banner_conn).handler(server="en")
    assert IMAGE_REFS_LIMITATION not in banner_env.limitations


def test_no_image_refs_limitation_when_banner_page_emits_no_ref(tmp_path: Path) -> None:
    # §V72: even with the gate ON, a page whose featured ops all stay unresolved emits no
    # image_refs list, so the derived-unverified caveat does NOT ride -- the caveat tracks
    # an actual link, never appears on a page that emitted none.
    conn = open_read_only(_seed_banner_db(tmp_path, with_operator=False))
    env = build_get_banners_spec(lambda: conn, image_refs_enabled=True).handler(server="en")
    ops = env.to_dict()["data"]["banners"][0]["featured_ops"]  # type: ignore[index]
    assert all("image_refs" not in o for o in ops)
    assert IMAGE_REFS_LIMITATION not in env.limitations


def test_named_gallery_drops_partial_limitation_and_notes_alt_form(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # §T182/§V88: with the skin domain imported the outfit list is complete, so the T181
    # partial-gallery limitation is DROPPED; the emitted gallery here carries an alt-form
    # ref, so the standing alt-form note rides instead, exactly ONCE per envelope
    # (ADR 0015: labeled + disclosed, never silently folded into the base operator).
    op_env = build_get_operator_spec(lambda: conn, image_refs_enabled=True).handler(
        server="en", game_id=_AMIYA
    )
    assert SKIN_GALLERY_PARTIAL_LIMITATION not in op_env.limitations
    assert op_env.limitations.count(SKIN_ALT_FORM_NOTE) == 1

    # Surfaces that emit NO skin category (enemy sprite; banner portrait+avatar) never
    # carry either skin caveat -- they track an actual skin ref.
    enemy_env = build_get_enemy_spec(lambda: conn, image_refs_enabled=True).handler(
        server="en", game_id=_SLIME
    )
    assert SKIN_GALLERY_PARTIAL_LIMITATION not in enemy_env.limitations
    assert SKIN_ALT_FORM_NOTE not in enemy_env.limitations
    banner_conn = open_read_only(_seed_banner_db(tmp_path))
    banner_env = build_get_banners_spec(lambda: banner_conn, image_refs_enabled=True).handler(
        server="en"
    )
    assert SKIN_GALLERY_PARTIAL_LIMITATION not in banner_env.limitations
    assert SKIN_ALT_FORM_NOTE not in banner_env.limitations


def test_fallback_path_keeps_partial_limitation(tmp_path: Path) -> None:
    # §V88/§V26 (§T181->§T182, B99): a build WITHOUT imported skin rows (combat-only
    # snapshot / pre-0014 active DB) emits the derived base `_1b`/`_2b` fallback pair and
    # the partial-gallery limitation rides exactly ONCE -- the deferral stays visible,
    # never a silently partial gallery a client presents as complete. The alt-form note
    # never rides the fallback (no alt-form ref is emitted there).
    conn = open_read_only(_seed_banner_db(tmp_path))  # operator seeded, no skin rows
    op_env = build_get_operator_spec(lambda: conn, image_refs_enabled=True).handler(
        server="en", game_id=_AMIYA
    )
    refs = op_env.to_dict()["data"]["operator"]["image_refs"]  # type: ignore[index]
    skin_paths = [r["path"] for r in refs if r["category"] == "skin"]
    assert skin_paths == [f"skin/{_AMIYA}_1b.png", f"skin/{_AMIYA}_2b.png"]
    assert op_env.limitations.count(SKIN_GALLERY_PARTIAL_LIMITATION) == 1
    assert SKIN_ALT_FORM_NOTE not in op_env.limitations


def test_no_skin_gallery_limitation_when_gate_off(conn: sqlite3.Connection) -> None:
    # §V88: gate OFF -> no skin ref emitted -> no partial-gallery caveat (it rides
    # exactly when a skin link is present, like the §V72 derived-unverified caveat).
    op_env = build_get_operator_spec(lambda: conn).handler(server="en", game_id=_AMIYA)
    assert SKIN_GALLERY_PARTIAL_LIMITATION not in op_env.limitations


def test_banner_avatar_survives_absent_portrait(tmp_path: Path) -> None:
    # §V72 accept (B61): the mirror's portrait tree lags newer ops, so a resolved featured
    # op must carry a WORKING avatar ref alongside the portrait -- even if the mirror lacks
    # the portrait, the banner still carries a usable avatar reference, and the standing
    # limitation names the avatar as the best-coverage fallback. The server never fetches
    # to check (§V63), so honesty is the disclosure, not a live probe.
    conn = open_read_only(_seed_banner_db(tmp_path))
    env = build_get_banners_spec(lambda: conn, image_refs_enabled=True).handler(server="en")
    ops = env.to_dict()["data"]["banners"][0]["featured_ops"]  # type: ignore[index]
    resolved = {o["char_id"]: o for o in ops}
    categories = {r["category"] for r in resolved[_AMIYA]["image_refs"]}
    assert "avatar" in categories  # a working avatar ref rides alongside the portrait
    assert IMAGE_REFS_LIMITATION in env.limitations
    assert "avatar" in IMAGE_REFS_LIMITATION and "fallback" in IMAGE_REFS_LIMITATION


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
    # A small bounded list (portrait 2 + avatar 2 + one ref per imported skin row),
    # attached to the one entity -- never a catalog list/page/search key (§V19 no
    # bulk/enum; the skin rows are the operator's OWN gallery, not an art index).
    assert len(op["image_refs"]) == 7
    assert "page" not in data and "results" not in data  # type: ignore[operator]
