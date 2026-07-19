"""Test-only helpers (not shipped in the wheel).

Holds in-memory / filesystem-backed fakes used by unit tests so they never live
in the production safety module (``arknights_mcp.sources.arknights_assets``), per
L13 of the T1-T27 review.

Also home to the one build-the-distribution helper shared by the packaging test
(§T47) and the release audit (§T49) -- so the ``python -m build`` invocation and
artifact-reading logic live in exactly one place (§V37).
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from arknights_mcp.sources.base import SourceAdapterError

#: Repo root (this file is ``<root>/tests/support/__init__.py``).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Policy / legal files a source release must carry (§V16 code-license scope,
#: §V27 registry mirror). Canonical list -- both ``test_policy_files`` (files on
#: disk) and ``test_release_audit`` (files inside the sdist) read it (§V37).
REQUIRED_POLICY_FILES: tuple[str, ...] = (
    "LICENSE",
    "NOTICE",
    "README.md",
    "DATA_SOURCES.md",
    "DATA_POLICY.md",
    "TAKEDOWN_POLICY.md",
    "PRIVACY.md",
    "SECURITY.md",
)

#: A minimal test fixture is a few KB; a raw upstream dump (``character_table``,
#: ``enemy_database``) is multiple MB. This cap cleanly separates the two so a
#: full-dump snapshot cannot ride into a release artifact as a "fixture" (§V16;
#: §T15 "no full dump"). Headroom over the largest real fixture (~6 KB).
MAX_DATA_JSON_BYTES = 64 * 1024


@dataclass(frozen=True)
class BuiltDistributions:
    """A wheel + sdist built once, with each artifact's file members and sizes.

    ``*_sizes`` map an artifact-relative member path to its uncompressed byte
    size (directories excluded). Sdist paths are stripped of the
    ``<name>-<version>/`` top-level directory so they read repo-relative.
    """

    wheel: Path
    sdist: Path
    wheel_sizes: dict[str, int]
    sdist_sizes: dict[str, int]


def build_distributions(outdir: Path) -> BuiltDistributions:
    """Build the wheel + sdist offline into ``outdir`` and read their members.

    Offline + no build isolation (uses the locked dev-env hatchling) so it runs
    in the default gate with no network (§V16 fetch-free).
    """
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--sdist",
            "--no-isolation",
            "--outdir",
            str(outdir),
            str(REPO_ROOT),
        ],
        check=True,
        capture_output=True,
    )
    wheels = list(outdir.glob("arknights_mcp-*.whl"))
    sdists = list(outdir.glob("arknights_mcp-*.tar.gz"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    assert len(sdists) == 1, f"expected exactly one sdist, got {sdists}"
    return BuiltDistributions(
        wheel=wheels[0],
        sdist=sdists[0],
        wheel_sizes=_wheel_member_sizes(wheels[0]),
        sdist_sizes=_sdist_member_sizes(sdists[0]),
    )


def _wheel_member_sizes(wheel: Path) -> dict[str, int]:
    with zipfile.ZipFile(wheel) as zf:
        return {info.filename: info.file_size for info in zf.infolist() if not info.is_dir()}


def _sdist_member_sizes(sdist: Path) -> dict[str, int]:
    sizes: dict[str, int] = {}
    with tarfile.open(sdist, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            # Strip the leading ``arknights_mcp-<version>/`` so paths read
            # repo-relative (``LICENSE``, ``tests/fixtures/...``).
            _, _, rel = member.name.partition("/")
            sizes[rel or member.name] = member.size
    return sizes


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


__all__ = [
    "MAX_DATA_JSON_BYTES",
    "REPO_ROOT",
    "REQUIRED_POLICY_FILES",
    "BuiltDistributions",
    "DictFetcher",
    "build_distributions",
    "dict_fetcher_from_snapshot",
]
