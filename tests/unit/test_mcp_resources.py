"""§T37 ``arknights://`` MCP resource tests (§V27; §V14/§V37; §V5/§V23; §I.resource).

The resources are a second, read-only projection over the same services the tools
expose. These drive the shared :class:`ResourceRegistry` end to end against the
pinned 4-4 fixture (two enemies, one stage) and assert:

* **§I.resource / PRD §13.11** -- the advertised surface: the fixed
  ``arknights://sources`` resource + the enemy/stage/status templates, with the
  operator resource intentionally absent until ``get_operator`` (§T44);
* **§V14 / §V37** -- an entity read returns the *same* envelope the corresponding
  tool returns (resources dispatch the tool handler, no duplicated logic);
* **§V27** -- ``arknights://sources`` is public-safe (no policy notes / local path);
* **§V5 / §V23** -- region + provenance on factual bodies, a typed status, and
  fail-closed ``unsupported_server`` / ``not_found`` / unknown-uri handling with no
  leak; **§V19** -- point lookups only, no bulk-enumeration resource;
* **§V2** -- reads never write.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from mcp.types import ReadResourceResult, Resource, ResourceTemplate

from arknights_mcp.db.connection import DatabaseUnavailable, open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.instructions import BLACKBOARD_KEY_ENTRIES, BLACKBOARD_KEY_GLOSSARY
from arknights_mcp.mcp.resources import (
    ResourceError,
    ResourceRegistry,
    build_default_resources,
)
from arknights_mcp.mcp.tools.enemy import build_get_enemy_spec
from arknights_mcp.services.source_status import get_data_sources
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import SourceRegistry, load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"


@pytest.fixture
def registry() -> SourceRegistry:
    return load_source_registry(REGISTRY)


@pytest.fixture
def conn(tmp_path: Path, registry: SourceRegistry) -> sqlite3.Connection:
    """Build the 4-4 fixture candidate read-only (two enemies + one stage)."""
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(path, [ServerImport("en", adapter, "local_snapshot")], registry=registry)
    return open_read_only(path)


@pytest.fixture
def resources(conn: sqlite3.Connection, registry: SourceRegistry) -> ResourceRegistry:
    return build_default_resources(lambda: conn, registry=registry, mode="local")


def _body(result: ReadResourceResult) -> dict[str, object]:
    """Decode the single JSON resource body back to the envelope dict."""
    assert len(result.contents) == 1
    content = result.contents[0]
    assert content.mimeType == "application/json"
    text = content.text  # type: ignore[union-attr]
    return json.loads(text)


# --- §I.resource / PRD §13.11 advertised surface ------------------------------


def test_lists_fixed_and_template_resources(resources: ResourceRegistry) -> None:
    fixed = resources.list_resources()
    templates = resources.list_resource_templates()
    assert all(isinstance(r, Resource) for r in fixed)
    assert all(isinstance(r, ResourceTemplate) for r in templates)

    fixed_uris = {str(r.uri) for r in fixed}
    template_uris = {r.uriTemplate for r in templates}
    # §V84/§V111 (a) (§T207): two static reading guides joined the fixed set -- the homes
    # the stage-map and search-coverage blocks moved to when they outgrew the §V71 (f)
    # description budget. A pointer must name a surface a client can really fetch (B144).
    assert fixed_uris == {
        "arknights://sources",
        "arknights://glossary/blackboard",
        "arknights://glossary/stage-map",
        "arknights://glossary/search-coverage",
    }
    assert template_uris == {
        "arknights://enemy/{server}/{game_id}",
        "arknights://stage/{server}/{stage_id}",
        "arknights://status/{server}",
        "arknights://banners/{server}",
    }


def test_operator_resource_absent_until_get_operator(resources: ResourceRegistry) -> None:
    # §T44: the operator service is a stub -> no operator resource is advertised
    # (a resource whose reads always fail is worse than an absent one).
    assert "operator" not in resources
    all_uris = {str(r.uri) for r in resources.list_resources()} | {
        r.uriTemplate for r in resources.list_resource_templates()
    }
    assert not any("operator" in uri for uri in all_uris)


def test_no_bulk_enumeration_resource(resources: ResourceRegistry) -> None:
    # §V19 / PRD §13.11: entity resources are point lookups -- each carries an id
    # placeholder; there is no plural/list resource that dumps the dataset.
    for template in resources.list_resource_templates():
        if template.uriTemplate.startswith(("arknights://enemy", "arknights://stage")):
            assert "{game_id}" in template.uriTemplate or "{stage_id}" in template.uriTemplate
    all_uris = {str(r.uri) for r in resources.list_resources()} | {
        r.uriTemplate for r in resources.list_resource_templates()
    }
    assert "arknights://enemies" not in all_uris
    assert "arknights://stages" not in all_uris


# --- §V14 / §V37: same envelope as the tool -----------------------------------


def test_enemy_read_matches_get_enemy_tool(
    resources: ResourceRegistry, conn: sqlite3.Connection
) -> None:
    # §V14: the resource dispatches the exact get_enemy tool handler -> identical body.
    tool_env = build_get_enemy_spec(lambda: conn).handler(server="en", game_id="enemy_1007_slime")
    body = _body(resources.read("arknights://enemy/en/enemy_1007_slime"))
    assert body == tool_env.to_dict()
    assert body["status"] == "ok"
    enemy = body["data"]["enemy"]  # type: ignore[index]
    assert enemy["game_id"] == "enemy_1007_slime"
    assert enemy["motion_type"] == "WALK"


def test_stage_read_resolves_by_game_id(resources: ResourceRegistry) -> None:
    # §I.resource: {stage_id} is the game stage id (game_id), not the display code.
    body = _body(resources.read("arknights://stage/en/main_04-04"))
    assert body["status"] == "ok"
    stage = body["data"]["stage"]  # type: ignore[index]
    assert stage["game_id"] == "main_04-04"
    assert stage["stage_code"] == "4-4"
    # §V22: the heavy sections stay off the default resource body (facts only). §V14: the
    # resource dispatches the get_stage TOOL, so it carries the same §V104 (b) enum_legend
    # -- a domain must not be decidable on one surface and not the other.
    assert set(body["data"]) == {"stage", "enum_legend"}  # type: ignore[arg-type]


# --- §V5 region + provenance --------------------------------------------------


def test_enemy_body_carries_region_and_provenance(resources: ResourceRegistry) -> None:
    body = _body(resources.read("arknights://enemy/en/enemy_1007_slime"))
    prov = body["provenance"]
    assert isinstance(prov, list) and len(prov) == 1
    assert prov[0]["server"] == "en"
    assert prov[0]["snapshot_id"]
    assert prov[0]["imported_at"]


def test_wrong_region_entity_is_not_found(resources: ResourceRegistry) -> None:
    # §V5: en data is never surfaced under a cn URI (en/cn never mixed).
    body = _body(resources.read("arknights://enemy/cn/enemy_1007_slime"))
    assert body["status"] == "not_found"


# --- §V27 public-safe sources -------------------------------------------------


def test_sources_read_is_public_safe(
    resources: ResourceRegistry, conn: sqlite3.Connection, registry: SourceRegistry
) -> None:
    result = resources.read("arknights://sources")
    body = _body(result)
    assert body["status"] == "ok"
    # §V27/§V34: identical to the service (routed through registry.public_view()).
    assert body["data"] == get_data_sources(registry, conn).to_dict()
    dumped = json.dumps(body)
    assert "policy_notes" not in dumped
    assert str(REPO_ROOT) not in dumped


# --- §V84/B144: the blackboard glossary's CLIENT-FETCHABLE home ---------------


def test_glossary_read_serves_the_single_entries_home(resources: ResourceRegistry) -> None:
    # §V84 (B144): the operator tools' descriptions used to point at the server
    # ``instructions``, an OPTIONAL initialize field a client may drop -- for such a
    # client the pointer named a surface that did not exist. This resource IS the home
    # they now name, and it projects the one shared entries tuple (§V37), so the resource
    # and the instructions can never disagree about a key's meaning.
    body = _body(resources.read("arknights://glossary/blackboard"))
    assert body["status"] == "ok"
    data = body["data"]
    entries = data["entries"]  # type: ignore[index]
    assert [(tuple(e["keys"]), e["meaning"]) for e in entries] == [
        (keys, meaning) for keys, meaning in BLACKBOARD_KEY_ENTRIES
    ]
    # Every glossed key really appears in the instructions projection of the same home.
    for entry in entries:
        for key in entry["keys"]:
            assert key in BLACKBOARD_KEY_GLOSSARY, key


def test_glossary_read_needs_no_database(registry: SourceRegistry) -> None:
    # Static project text (§V18/§V31: never assembled from imported source strings), so it
    # must stay readable on a server with no promoted build -- the §V65 (c) grounding path
    # cannot depend on the data being there.
    def broken_conn() -> sqlite3.Connection:
        raise DatabaseUnavailable("no build")

    resources = build_default_resources(broken_conn, registry=registry, mode="local")
    body = _body(resources.read("arknights://glossary/blackboard"))
    assert body["status"] == "ok"
    assert body["data"]["entries"]  # type: ignore[index]
    # It states no source fact, so it carries no region/provenance (§V5 governs facts).
    assert body["provenance"] == []


# --- §V23 typed status resource -----------------------------------------------


def test_status_read_is_region_scoped_with_provenance(resources: ResourceRegistry) -> None:
    body = _body(resources.read("arknights://status/en"))
    assert body["status"] in {"ok", "data_stale"}
    data = body["data"]
    assert data["server"] == "en"  # type: ignore[index]
    snaps = data["snapshots"]  # type: ignore[index]
    # §V66/B78 (T157): region + ``imported_at`` live on the top-level ``server`` +
    # envelope provenance; each snapshot row keeps the source/age extras PLUS its
    # ``snapshot_id`` -- the §V87 inline join key that ties the row to its
    # provenance entry (one region holds several active snapshots, so position
    # cannot). Rows stay server-less here (§V77: region stated once for a
    # region-scoped resource), unlike the multi-region tool's inline region key
    # (§V87/B96); null commit/version keys are scrubbed (§V67/B96).
    prov = body["provenance"]
    assert isinstance(prov, list) and prov and prov[0]["server"] == "en"
    prov_ids = {p["snapshot_id"] for p in prov}
    assert snaps
    for s in snaps:
        assert "server" not in s and "imported_at" not in s
        assert s["source_id"]
        assert s["snapshot_id"] in prov_ids
        assert "commit_sha" not in s and "upstream_version" not in s


def test_banners_read_is_region_scoped_ok_list(resources: ResourceRegistry) -> None:
    # §T114/§V62: banners/{server} dispatches get_banners -> a typed ok list body. The
    # 4-4 fixture has no gacha_table, so a region is a legitimate empty ``ok`` list
    # (tolerant-absent, §V41/B36), never a not_found.
    body = _body(resources.read("arknights://banners/en"))
    assert body["status"] == "ok"
    data = body["data"]
    # §V77/§V66 (B79): region stated ONCE on the parent server, never per row.
    assert set(data) == {"server", "banners", "page"}  # type: ignore[arg-type]
    assert data["server"] == "en"  # type: ignore[index]
    assert data["banners"] == []  # type: ignore[index]
    assert data["page"]["total"] == 0  # type: ignore[index]


def test_banners_unsupported_region_fails_closed(resources: ResourceRegistry) -> None:
    # §V5: a bad region short-circuits to unsupported_server, never a leaked value.
    body = _body(resources.read("arknights://banners/jp"))
    assert body["status"] == "unsupported_server"
    assert "jp" not in json.dumps(body["data"])


def test_status_other_region_has_no_snapshots(resources: ResourceRegistry) -> None:
    # Only the en fixture is imported; cn is region-scoped to an empty snapshot list.
    body = _body(resources.read("arknights://status/cn"))
    assert body["data"]["snapshots"] == []  # type: ignore[index]
    assert body["provenance"] == []
    # §V5: a region with no active snapshot is data_stale for that region -- the
    # global "ok" verdict (en is present) must not leak into the cn view.
    assert body["status"] == "data_stale"
    # §V99/§V66 (B148, T198): the verdict lives on the ENVELOPE only. The ``data.status``
    # echo is gone -- it duplicated the envelope carrier and shared its name with the
    # snapshot rows' own lifecycle state, in the same payload.
    assert "status" not in body["data"]  # type: ignore[operator]
    assert "analyzer_version" not in body["data"]  # type: ignore[operator]
    assert body["data"]["warnings"]  # type: ignore[index]


# --- §V23 fail-closed paths ---------------------------------------------------


def test_unsupported_region_fails_closed(resources: ResourceRegistry) -> None:
    body = _body(resources.read("arknights://enemy/jp/enemy_1007_slime"))
    assert body["status"] == "unsupported_server"
    # §V23: the message never echoes the untrusted URI region back.
    assert "jp" not in json.dumps(body["data"])


def test_status_unsupported_region_fails_closed(resources: ResourceRegistry) -> None:
    body = _body(resources.read("arknights://status/jp"))
    assert body["status"] == "unsupported_server"


def test_over_length_id_is_not_found(resources: ResourceRegistry) -> None:
    # §V18/§V23: an over-length id trips the tool's bounded model -> not_found, not a
    # leaked ValidationError.
    body = _body(resources.read("arknights://enemy/en/" + "x" * 500))
    assert body["status"] == "not_found"


def test_unknown_uri_raises(resources: ResourceRegistry) -> None:
    with pytest.raises(ResourceError):
        resources.read("arknights://weapon/en/thing")
    with pytest.raises(ResourceError):
        resources.read("https://example.com/whatever")


def test_database_unavailable_fails_closed(registry: SourceRegistry) -> None:
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: cand.sqlite")

    res = build_default_resources(boom, registry=registry, mode="local")
    body = _body(res.read("arknights://enemy/en/enemy_1007_slime"))
    assert body["status"] == "database_unavailable"
    # §V23: no local path / file name leaks into the client-facing body.
    assert "cand.sqlite" not in json.dumps(body)


# --- §V2 read-only ------------------------------------------------------------


def test_reads_do_not_write(resources: ResourceRegistry, conn: sqlite3.Connection) -> None:
    before = conn.total_changes
    resources.read("arknights://enemy/en/enemy_1007_slime")
    resources.read("arknights://stage/en/main_04-04")
    resources.read("arknights://status/en")
    resources.read("arknights://banners/en")
    resources.read("arknights://sources")
    assert conn.total_changes == before


# --- registration guards ------------------------------------------------------


def test_read_result_is_read_resource_result(resources: ResourceRegistry) -> None:
    result = resources.read("arknights://sources")
    assert isinstance(result, ReadResourceResult)
