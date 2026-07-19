"""§T52/§V10: granted-scope union + required-scope AND semantics."""

from __future__ import annotations

from arknights_mcp.auth.scopes import granted_scopes, has_required_scopes, missing_scopes


def test_granted_scopes_from_scope_string() -> None:
    granted = granted_scopes({"scope": "arknights:read arknights:stages"})
    assert granted == frozenset({"arknights:read", "arknights:stages"})


def test_granted_scopes_from_permissions_array() -> None:
    granted = granted_scopes({"permissions": ["arknights:read", "arknights:enemies"]})
    assert granted == frozenset({"arknights:read", "arknights:enemies"})


def test_granted_scopes_is_union_of_both_sources() -> None:
    # §V10: granted = scope ∪ permissions.
    granted = granted_scopes({"scope": "arknights:read", "permissions": ["arknights:stages"]})
    assert granted == frozenset({"arknights:read", "arknights:stages"})


def test_granted_scopes_ignores_wrong_types() -> None:
    # Fail-closed: an unparseable authority source contributes nothing, never all.
    assert granted_scopes({"scope": 123, "permissions": "not-a-list"}) == frozenset()
    assert granted_scopes({"permissions": ["ok", 5, None]}) == frozenset({"ok"})


def test_has_required_scopes_is_and() -> None:
    # §V10: every required scope must be present (AND).
    assert has_required_scopes({"a", "b", "c"}, ["a", "b"])
    assert not has_required_scopes({"a"}, ["a", "b"])


def test_missing_scopes_reports_the_gap() -> None:
    assert missing_scopes({"a"}, ["a", "b"]) == frozenset({"b"})
    assert missing_scopes({"a", "b"}, ["a", "b"]) == frozenset()
