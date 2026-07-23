"""T9: machine-readable source registry loads, is complete for enabled sources
(§V27), stays in sync with the DATA_SOURCES.md mirror, and its public view omits
internal-only fields.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arknights_mcp.services.source_status import get_data_sources
from arknights_mcp.sources.registry import (
    _INTERNAL_ONLY_FIELDS,
    _PUBLIC_FIELDS,
    RegistryError,
    SourceRegistry,
    SourceRegistryEntry,
    load_source_registry,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"
EXAMPLE_REGISTRY = REPO_ROOT / "config" / "data_sources.example.toml"
DATA_SOURCES_MD = REPO_ROOT / "DATA_SOURCES.md"
NOTICE = REPO_ROOT / "NOTICE"

EXPECTED_SOURCE_IDS = {
    "arknights_assets_gamedata",
    "local_snapshot",
    "kengxxiao_gamedata",
    "penguin_statistics",
    "arknights_global_official_news",
    "arknights_cn_official_news",
    "arknights_game_resource",
}


def test_registry_loads_and_is_complete() -> None:
    reg = load_source_registry(REGISTRY)  # validate=True by default
    assert set(reg.entries) == EXPECTED_SOURCE_IDS


def test_example_registry_matches_runtime_registry() -> None:
    assert REGISTRY.read_text(encoding="utf-8") == EXAMPLE_REGISTRY.read_text(encoding="utf-8")


def test_primary_source_enabled_with_mandatory_fields() -> None:
    reg = load_source_registry(REGISTRY)
    primary = reg.get("arknights_assets_gamedata")
    assert primary is not None
    assert primary.enabled is True
    assert primary.missing_mandatory_fields() == []
    assert primary.regions == ["en", "cn"]


def test_registry_mirror_lists_every_source_id() -> None:
    # DATA_SOURCES.md (human mirror) must list every machine-registry source_id.
    reg = load_source_registry(REGISTRY)
    md = DATA_SOURCES_MD.read_text(encoding="utf-8")
    for source_id in reg.entries:
        assert source_id in md, f"DATA_SOURCES.md out of sync: missing {source_id}"


def test_public_view_omits_internal_fields() -> None:
    reg = load_source_registry(REGISTRY)
    for entry in reg.public_registry():
        # policy_notes (may carry takedown correspondence) is the only internal field.
        assert "policy_notes" not in entry
        # Public-safe fields are present (name, URL, status, attribution, review).
        assert entry["source_id"]
        assert "attribution_text" in entry
        assert "last_reviewed_at" in entry
        # PRD §13.10 posture is intended-public and must appear (aligns with the
        # get_data_sources service so the two projections cannot diverge, M4).
        assert "private_hosting_status" in entry
        assert "redistribution_status" in entry


def test_public_projections_do_not_diverge() -> None:
    # §V34/B18: the CLI `source list --json` view and the get_data_sources service
    # must emit an IDENTICAL public field set -- both route through the single
    # registry.public_view() projection -- apart from the DB-only active_snapshots
    # enrichment the service adds. A set-equality assert (not a named-field spot
    # check) is what catches a re-forked allowlist: B18 slipped through precisely
    # because the service re-enumerated fields and dropped adapter_version /
    # transform_version while the CLI view kept them.
    reg = load_source_registry(REGISTRY)
    cli_keys: set[str] = set().union(*(e.keys() for e in reg.public_registry()))
    svc_keys: set[str] = set().union(*(s.to_dict().keys() for s in get_data_sources(reg).sources))
    assert _INTERNAL_ONLY_FIELDS.isdisjoint(cli_keys)
    assert _INTERNAL_ONLY_FIELDS.isdisjoint(svc_keys)
    # The service adds exactly the DB-only enrichment and re-forks nothing else.
    assert svc_keys - cli_keys == {"active_snapshots"}
    assert cli_keys == svc_keys - {"active_snapshots"}


def test_public_view_is_allowlist_partition() -> None:
    # §V27/§V34 (finding #5): public_view is an allowlist, not a denylist. Every
    # model field must be classified as either public or internal-only, and the two
    # sets are disjoint -- so a field added to SourceRegistryEntry is withheld from
    # clients until explicitly classified (fail-closed), rather than leaking by
    # default the way a model_dump()+pop denylist would.
    model_fields = set(SourceRegistryEntry.model_fields)
    assert _PUBLIC_FIELDS.isdisjoint(_INTERNAL_ONLY_FIELDS)
    assert model_fields == _PUBLIC_FIELDS | _INTERNAL_ONLY_FIELDS, (
        "every SourceRegistryEntry field must be classified public or internal-only"
    )
    # The projection emits exactly the public allowlist -- no more, no less.
    entry = SourceRegistryEntry(source_id="x")
    assert set(entry.public_view()) == _PUBLIC_FIELDS


def test_incomplete_enabled_source_rejected() -> None:
    # V27: an enabled source missing a mandatory field must fail validation.
    bad = SourceRegistry(
        entries={
            "x": SourceRegistryEntry(
                source_id="x",
                enabled=True,
                owner_name="",  # missing
                canonical_url="https://example.com",
                source_type="t",
                purpose="p",
                license_status="l",
                permission_status="pm",
                attribution_text="a",
                last_reviewed_at="2026-07-17",
                regions=["en"],
            )
        }
    )
    with pytest.raises(RegistryError, match="V27"):
        bad.assert_complete()


def test_disabled_source_exempt_from_completeness() -> None:
    reg = SourceRegistry(
        entries={
            "x": SourceRegistryEntry(source_id="x", enabled=False),
        }
    )
    reg.assert_complete()  # must not raise


# --- T118: image-ref source `arknights_game_resource` (§V27/§V63/§V16) ---------


def test_image_ref_source_registered_and_complete() -> None:
    # §V27: the new source is present and, though disabled-by-default, still
    # populates every §V27 mandatory field (the task requires completeness; a
    # snapshot commit is N/A because nothing is imported). The runtime registry
    # still loads (validate=True) since a disabled source is exempt from the
    # enabled-only completeness gate.
    reg = load_source_registry(REGISTRY)
    entry = reg.get("arknights_game_resource")
    assert entry is not None
    assert entry.missing_mandatory_fields() == []
    assert entry.owner_name == "yuanyan3060"
    assert entry.canonical_url == "https://github.com/yuanyan3060/ArknightsGameResource"
    assert entry.last_reviewed_at == "2026-07-22"
    # §V27: the public projection carries no internal-only field (e.g. policy_notes,
    # which may hold takedown correspondence) and no secret/local-path/OAuth key.
    public = entry.public_view()
    assert "policy_notes" not in public
    assert public["attribution_text"]


def test_image_ref_source_on_by_default_regions_and_posture() -> None:
    # §V63/§T124: image URL REFERENCE source is ON by default (founder 2026-07-22),
    # region-scoped en/cn, owned by yuanyan3060, and records the AGPL-code /
    # Yostar-copyright / removal-on-request permission posture. No snapshot commit (no import).
    reg = load_source_registry(REGISTRY)
    entry = reg.get("arknights_game_resource")
    assert entry is not None
    assert entry.enabled is True  # ON by default (§T124); still private + noncommercial
    assert entry.regions == ["en", "cn"]  # region-scoped, en/cn never mixed
    assert entry.license_identifier == "AGPL-3.0"  # mirror CODE license
    # Permission posture: not granted, private/noncommercial, removal on request.
    assert "removal_on_request" in entry.permission_status
    assert "noncommercial" in entry.private_hosting_status
    assert "never_public" in entry.private_hosting_status
    # No adapter/transform/snapshot: pure query-time derivation, nothing imported.
    assert entry.fields_consumed  # documented as "none -- no import ..."
    assert "no import" in entry.fields_consumed[0]


def test_image_ref_source_reference_link_only_never_bytes() -> None:
    # §V16: redistribution posture is reference-link only, NEVER bytes; the human
    # mirror (DATA_SOURCES.md) and NOTICE both record the no-bytes attribution so
    # a release artifact carries the reference-only posture, not artwork.
    reg = load_source_registry(REGISTRY)
    entry = reg.get("arknights_game_resource")
    assert entry is not None
    assert entry.redistribution_status == "reference_link_only_never_bytes"

    md = DATA_SOURCES_MD.read_text(encoding="utf-8")
    assert "arknights_game_resource" in md
    assert "never bytes" in md  # reference-link only, no image bytes

    notice = NOTICE.read_text(encoding="utf-8")
    assert "ArknightsGameResource" in notice
    assert "reference-link only, never bytes" in notice
