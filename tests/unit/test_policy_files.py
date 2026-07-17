"""T4: repository policy / legal files exist and carry their non-negotiable
content.

Guards SPEC §V16 (release artifact excludes raw data + game content; NOTICE
scopes the code license) and §V27 (source registry completeness in the
human-readable mirror `DATA_SOURCES.md`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_FILES = [
    "LICENSE",
    "NOTICE",
    "README.md",
    "DATA_SOURCES.md",
    "DATA_POLICY.md",
    "TAKEDOWN_POLICY.md",
    "PRIVACY.md",
    "SECURITY.md",
]

# Source ids that must appear in the human-readable registry mirror (§V27).
REGISTRY_SOURCE_IDS = [
    "arknights_assets_gamedata",
    "kengxxiao_gamedata",
    "penguin_statistics",
    "arknights_global_official_news",
    "arknights_cn_official_news",
    "local_snapshot",
]

# Mandatory per-source fields the mirror must surface for the enabled primary
# source (§V27 / PRD 10.1).
REGISTRY_FIELD_MARKERS = [
    "Owner",
    "Canonical URL",
    "License / permission status",
    "Redistribution status",
    "Required attribution",
    "Enabled",
    "Last reviewed",
]


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _norm(text: str) -> str:
    """Lower-case, whitespace-collapsed view so hard-wrapped prose still matches
    multi-word phrase assertions."""
    return " ".join(text.split()).lower()


@pytest.mark.parametrize("name", REQUIRED_FILES)
def test_policy_file_present_and_nonempty(name: str) -> None:
    path = REPO_ROOT / name
    assert path.is_file(), f"missing required policy file: {name}"
    assert path.read_text(encoding="utf-8").strip(), f"{name} is empty"


def test_license_is_apache_2() -> None:
    text = _read("LICENSE")
    assert "Apache License" in text
    assert "Version 2.0" in text


def test_notice_scopes_code_license_and_excludes_data() -> None:
    # V16: Apache-2.0 covers project code only; imported data/game content excluded.
    text = _read("NOTICE")
    assert "applies ONLY" in text and "source code" in text
    assert "does NOT" in text or "does not relicense" in text.lower()
    assert "imported data" in text.lower()
    # V16: no bundled raw snapshots or prebuilt databases in releases.
    norm = _norm(text)
    assert "prebuilt database" in norm
    assert "raw game-data snapshot" in norm


def test_readme_has_unofficial_disclaimer() -> None:
    text = _read("README.md").lower()
    assert "unofficial" in text
    assert "not affiliated" in text
    # Trademark / rights-holder acknowledgement.
    assert "hypergryph" in text and "yostar" in text


def test_data_sources_lists_every_source_id() -> None:
    text = _read("DATA_SOURCES.md")
    for source_id in REGISTRY_SOURCE_IDS:
        assert source_id in text, f"DATA_SOURCES.md missing source_id: {source_id}"


def test_data_sources_carries_mandatory_fields() -> None:
    # V27: mandatory registry fields must be present in the mirror.
    text = _read("DATA_SOURCES.md")
    for marker in REGISTRY_FIELD_MARKERS:
        assert marker in text, f"DATA_SOURCES.md missing mandatory field marker: {marker}"


def test_data_policy_core_rules() -> None:
    text = _read("DATA_POLICY.md").lower()
    assert "allowlist" in text
    assert "bulk" in text  # no bulk dump
    assert "provenance" in text
    assert "record_hash" in text or "content hash" in text


def test_takedown_policy_procedure() -> None:
    text = _read("TAKEDOWN_POLICY.md")
    assert "disable" in text.lower()
    assert "purge" in text.lower()
    assert "--rebuild" in text
    assert "contact" in text.lower()


def test_privacy_no_credentials_and_retention() -> None:
    text = _read("PRIVACY.md").lower()
    assert "credential" in text
    assert "retention" in text
    assert "roster" in text
