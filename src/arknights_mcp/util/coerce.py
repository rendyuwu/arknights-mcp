"""Shared JSON-value coercion helpers for importers (§V37 DRY).

These four helpers were previously copy-pasted across ``importers/enemies.py``,
``importers/levels.py``, and ``importers/stages.py``. They live here as the
single home so a behavioural change happens in exactly one place.

The one behavioural difference between the old copies -- ``levels`` sanitized
strings while ``enemies``/``stages`` returned them raw -- is now an explicit
``sanitize=`` argument on :func:`as_str`, not a silent divergent copy (§V37).
That difference is intentional: ``enemies``/``stages`` read from a ``kept`` dict
already sanitized by :func:`~arknights_mcp.importers.field_policy.apply_allowlist`,
whereas ``levels`` reads raw tile/wave dicts that never passed through the
allowlist and so must sanitize their own string leaves (§V18).
"""

from __future__ import annotations

import json
from typing import Any

from arknights_mcp.util.text import sanitize_text


def as_int(value: Any) -> int | None:
    """Coerce a JSON numeric/bool scalar to ``int``; anything else to ``None``."""
    return int(value) if isinstance(value, bool | int | float) else None


def as_float(value: Any) -> float | None:
    """Coerce a JSON numeric/bool scalar to ``float``; anything else to ``None``."""
    return float(value) if isinstance(value, bool | int | float) else None


def as_str(value: Any, *, sanitize: bool = False) -> str | None:
    """Return ``value`` if it is a ``str`` else ``None``.

    ``sanitize=True`` runs the string through
    :func:`~arknights_mcp.util.text.sanitize_text` (strip control/format chars,
    cap length) -- used by callers reading raw, non-allowlisted source dicts.
    """
    if not isinstance(value, str):
        return None
    return sanitize_text(value) if sanitize else value


def json_or_none(value: Any) -> str | None:
    """JSON-encode ``value`` (stable key order) or return ``None`` for ``None``."""
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)
