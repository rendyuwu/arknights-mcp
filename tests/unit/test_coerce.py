"""Shared coercion helpers + DRY guard (SPEC §V37; T70).

``as_int``/``as_float``/``as_str``/``json_or_none`` were copy-pasted across the
three importers. They now live in one home (``util/coerce.py``); the one-time
behavioural fork (``levels`` sanitized, ``enemies``/``stages`` did not) is an
explicit ``sanitize=`` argument, not a silent divergent copy. These tests pin
both the behaviour and the no-re-duplication guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arknights_mcp.importers import enemies, levels, stages
from arknights_mcp.util.coerce import as_float, as_int, as_str, json_or_none
from arknights_mcp.util.text import DEFAULT_MAX_TEXT_LENGTH

# A control (Cc) char and a bidi-override format (Cf) char sanitize_text strips.
_INJECTION = "safe\x00te‮xt"
_SANITIZED = "safetext"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5, 5),
        (True, 1),
        (False, 0),
        (3.9, 3),  # truncates toward zero like int()
        ("7", None),  # numeric-looking strings are NOT coerced
        (None, None),
        ([1], None),
        ({}, None),
    ],
)
def test_as_int(value: object, expected: int | None) -> None:
    assert as_int(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5, 5.0),
        (2.5, 2.5),
        (True, 1.0),
        ("2.5", None),
        (None, None),
        ([], None),
    ],
)
def test_as_float(value: object, expected: float | None) -> None:
    assert as_float(value) == expected


def test_as_str_default_is_raw() -> None:
    # Default (enemies/stages path): the string passes through untouched because
    # the caller already ran it through apply_allowlist's sanitize_value (§V18).
    assert as_str(_INJECTION) == _INJECTION
    assert as_str(123) is None
    assert as_str(None) is None


def test_as_str_sanitize_strips_control_and_format_chars() -> None:
    # levels path: reads raw, non-allowlisted dicts, so must sanitize itself.
    assert as_str(_INJECTION, sanitize=True) == _SANITIZED
    assert as_str(None, sanitize=True) is None


def test_as_str_sanitize_caps_length() -> None:
    long = "x" * (DEFAULT_MAX_TEXT_LENGTH + 50)
    assert len(as_str(long, sanitize=True) or "") == DEFAULT_MAX_TEXT_LENGTH
    # raw mode never caps
    assert len(as_str(long) or "") == DEFAULT_MAX_TEXT_LENGTH + 50


def test_json_or_none() -> None:
    assert json_or_none(None) is None
    # stable key order + non-ASCII preserved
    assert json_or_none({"b": 1, "a": "é"}) == json.dumps(
        {"a": "é", "b": 1}, ensure_ascii=False, sort_keys=True
    )
    assert json_or_none([1, 2]) == "[1, 2]"


def test_all_importers_share_the_one_home() -> None:
    """§V37: the coerce helpers resolve to the single ``util.coerce`` module."""
    for mod in (enemies, levels, stages):
        for name in ("as_int", "as_str"):
            fn = getattr(mod, name)
            assert fn.__module__ == "arknights_mcp.util.coerce", (
                f"{mod.__name__}.{name} is not the shared util.coerce helper"
            )
    # only levels uses float/json coercion of the four; assert it too shares.
    assert as_float.__module__ == "arknights_mcp.util.coerce"
    assert json_or_none.__module__ == "arknights_mcp.util.coerce"


def test_no_importer_redefines_coerce_helpers() -> None:
    """§V37: no importer re-introduces a local copy (`def _as_int` etc.)."""
    importer_dir = Path(enemies.__file__).resolve().parent
    banned = ("def _as_int", "def _as_float", "def _as_str", "def _json_or_none")
    offenders: list[str] = []
    for path in sorted(importer_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert not offenders, f"copy-pasted coerce helpers reintroduced: {offenders}"
