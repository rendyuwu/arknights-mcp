"""Test-only helpers (not shipped in the wheel).

Holds in-memory / filesystem-backed fakes used by unit tests so they never live
in the production safety module (``arknights_mcp.sources.arknights_assets``), per
L13 of the T1-T27 review.
"""

from __future__ import annotations

from pathlib import Path

from arknights_mcp.sources.base import SourceAdapterError


class DictFetcher:
    """In-memory ``Fetcher`` backed by a ``{url: bytes}`` map (tests only)."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = dict(files)

    def fetch(self, url: str, *, max_bytes: int) -> bytes:
        if url not in self._files:
            raise SourceAdapterError(f"not found: {url!r}")
        data = self._files[url]
        if len(data) > max_bytes:
            raise SourceAdapterError(f"download exceeds per-file cap ({max_bytes} bytes): {url!r}")
        return data


def dict_fetcher_from_snapshot(base_url: str, root: str | Path) -> DictFetcher:
    """Build a :class:`DictFetcher` mapping ``base_url``/<rel> to a local tree's bytes."""
    base = base_url.rstrip("/")
    root_path = Path(root)
    files: dict[str, bytes] = {}
    for path in root_path.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root_path).as_posix()
            files[f"{base}/{rel}"] = path.read_bytes()
    return DictFetcher(files)


__all__ = ["DictFetcher", "dict_fetcher_from_snapshot"]
