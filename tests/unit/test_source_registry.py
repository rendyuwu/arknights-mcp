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
    RegistryError,
    SourceRegistry,
    SourceRegistryEntry,
    load_source_registry,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"
EXAMPLE_REGISTRY = REPO_ROOT / "config" / "data_sources.example.toml"
DATA_SOURCES_MD = REPO_ROOT / "DATA_SOURCES.md"

EXPECTED_SOURCE_IDS = {
    "arknights_assets_gamedata",
    "local_snapshot",
    "kengxxiao_gamedata",
    "penguin_statistics",
    "arknights_global_official_news",
    "arknights_cn_official_news",
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
    # M4: the CLI `source list --json` view and the get_data_sources service must
    # exclude exactly the internal-only fields and agree on the intended-public
    # posture, so a field added to only one path can't silently leak or vanish.
    reg = load_source_registry(REGISTRY)
    cli_keys: set[str] = set().union(*(e.keys() for e in reg.public_registry()))
    svc_keys: set[str] = set().union(*(s.to_dict().keys() for s in get_data_sources(reg).sources))
    assert _INTERNAL_ONLY_FIELDS.isdisjoint(cli_keys)
    assert _INTERNAL_ONLY_FIELDS.isdisjoint(svc_keys)
    for field in ("private_hosting_status", "redistribution_status", "license_status"):
        assert field in cli_keys, field
        assert field in svc_keys, field


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
