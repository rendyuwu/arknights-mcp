"""T11: field allowlist + untrusted-string sanitization (§V18)."""

from __future__ import annotations

from arknights_mcp.importers.field_policy import (
    ENEMY_HANDBOOK_ALLOWLIST,
    FIELD_POLICY_VERSION,
    OVERWRITTEN_DATA_ALLOWLIST,
    apply_allowlist,
)
from arknights_mcp.util.text import DEFAULT_MAX_TEXT_LENGTH, sanitize_text, strip_control_chars


def test_field_policy_version_present() -> None:
    # 2: B46/§V59 added name_i18n to ITEM_ALLOWLIST (region-locale item names).
    assert FIELD_POLICY_VERSION == "2"


def test_allowlist_drops_unlisted_prose() -> None:
    raw = {
        "enemyId": "enemy_1007_slime",
        "name": "Originium Slug",
        "enemyLevel": "NORMAL",
        "description": "A long lore blurb that must never be imported.",  # prose, not allowlisted
        "hideInHandbook": False,
    }
    result = apply_allowlist(raw, ENEMY_HANDBOOK_ALLOWLIST)
    assert set(result.kept) <= ENEMY_HANDBOOK_ALLOWLIST
    assert "description" not in result.kept
    assert "description" in result.dropped
    assert "hideInHandbook" in result.dropped
    assert result.kept["enemyId"] == "enemy_1007_slime"


def test_overwritten_data_allowlist_drops_variant_prose() -> None:
    """§T80/§V18: a useDb:false ref's overwrittenData carries prose (name/
    description) alongside its stats; only the structural keys survive the
    allowlist, so the inline variant is built without any prose leaf."""
    raw = {
        "prefabKey": {"m_defined": True, "m_value": "enemy_1105_tyokai"},
        "attributes": {"def": {"m_defined": True, "m_value": 9999}},
        "motion": {"m_defined": True, "m_value": "FLY"},
        "name": {"m_defined": True, "m_value": "A prose display name"},
        "description": "A lore blurb that must never be imported.",
    }
    result = apply_allowlist(raw, OVERWRITTEN_DATA_ALLOWLIST)
    assert set(result.kept) <= OVERWRITTEN_DATA_ALLOWLIST
    assert "name" in result.dropped
    assert "description" in result.dropped
    assert "prefabKey" in result.kept
    assert "attributes" in result.kept
    assert "motion" in result.kept


def test_allowlist_sanitizes_kept_strings() -> None:
    raw = {"name": "Bad‮name\x00\t here", "enemyId": "e1"}
    result = apply_allowlist(raw, ENEMY_HANDBOOK_ALLOWLIST)
    # Control + bidi-override chars stripped; value trimmed.
    assert "‮" not in result.kept["name"]
    assert "\x00" not in result.kept["name"]
    assert "\t" not in result.kept["name"]


def test_allowlist_keeps_nonstring_values() -> None:
    raw = {"level": 0, "hp": 1650, "abilities": ["fly"], "def": 100}
    from arknights_mcp.importers.field_policy import ENEMY_LEVEL_ALLOWLIST

    result = apply_allowlist(raw, ENEMY_LEVEL_ALLOWLIST)
    assert result.kept["level"] == 0
    assert result.kept["hp"] == 1650
    assert result.kept["abilities"] == ["fly"]


def test_allowlist_sanitizes_nested_string_leaves() -> None:
    # H3/§V18: control + bidi chars nested inside kept dict/list values are
    # stripped too, not only top-level strings.
    from arknights_mcp.importers.field_policy import ENEMY_LEVEL_ALLOWLIST

    raw = {
        "abilities": ["fl‮y\x00", {"note": "wat\x00ch"}],
        "immunities": {"‮key": "v\x00al"},
    }
    result = apply_allowlist(raw, ENEMY_LEVEL_ALLOWLIST)
    blob = str(result.kept)
    assert "‮" not in blob  # bidi override stripped at every depth
    assert "\x00" not in blob  # NUL stripped at every depth
    assert "fly" in blob  # benign content preserved


def test_sanitize_caps_length() -> None:
    long = "x" * (DEFAULT_MAX_TEXT_LENGTH + 50)
    assert len(sanitize_text(long)) == DEFAULT_MAX_TEXT_LENGTH


def test_strip_control_chars_removes_controls_keeps_spaces() -> None:
    assert strip_control_chars("a\x00b\tc\nd") == "abcd"
    assert strip_control_chars("keep spaces") == "keep spaces"
