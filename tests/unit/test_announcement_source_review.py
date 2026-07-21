"""T93 (M9): the announcement-source policy review is recorded in the registry
and its human-readable mirror.

T93 is a docs/registry-only task -- the announcement adapter, migration, and
importer land in T94-T96. This test guards the *review itself*: both official
news entries exist, stay disabled by default (§V56 / D14), carry a recorded
review (§V27 ``last_reviewed_at`` + a permission_status past "pending"), and are
scoped metadata-ONLY -- ``fields_consumed`` is a subset of the §V56 allowlist and
never names a body/html/prose/image field. The DATA_SOURCES.md mirror must state
the metadata-only scope and prohibit article bodies.

Guards SPEC §V56 (announcement importer metadata-only, disabled by default,
enablement requires a recorded review) and §V27 (registry completeness + mirror
in sync).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"
DATA_SOURCES_MD = REPO_ROOT / "DATA_SOURCES.md"

# §V56: the full metadata-only field allowlist. `fields_consumed` for an
# announcement source must be a subset of this -- nothing else may be ingested.
_V56_METADATA_FIELDS = {"announce_id", "title", "date", "url", "category", "region"}

# §V56/§V16: substrings that would signal full-body / prose / image ingestion.
# Neither the machine registry nor the mirror scope may name any of these as a
# consumed field.
_FORBIDDEN_SCOPE_TOKENS = ("body", "html", "prose", "image")

_ANNOUNCEMENT_SOURCES = {
    "arknights_global_official_news": "en",
    "arknights_cn_official_news": "cn",
}


def test_announcement_sources_present_enabled_and_region_scoped() -> None:
    # §V56/D14/§T106: both official news entries exist, are ENABLED by default (the M9
    # review satisfied the D14 gate; the metadata-only importer + get_announcements
    # landed), and are region-scoped (en/cn never mixed, §V5).
    reg = load_source_registry(REGISTRY, validate=False)
    for source_id, region in _ANNOUNCEMENT_SOURCES.items():
        entry = reg.get(source_id)
        assert entry is not None, f"missing announcement source: {source_id}"
        assert entry.enabled is True, f"{source_id} must be enabled by default (§V56/§T106)"
        assert entry.regions == [region], f"{source_id} region must be [{region!r}] (§V5)"


def test_announcement_review_is_recorded() -> None:
    # §V27/D14: enablement requires a recorded review. The M9 review stamps
    # last_reviewed_at and moves permission_status past the "pending" placeholder.
    reg = load_source_registry(REGISTRY, validate=False)
    for source_id in _ANNOUNCEMENT_SOURCES:
        entry = reg.get(source_id)
        assert entry is not None
        assert entry.last_reviewed_at == "2026-07-21", f"{source_id}: M9 review date not recorded"
        assert "pending" not in entry.permission_status, (
            f"{source_id}: permission_status still marked pending after review"
        )


def test_announcement_scope_is_metadata_only() -> None:
    # §V56/§V16: fields_consumed is a subset of the metadata allowlist and never
    # names a body/html/prose/image field -- metadata-only is the maximum scope.
    reg = load_source_registry(REGISTRY, validate=False)
    for source_id in _ANNOUNCEMENT_SOURCES:
        entry = reg.get(source_id)
        assert entry is not None
        consumed = set(entry.fields_consumed)
        assert consumed, f"{source_id}: fields_consumed must be recorded after review"
        assert consumed <= _V56_METADATA_FIELDS, (
            f"{source_id}: fields_consumed {consumed - _V56_METADATA_FIELDS} outside §V56 allowlist"
        )
        for field in entry.fields_consumed:
            low = field.lower()
            assert not any(bad in low for bad in _FORBIDDEN_SCOPE_TOKENS), (
                f"{source_id}: forbidden non-metadata field consumed: {field!r}"
            )


@pytest.mark.parametrize("source_id", sorted(_ANNOUNCEMENT_SOURCES))
def test_mirror_documents_metadata_only_scope(source_id: str) -> None:
    # §V27: the human mirror must document the metadata-only scope and prohibit
    # article bodies for each reviewed announcement source.
    md = DATA_SOURCES_MD.read_text(encoding="utf-8")
    assert source_id in md
    low = md.lower()
    assert "metadata-only" in low
    # The prohibition on full bodies must be explicit in the mirror.
    assert "never the article body" in low or "full announcement body" in low
    assert "2026-07-21" in md
