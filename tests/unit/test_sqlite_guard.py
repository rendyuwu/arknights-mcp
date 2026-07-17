"""§V37 / §V33: the single sqlite ``IntegrityError`` -> typed-error guard.

Three formerly inline copies (``enemies.insert_enemies``, ``levels.insert_level``,
``purge.purge_and_rebuild``) now route through
:func:`arknights_mcp.util.sqlite.integrity_guard`. This verifies the guard's
behaviour and that no divergent copies of the ``except sqlite3.IntegrityError``
translate-and-reraise pattern remain in those modules.
"""

from __future__ import annotations

import inspect
import sqlite3

import pytest

from arknights_mcp.db import purge as purge_mod
from arknights_mcp.importers import enemies as enemies_mod
from arknights_mcp.importers import levels as levels_mod
from arknights_mcp.util.sqlite import integrity_guard


class _Err(Exception):
    """Stand-in typed domain error for the guard tests."""


def _raise_integrity() -> None:
    """Provoke a real ``sqlite3.IntegrityError`` (duplicate PRIMARY KEY)."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (k INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO t (k) VALUES (1)")
        conn.execute("INSERT INTO t (k) VALUES (1)")
    finally:
        conn.close()


def test_guard_translates_with_prefix_message() -> None:
    # V33: a constraint anomaly becomes a typed error, chained to the original.
    with pytest.raises(_Err, match=r"boom: .*UNIQUE") as exc_info, integrity_guard("boom", _Err):
        _raise_integrity()
    assert isinstance(exc_info.value.__cause__, sqlite3.IntegrityError)


def test_guard_message_callable_receives_exc() -> None:
    with (
        pytest.raises(_Err, match=r"custom\(UNIQUE"),
        integrity_guard(lambda exc: f"custom({exc})", _Err),
    ):
        _raise_integrity()


def test_guard_runs_on_error_before_reraise() -> None:
    calls: list[str] = []
    with (
        pytest.raises(_Err),
        integrity_guard("x", _Err, on_error=lambda: calls.append("rollback")),
    ):
        _raise_integrity()
    assert calls == ["rollback"]


def test_guard_passes_through_non_integrity_error() -> None:
    # Only IntegrityError is translated; anything else propagates untouched.
    with pytest.raises(ValueError, match="unrelated"), integrity_guard("x", _Err):
        raise ValueError("unrelated")


def test_guard_is_transparent_on_success() -> None:
    marker = 0
    with integrity_guard("x", _Err):
        marker = 42
    assert marker == 42


def test_integrity_guard_has_single_home() -> None:
    # V37: the translate-and-reraise pattern lives in exactly one module; the
    # three former copies now import the shared guard and hold no local copy.
    for mod in (enemies_mod, levels_mod, purge_mod):
        src = inspect.getsource(mod)
        assert "except sqlite3.IntegrityError" not in src, mod.__name__
        assert "integrity_guard" in src, mod.__name__
    assert integrity_guard.__module__ == "arknights_mcp.util.sqlite"
