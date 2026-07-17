"""Content and record hashing helpers (SPEC §V17).

Deterministic hashes for provenance: file content hashes for the snapshot
manifest, and canonical-JSON record hashes for per-record provenance.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_hex(data: bytes) -> str:
    """Hex SHA-256 of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    """Streaming hex SHA-256 of a file's bytes (§V4 build ``database_hash``)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(obj: Any) -> bytes:
    """Deterministic UTF-8 JSON encoding (sorted keys, tight separators)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def record_hash(obj: Any) -> str:
    """Stable SHA-256 over a record's canonical JSON (§V17 ``record_hash``)."""
    return sha256_hex(canonical_json(obj))
