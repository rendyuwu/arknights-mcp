"""Source adapter contract (SPEC §V1).

A source adapter is the *only* boundary through which the import pipeline reads
raw snapshot files. Adapters are used exclusively by CLI ``sync``/``import``
jobs; runtime MCP tools never touch an adapter (§V1). Each adapter is scoped to
one region (``server``) and exposes read-only access to files under a fixed
root, with path-traversal prevention.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable


class SourceAdapterError(Exception):
    """Raised for unsafe paths, missing files, or malformed snapshot content."""


@runtime_checkable
class SourceAdapter(Protocol):
    """Read-only access to a single region's snapshot files.

    ``touches_network`` documents whether an adapter may perform network I/O.
    Local adapters are always ``False``; a network sync adapter (T21) sets it
    ``True`` and is still only ever invoked from CLI sync jobs, never at query
    time (§V1).
    """

    source_id: str
    server: str
    touches_network: bool

    def exists(self, relative_path: str) -> bool:
        """Whether ``relative_path`` resolves to a readable file within the root."""
        ...

    def read_bytes(self, relative_path: str) -> bytes:
        """Raw bytes of a file within the root (for hashing/manifest)."""
        ...

    def read_json(self, relative_path: str) -> Any:
        """Parsed JSON of a file within the root."""
        ...

    def iter_files(self) -> Iterator[str]:
        """Yield POSIX-style relative paths of every file under the root."""
        ...
