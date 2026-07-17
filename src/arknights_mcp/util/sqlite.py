"""Shared sqlite constraint-error guard (§V37 DRY, §V33 fail-closed).

A duplicate, absent, or otherwise anomalous source constraint (dup PK, repeated
variant, missing index) surfaces mid-build as a low-level ``sqlite3.IntegrityError``.
Left uncaught it tears down the whole multi-region candidate build with a raw
traceback (§V33 / §V3). Every importer + purge write path guarded against this
with the *same* translate-and-reraise pattern; that pattern now lives here exactly
once (§V37).

The variance between the former copies -- which typed error to raise, the message,
and any cleanup (e.g. a transaction rollback) -- is passed explicitly, never forked
into silent divergent copies (§V37).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager


@contextmanager
def integrity_guard(
    message: str | Callable[[sqlite3.IntegrityError], str],
    error_type: type[Exception],
    *,
    on_error: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Translate a ``sqlite3.IntegrityError`` raised in the block into ``error_type``.

    ``message`` is either a fixed prefix -- the raised message is then
    ``f"{message}: {exc}"`` -- or a callable given the caught exception that returns
    the full message. ``on_error``, when set, runs before re-raising (used to roll
    back an open transaction). Non-``IntegrityError`` exceptions propagate untouched.
    """
    try:
        yield
    except sqlite3.IntegrityError as exc:
        if on_error is not None:
            on_error()
        text = message(exc) if callable(message) else f"{message}: {exc}"
        raise error_type(text) from exc
