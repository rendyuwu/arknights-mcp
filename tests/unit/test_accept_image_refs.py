"""T121: the M12 acceptance tests (§V63, §V1, §V5, §V27, §V16).

The milestone gate for M12 (image URL references). Where the per-task tests drove the
pieces in isolation -- T119 proved the derivation is pure + network-free, T120 wired the
additive ``image_refs`` field into ``get_operator``/``get_enemy``/``get_banners`` -- this
task asserts the whole story end to end through the SAME shared tool registry both
transports dispatch from (§V14, :func:`~arknights_mcp.mcp.tools.build_tool_registry`),
over a real promoted read-only build, driven by the REAL combined emission gate the app
layer computes (:func:`~arknights_mcp.services.image_refs.refs_enabled` over the config
posture AND the machine registry, exactly as :func:`~arknights_mcp.app.build_application`
does) -- not a hand-passed ``image_refs_enabled=True``.

Two knobs make the acceptance real rather than a re-run of the wiring unit tests:

* it goes through ``build_tool_registry(... image_refs_enabled=refs_enabled(...))`` so the
  gate is the production computation, and the DISABLED case uses the SHIPPED registry
  (source ``enabled=false``) so the default posture is what actually ships; and
* it reaches past the served envelope into the built database to prove §V16/§V63
  store-nothing: no derived URL and no art byte is persisted -- the URL exists only at
  response-build time.

One test group per cited invariant:

* **§V63** -- an enabled image-ref source makes ``get_operator`` carry the exact DERIVED
  portrait ``_1``/``_2`` + avatar base/``_2`` + skin ``_1b``/``_2b`` URLs and ``get_enemy``
  the enemy base URL, each stamped with the ``arknights_game_resource`` ``source_id``;
  the DEFAULT (shipped, source-disabled) posture emits no ``image_refs`` at all; and the
  ``#``/``+`` percent-encode holds through the real tool path on a skin-suffix id.
* **§V1** -- the whole tool path opens no socket: with socket creation booby-trapped the
  enabled ``get_operator``/``get_enemy`` calls still emit refs, proving the server never
  fetches/HEADs/validates a derived link.
* **§V5** -- a ref rides the entity's OWN region envelope: the en operator carries en-only
  provenance + en-derived refs, the cn operator cn-only, and a cross-region lookup is
  ``not_found`` with no ref leak (en/cn never mixed).
* **§V27** -- ``get_data_sources`` surfaces the image source's attribution + license /
  permission posture, and never its internal ``policy_notes`` / secrets.
* **§V16** -- the built DB stores neither a derived URL nor an art byte: the derived
  ``raw.githubusercontent.com`` host is absent from every entity/provenance table and the
  specific derived URLs appear nowhere -- derivation is query-time only.
"""

from __future__ import annotations

import socket
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.config import AppConfig
from arknights_mcp.db.connection import open_read_only
from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.services.image_refs import SOURCE_ID, refs_enabled
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import SourceRegistry, load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
ENEMY_ROOT = FIXTURES / "stage_4_4"
OPERATOR_EN = FIXTURES / "operator" / "en"
OPERATOR_CN = FIXTURES / "operator" / "cn"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: The pinned raw-content base (§V63/ADR 0008) every derived URL is built from.
BASE = "https://raw.githubusercontent.com/yuanyan3060/ArknightsGameResource/main"

_SRC = "local_snapshot"
_AMIYA = "char_002_amiya"  # en operator fixture
_CHEN = "char_1013_chen"  # cn operator fixture
_SLIME = "enemy_1007_slime"  # en 4-4 enemy fixture
#: Pinned so the built provenance (snapshot_id + imported_at) is byte-stable.
PINNED_IMPORTED_AT = "2026-07-18T00:00:00+00:00"


# --- shared wiring: the REAL app gate over a real build -----------------------


def _registry(*, image_source_enabled: bool) -> SourceRegistry:
    """The shipped machine registry, optionally with the image-ref source enabled.

    Loading the real registry (not a hand-built stub) means ``get_data_sources`` surfaces
    the genuine §V27 attribution/license posture; flipping only ``enabled`` models the
    ``arknights-mcp source enable`` kill switch (§V20) without touching any other field.
    """
    reg = load_source_registry(REGISTRY)
    if image_source_enabled:
        reg.entries[SOURCE_ID] = reg.entries[SOURCE_ID].model_copy(update={"enabled": True})
    return reg


def _gate(reg: SourceRegistry) -> bool:
    """The production combined gate (§T120): private-only config AND source enabled.

    Mirrors :func:`arknights_mcp.app.build_application` exactly -- a local (private) config
    with ``[image_refs].enabled = true`` AND the registry source enabled. So the DISABLED
    case below fails the gate for the real shipped reason (source ``enabled=false``), not a
    config toggle.
    """
    cfg = AppConfig.model_validate({"image_refs": {"enabled": True}})
    return refs_enabled(config_enabled=cfg.image_refs_enabled, registry=reg)


def _tools(conn: sqlite3.Connection, reg: SourceRegistry) -> ToolRegistry:
    """The shared registry both transports dispatch from (§V14), with the real gate."""
    return build_tool_registry(
        lambda: conn, registry=reg, mode="local", image_refs_enabled=_gate(reg)
    )


def _build(tmp_path: Path) -> Path:
    """Build the multi-region candidate the way a CLI sync would (en + cn).

    en carries the 4-4 enemies AND operator Amiya; cn carries operator Chen. So en/cn
    both hold an operator addressable under their OWN region (the §V5 case) and the enemy
    surface is exercised too.
    """
    path = tmp_path / "cand.sqlite"
    build_candidate(
        path,
        [
            ServerImport("en", LocalSnapshotAdapter(ENEMY_ROOT, "en", _SRC), _SRC),
            ServerImport("en", LocalSnapshotAdapter(OPERATOR_EN, "en", _SRC), _SRC),
            ServerImport("cn", LocalSnapshotAdapter(OPERATOR_CN, "cn", _SRC), _SRC),
        ],
        registry=load_source_registry(REGISTRY),
        imported_at=PINNED_IMPORTED_AT,
    )
    return path


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return open_read_only(_build(tmp_path))


def _seed_operator_db(tmp_path: Path, game_id: str) -> Path:
    """A minimal candidate with one en operator whose ``game_id`` is ``game_id``.

    Used to drive a ``#``/``+`` skin-suffix id through the real ``get_operator`` path: the
    file fixtures carry only clean ids, so a synthetic row proves the unconditional
    percent-encode (§V63) survives the full DB -> service -> derivation -> wire path.
    """
    path = tmp_path / "op.sqlite"
    db = build_database(path)
    try:
        db.execute(
            "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
            "source_type, regions_json, adapter_version, license_status, permission_status, "
            "redistribution_status, attribution_text, enabled, last_reviewed_at) VALUES "
            "(?, 'gd', 'o', 'https://x/', 'game_data_repository', '[\"en\"]', '0', "
            "'reviewed', 'reviewed', 'derived', 'a', 1, '2026-07-21')",
            (_SRC,),
        )
        db.execute(
            "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
            "manifest_hash, status, field_policy_version) VALUES "
            "('snap-en', ?, 'en', ?, 'h', 'active', 'test')",
            (_SRC, PINNED_IMPORTED_AT),
        )
        prov = db.execute(
            "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
            "record_hash, transform_version, field_policy_version) VALUES "
            "('snap-en', 'gamedata/excel/character_table.json', ?, 'rh', 'test', 'test')",
            (game_id,),
        ).lastrowid
        db.execute(
            "INSERT INTO operators (server, game_id, display_name, provenance_id) "
            "VALUES ('en', ?, 'Dirty', ?)",
            (game_id, prov),
        )
        db.commit()
    finally:
        db.close()
    return path


# --- §V63: enabled source -> correct DERIVED urls + source_id -----------------


def test_accept_enabled_operator_carries_derived_refs(conn: sqlite3.Connection) -> None:
    # §V63: through the shared registry + real gate, get_operator carries the exact
    # verified shape (portrait _1/_2, avatar base/_2, skin _1b/_2b), each attributed.
    tools = _tools(conn, _registry(image_source_enabled=True))
    op = (
        tools.get("get_operator").handler(server="en", game_id=_AMIYA).to_dict()["data"]["operator"]
    )  # type: ignore[index]
    refs = op["image_refs"]  # type: ignore[index]
    assert all(r["source_id"] == SOURCE_ID for r in refs)
    by_cat: dict[str, list[str]] = {}
    for r in refs:
        by_cat.setdefault(r["category"], []).append(r["url"])
    assert by_cat["portrait"] == [
        f"{BASE}/portrait/{_AMIYA}_1.png",
        f"{BASE}/portrait/{_AMIYA}_2.png",
    ]
    assert by_cat["avatar"] == [f"{BASE}/avatar/{_AMIYA}.png", f"{BASE}/avatar/{_AMIYA}_2.png"]
    assert by_cat["skin"] == [f"{BASE}/skin/{_AMIYA}_1b.png", f"{BASE}/skin/{_AMIYA}_2b.png"]


def test_accept_enabled_enemy_carries_derived_ref(conn: sqlite3.Connection) -> None:
    # §V63: get_enemy carries the single derived enemy-sprite URL + source_id attribution.
    tools = _tools(conn, _registry(image_source_enabled=True))
    enemy = tools.get("get_enemy").handler(server="en", game_id=_SLIME).to_dict()["data"]["enemy"]  # type: ignore[index]
    assert enemy["image_refs"] == [  # type: ignore[index]
        {"category": "enemy", "url": f"{BASE}/enemy/{_SLIME}.png", "source_id": SOURCE_ID}
    ]


def test_accept_disabled_by_default_emits_no_refs(conn: sqlite3.Connection) -> None:
    # §V63/§C: the SHIPPED registry ships the source disabled, so the real combined gate
    # is OFF even with the config flag on -> neither surface carries image_refs.
    shipped = _registry(image_source_enabled=False)
    assert _gate(shipped) is False
    assert _gate(_registry(image_source_enabled=True)) is True

    tools = _tools(conn, shipped)
    op = (
        tools.get("get_operator").handler(server="en", game_id=_AMIYA).to_dict()["data"]["operator"]
    )  # type: ignore[index]
    assert "image_refs" not in op
    enemy = tools.get("get_enemy").handler(server="en", game_id=_SLIME).to_dict()["data"]["enemy"]  # type: ignore[index]
    assert "image_refs" not in enemy


def test_accept_percent_encode_on_hash_plus_skin_id(tmp_path: Path) -> None:
    # §V63: the unconditional #->%23 / +->%2B encode holds through the real get_operator
    # path on a skin-suffix id carrying both characters.
    dirty = "char_x_epoque#4+alt"
    conn = open_read_only(_seed_operator_db(tmp_path, dirty))
    try:
        tools = _tools(conn, _registry(image_source_enabled=True))
        op = (
            tools.get("get_operator")
            .handler(server="en", game_id=dirty)
            .to_dict()["data"]["operator"]
        )  # type: ignore[index]
        urls = [r["url"] for r in op["image_refs"]]  # type: ignore[index]
        assert urls, "expected derived refs for the seeded operator"
        for url in urls:
            assert "#" not in url and "+" not in url
        skin = sorted(r["url"] for r in op["image_refs"] if r["category"] == "skin")  # type: ignore[index]
        assert skin == [
            f"{BASE}/skin/char_x_epoque%234%2Balt_1b.png",
            f"{BASE}/skin/char_x_epoque%234%2Balt_2b.png",
        ]
    finally:
        conn.close()


# --- §V1: the whole tool path opens no socket ---------------------------------


def test_accept_enabled_tools_open_no_socket(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # §V1: the server never fetches -- with socket creation booby-trapped the enabled
    # get_operator/get_enemy calls still emit refs (pure DB read + string derivation).
    tools = _tools(conn, _registry(image_source_enabled=True))

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("image-ref tool path must not open a socket (§V1)")

    monkeypatch.setattr(socket, "socket", _boom)
    op = (
        tools.get("get_operator").handler(server="en", game_id=_AMIYA).to_dict()["data"]["operator"]
    )  # type: ignore[index]
    assert op["image_refs"]  # type: ignore[index]
    enemy = tools.get("get_enemy").handler(server="en", game_id=_SLIME).to_dict()["data"]["enemy"]  # type: ignore[index]
    assert enemy["image_refs"]  # type: ignore[index]


# --- §V5: ref rides the entity's OWN region envelope, en/cn never mixed -------


def test_accept_refs_scoped_to_region_never_mixed(conn: sqlite3.Connection) -> None:
    tools = _tools(conn, _registry(image_source_enabled=True))

    # en operator: en-only provenance + refs derived from the en game_id.
    en = tools.get("get_operator").handler(server="en", game_id=_AMIYA).to_dict()
    assert [p["server"] for p in en["provenance"]] == ["en"]  # type: ignore[index]
    assert en["data"]["operator"]["image_refs"][0]["url"] == f"{BASE}/portrait/{_AMIYA}_1.png"  # type: ignore[index]

    # cn operator: cn-only provenance + its OWN derived refs (never the en set).
    cn = tools.get("get_operator").handler(server="cn", game_id=_CHEN).to_dict()
    assert [p["server"] for p in cn["provenance"]] == ["cn"]  # type: ignore[index]
    assert cn["data"]["operator"]["image_refs"][0]["url"] == f"{BASE}/portrait/{_CHEN}_1.png"  # type: ignore[index]

    # Cross-region lookups are not_found with no data + no ref leak (en/cn never mixed).
    miss_cn = tools.get("get_operator").handler(server="cn", game_id=_AMIYA)
    assert miss_cn.status == "not_found"
    assert "operator" not in miss_cn.to_dict()["data"]  # type: ignore[operator]
    miss_en = tools.get("get_operator").handler(server="en", game_id=_CHEN)
    assert miss_en.status == "not_found"
    assert "operator" not in miss_en.to_dict()["data"]  # type: ignore[operator]


# --- §V27: get_data_sources shows attribution + license/permission ------------


def test_accept_get_data_sources_shows_image_source_posture(conn: sqlite3.Connection) -> None:
    # §V27: the image source's attribution + license/permission posture is reachable via
    # get_data_sources, WITHOUT leaking its internal policy_notes / secrets. Uses the
    # shipped (disabled) registry -- the posture must be visible before the source is
    # ever enabled.
    tools = _tools(conn, _registry(image_source_enabled=False))
    sources = {
        s["source_id"]: s
        for s in tools.get("get_data_sources").handler().to_dict()["data"]["sources"]  # type: ignore[index]
    }
    assert SOURCE_ID in sources
    img = sources[SOURCE_ID]
    assert img["enabled"] is False  # shipped disabled, posture still visible
    assert img["attribution_text"]
    assert "ArknightsGameResource" in img["attribution_text"]
    assert img["license_identifier"] == "AGPL-3.0"
    assert img["license_status"]
    assert img["permission_status"]
    assert "removal_on_request" in img["permission_status"]
    assert img["redistribution_status"] == "reference_link_only_never_bytes"
    # §V27: internal-only fields never reach the client.
    assert "policy_notes" not in img


# --- §V16: the built DB stores no derived URL and no art byte ------------------


def test_accept_db_stores_no_derived_url_or_bytes(conn: sqlite3.Connection) -> None:
    # §V16/§V63 store-nothing: the DERIVED url exists only at response-build time. The
    # raw-content HOST must be absent from every entity/provenance table, and the specific
    # derived urls for the present entities must appear NOWHERE.
    tables = [
        name
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]

    # data_sources holds the project-authored source REGISTRY documentation -- its
    # policy_notes legitimately DESCRIBE the derive-at-query-time design (mentioning the
    # raw host in prose) and its canonical_url is the github.com repo page. Every OTHER
    # table is imported entity/provenance data that must never carry a derived image url
    # or byte. Scan the data tables for the derived HOST, then assert the specific derived
    # urls appear in NO table at all (including data_sources).
    def _dump(names: list[str]) -> str:
        return "\n".join(str(row) for t in names for row in conn.execute(f"SELECT * FROM {t}"))

    data_dump = _dump([t for t in tables if t != "data_sources"])
    assert "raw.githubusercontent.com" not in data_dump
    assert BASE not in data_dump

    full_dump = data_dump + "\n" + _dump(["data_sources"])
    for url in (
        f"{BASE}/portrait/{_AMIYA}_1.png",
        f"{BASE}/avatar/{_AMIYA}.png",
        f"{BASE}/skin/{_AMIYA}_1b.png",
        f"{BASE}/enemy/{_SLIME}.png",
    ):
        assert url not in full_dump
