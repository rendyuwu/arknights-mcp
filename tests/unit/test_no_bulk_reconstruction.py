"""§T63 (M7) consolidated no-bulk-reconstruction suite (§V19; §V22).

The per-tool tests (``test_input_models`` / ``test_search_service`` /
``test_get_stage_tool`` / ``test_search_entities_tool``) already prove each bound
in isolation. This suite makes the *aggregate* §V19 claim the others cannot: that
the MCP tool surface **as a whole** offers no path to reconstruct the dataset. It
asserts the systemic properties, not the individual bounds again (§V37 DRY):

1. the full tool registry exposes no bulk-dump / DB-download / list-everything /
   admin capability -- and every registered tool is a *classified, bounded* shape
   (a new unbounded tool would leave the partition incomplete and fail here);
2. the §V19 window is one contract enforced identically at BOTH layers -- the
   Pydantic model *and* the service reject an out-of-range limit/page_size, with
   no silent clamp/widen in either (the B23 hole), asserted as a cross-surface
   parity matrix rather than per tool;
3. search is a hard cap with no walk (no offset/page/cursor), and the entity tools
   are selector-gated, so neither the search window nor pagination can be walked to
   enumerate the whole dataset;
4. the §V22 response cap withholds (fails closed to ``partial`` with data dropped)
   rather than leaking an oversized payload, so it is not a reconstruction vector.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.envelopes import MAX_RESPONSE_BYTES, ok, serialized_size
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.models.common import PAGE_SIZE_MAX, PageParams
from arknights_mcp.models.search import SearchEntitiesInput
from arknights_mcp.models.stages import GetStageInput, SearchStagesInput
from arknights_mcp.services.search import MAX_LIMIT, search_entities, search_stages
from arknights_mcp.services.stages import get_stage
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: The full §I.tool set. Any drift here (a tool added or removed) is a deliberate
#: surface change that must be re-classified in :func:`test_every_tool_is_bounded`.
_EXPECTED_TOOLS = frozenset(
    {
        "search_entities",
        "search_stages",
        "get_stage",
        "get_enemy",
        "get_operator",
        "compare_operator_modules",
        "analyze_stage",
        "get_data_status",
        "get_data_sources",
    }
)

#: §V19 classification of the surface. The union must cover the whole registry, so
#: an unclassified (potentially unbounded) new tool fails the partition assertion.
_SEARCH_TOOLS = frozenset({"search_entities", "search_stages"})  # bounded window
_STAGE_DETAIL_TOOLS = frozenset({"get_stage"})  # selector-gated + paged sections
_KEYED_ENTITY_TOOLS = frozenset(  # one entity by unique key/selector, no list knob
    {"get_enemy", "get_operator", "compare_operator_modules", "analyze_stage"}
)
_POSTURE_TOOLS = frozenset({"get_data_status", "get_data_sources"})  # fixed metadata

#: A tool name that leaks any of these is an enumeration/admin surface (§V19/§V28).
_FORBIDDEN_NAME_SUBSTRINGS = (
    "dump",
    "export",
    "download",
    "backup",
    "list_all",
    "all_entities",
    "everything",
    "raw",
    "sql",
    "sync",
    "import",
    "validate",
    "purge",
    "serve",
    "delete",
    "insert",
    "update",
    "write",
)

#: Free-slice knobs that would let a caller pull an unbounded / walkable window.
#: (``get_stage`` pages one *selected* stage's sub-rows via bounded ``*_page``
#: objects, not a top-level ``page``/``offset``, so it is excluded by name.)
_ENUMERATION_KNOBS = frozenset({"offset", "cursor", "skip", "page", "limit"})


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Build the 4-4 fixture candidate read-only (shared production path, §V2)."""
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    return open_read_only(path)


@pytest.fixture
def tool_registry(conn: sqlite3.Connection) -> ToolRegistry:
    """The single shared registry both transports dispatch (§V14) -- the exact
    tool surface a client can reach. Handlers are never invoked here (the schema +
    name enumeration needs no query), so the connection provider is only stored."""
    return build_tool_registry(
        lambda: conn,
        registry=load_source_registry(REGISTRY),
        mode="stdio",
    )


# --- (1) the tool surface offers no bulk-dump / admin capability ---------------


def test_registry_is_exactly_the_nine_read_only_tools(tool_registry: ToolRegistry) -> None:
    # The surface is closed: exactly the §I.tool set, every one read-only. An admin
    # op (sync/import/purge, §V28) or a dump tool could not be here -- the registry
    # rejects a non-read-only spec, and this pins the membership.
    assert set(tool_registry.names()) == _EXPECTED_TOOLS
    assert all(spec.read_only for spec in tool_registry.specs())


def test_no_tool_name_exposes_a_dump_or_admin_surface(tool_registry: ToolRegistry) -> None:
    # §V19/§V28: no tool advertises bulk export / DB download / raw-SQL / admin
    # mutation. Guards a future addition, not just today's names.
    for name in tool_registry.names():
        lowered = name.lower()
        offenders = [bad for bad in _FORBIDDEN_NAME_SUBSTRINGS if bad in lowered]
        assert not offenders, f"tool {name!r} name suggests a dump/admin surface: {offenders}"


def test_every_tool_is_classified_and_bounded(tool_registry: ToolRegistry) -> None:
    # §V19: the classification must partition the *whole* registry -- an unclassified
    # tool (a plausible unbounded escape hatch) fails here and forces a reviewer to
    # place it in a bounded category.
    covered = _SEARCH_TOOLS | _STAGE_DETAIL_TOOLS | _KEYED_ENTITY_TOOLS | _POSTURE_TOOLS
    assert set(tool_registry.names()) == covered

    for spec in tool_registry.specs():
        schema = spec.input_schema
        # §V18: every tool is a closed object -- no smuggled fields past the model.
        assert schema.get("additionalProperties") is False, spec.name
        props = set(schema.get("properties", {}))

        if spec.name in _SEARCH_TOOLS:
            # Bounded window on the wire + no walk knob (offset/cursor/page absent).
            assert schema["properties"]["limit"]["maximum"] == MAX_LIMIT
            assert schema["properties"]["limit"]["minimum"] == 1
            assert (props & _ENUMERATION_KNOBS) == {"limit"}
        elif spec.name in _STAGE_DETAIL_TOOLS:
            # Selector-gated (must name one stage) + each heavy section is a bounded
            # PageParams; no top-level enumeration knob.
            assert {"stage_code", "game_id"} <= props
            assert "server" in schema.get("required", [])
            page_size = schema["$defs"]["PageParams"]["properties"]["page_size"]
            assert page_size["maximum"] == PAGE_SIZE_MAX
            assert (props & _ENUMERATION_KNOBS) == set()
        elif spec.name in _KEYED_ENTITY_TOOLS:
            # Returns one entity addressed by a unique key/selector -- no list knob.
            assert props & {"game_id", "stage_code"}
            assert "server" in schema.get("required", [])
            assert (props & _ENUMERATION_KNOBS) == set()
        else:  # posture tools: no client-controlled enumeration input at all
            assert props == set()


# --- (2) the §V19 window is rejected identically at BOTH layers (no clamp) ------


@pytest.mark.parametrize("bad", [0, -1, MAX_LIMIT + 1, 100])
def test_search_limit_rejected_at_model_and_service(conn: sqlite3.Connection, bad: int) -> None:
    # One §V19 contract, enforced twice with no silent clamp/widen (B23): the model
    # gate rejects, and a caller reaching the service directly gets the *same*
    # rejection -- it raises rather than returning a clamped <=MAX_LIMIT result.
    with pytest.raises(ValidationError):
        SearchEntitiesInput(query="drone", limit=bad)
    with pytest.raises(ValidationError):
        SearchStagesInput(query="4-4", limit=bad)
    with pytest.raises(ValueError):
        search_entities(conn, query="drone", limit=bad)
    with pytest.raises(ValueError):
        search_stages(conn, query="4-4", limit=bad)


@pytest.mark.parametrize("bad_size", [0, -1, PAGE_SIZE_MAX + 1])
def test_page_size_rejected_at_model_and_service(conn: sqlite3.Connection, bad_size: int) -> None:
    # Same two-layer, no-clamp contract for pagination: model gate + service both
    # reject an out-of-range page_size on the one paged tool (get_stage).
    with pytest.raises(ValidationError):
        PageParams(page_size=bad_size)
    with pytest.raises(ValidationError):
        PageParams(page_size=bad_size)  # nested page bound, validated on construction
    with pytest.raises(ValueError, match="page_size"):
        get_stage(conn, server="en", stage_code="4-4", map_page_size=bad_size)


def test_page_below_one_rejected_at_model_and_service(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        PageParams(page=0)  # nested page bound, validated on construction
    with pytest.raises(ValueError, match="page"):
        get_stage(conn, server="en", stage_code="4-4", spawns_page=0)


# --- (3) neither the search window nor pagination is walkable to enumerate ------


def test_search_window_is_a_hard_cap_with_no_walk() -> None:
    # §V19: search takes only a bounded ``limit`` -- no offset/page/cursor/skip. So
    # the max window (50) is a hard ceiling: rows beyond it are unreachable, there
    # is no cursor to fetch the "next" page and enumerate the dataset.
    for model in (SearchEntitiesInput, SearchStagesInput):
        fields = set(model.model_fields)
        walk_knobs = fields & (_ENUMERATION_KNOBS - {"limit"})
        assert walk_knobs == set(), f"{model.__name__} exposes a walk knob: {walk_knobs}"


def test_stage_pagination_requires_a_selector_so_stages_are_not_listable(
    conn: sqlite3.Connection,
) -> None:
    # §V19: get_stage's bounded pagination only walks one *already-selected* stage's
    # sub-rows; it cannot list stages. Neither selector -> rejected, so there is no
    # "get every stage" call to seed an enumeration.
    with pytest.raises(ValidationError):
        GetStageInput(server="en")
    # A named stage's section is capped per page (never an unbounded slice).
    page = get_stage(
        conn, server="en", stage_code="4-4", include_map=True, map_page_size=1
    ).tiles_page
    assert page is not None and page.page_size == 1 and page.total >= len(("only-a-cap-check",))


# --- (4) the §V22 response cap withholds; it is not a reconstruction vector ------


def test_response_cap_is_the_documented_200kb_ceiling() -> None:
    # §V22: default response < 200 KB. The cap is measured worst-case ASCII-escaped
    # (B21), so this constant is the on-the-wire ceiling regardless of encoding.
    assert MAX_RESPONSE_BYTES == 200_000


def test_oversized_payload_fails_closed_and_drops_data() -> None:
    # §V22: an oversized payload is not emitted -- the builder fails closed to a
    # bounded ``partial`` with the data dropped + a cap limitation. So a caller
    # cannot use one huge response to exfiltrate a bulk slice; the data is withheld,
    # never truncated-but-leaked.
    blob = "x" * (MAX_RESPONSE_BYTES + 50_000)
    envelope = ok({"blob": blob})
    assert envelope.status == "partial"
    assert dict(envelope.data) == {}
    assert any("cap" in limitation for limitation in envelope.limitations)
    # The withheld payload does not survive anywhere in the serialized envelope, and
    # the emitted response is itself under the cap.
    rendered = str(envelope.to_dict())
    assert blob not in rendered
    assert serialized_size(envelope) <= MAX_RESPONSE_BYTES
