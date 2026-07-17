"""Content and record hashing helpers (SPEC §V17).

Deterministic hashes for provenance: file content hashes for the snapshot
manifest, and canonical-JSON record hashes for per-record provenance.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256_hex(data: bytes) -> str:
    """Hex SHA-256 of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> bytes:
    """Deterministic UTF-8 JSON encoding (sorted keys, tight separators)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def record_hash(obj: Any) -> str:
    """Stable SHA-256 over a record's canonical JSON (§V17 ``record_hash``)."""
    return sha256_hex(canonical_json(obj))
