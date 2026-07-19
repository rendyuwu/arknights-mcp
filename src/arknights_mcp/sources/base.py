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


#: Default JSON-safety caps every source adapter applies when parsing a file.
#: A single home (§V37) so the network stager and the local snapshot reader bound
#: nesting depth and node count identically (anti-abuse; B5, §V22/§V19-adjacent).
DEFAULT_MAX_JSON_DEPTH = 64
DEFAULT_MAX_JSON_NODES = 2_000_000


def json_within_limits(
    obj: Any,
    *,
    max_depth: int = DEFAULT_MAX_JSON_DEPTH,
    max_nodes: int = DEFAULT_MAX_JSON_NODES,
) -> None:
    """Bound parsed-JSON nesting depth + node count, else raise ``SourceAdapterError``.

    Shared by both adapters so a pathological document is refused identically
    whether it arrives over the network (:class:`ArknightsAssetsAdapter`) or from a
    local snapshot (:class:`LocalSnapshotAdapter`). ``walk`` raises the moment the
    depth cap is exceeded, so it never itself recurses past ``max_depth`` levels.
    """
    nodes = 0

    def walk(value: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            raise SourceAdapterError(f"JSON exceeds node cap ({max_nodes})")
        if depth > max_depth:
            raise SourceAdapterError(f"JSON exceeds depth cap ({max_depth})")
        if isinstance(value, dict):
            for item in value.values():
                walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

    walk(obj, 1)


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
