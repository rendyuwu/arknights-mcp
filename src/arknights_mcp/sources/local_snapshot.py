"""Local snapshot source adapter (SPEC §V1, §V2 path safety; PRD 10.2, 17.4).

Reads a user-supplied snapshot directory for one region. Performs **no** network
I/O and refuses any path that escapes the snapshot root (path-traversal
prevention). Consumed only by CLI ``import`` (and the M0 accept test), never by
runtime MCP tools.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from arknights_mcp.sources.base import SourceAdapterError, json_within_limits


class LocalSnapshotAdapter:
    """Read-only file access rooted at a single region's snapshot directory."""

    #: This adapter never performs network I/O (§V1).
    touches_network: bool = False

    def __init__(self, root: str | Path, server: str, source_id: str = "local_snapshot") -> None:
        resolved = Path(root).resolve()
        if not resolved.is_dir():
            raise SourceAdapterError(f"snapshot root is not a directory: {resolved.name}")
        self.root: Path = resolved
        self.server: str = server
        self.source_id: str = source_id

    def _safe_path(self, relative_path: str) -> Path:
        """Resolve ``relative_path`` under the root, rejecting traversal/escape."""
        rel = Path(relative_path)
        if rel.is_absolute():
            raise SourceAdapterError(f"absolute paths are not allowed: {relative_path!r}")
        candidate = (self.root / rel).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise SourceAdapterError(f"path escapes snapshot root: {relative_path!r}")
        return candidate

    def exists(self, relative_path: str) -> bool:
        try:
            return self._safe_path(relative_path).is_file()
        except SourceAdapterError:
            return False

    def read_bytes(self, relative_path: str) -> bytes:
        path = self._safe_path(relative_path)
        if not path.is_file():
            raise SourceAdapterError(f"file not found in snapshot: {relative_path!r}")
        return path.read_bytes()

    def read_json(self, relative_path: str) -> Any:
        raw = self.read_bytes(relative_path)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except RecursionError as exc:
            # Pathologically deep JSON blows the parser's stack before the depth cap
            # can reject it; surface a graceful capped error rather than an uncaught
            # traceback out of `arknights-mcp import` (B5, matching the network stager).
            raise SourceAdapterError(f"JSON exceeds safe nesting depth: {relative_path!r}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourceAdapterError(f"invalid JSON in {relative_path!r}: {exc}") from exc
        # Bound nesting depth + node count identically to the network path (§V37 home).
        json_within_limits(parsed)
        return parsed

    def iter_files(self) -> Iterator[str]:
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                yield path.relative_to(self.root).as_posix()
