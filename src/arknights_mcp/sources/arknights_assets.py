"""Primary allowlisted source adapter: ``arknights_assets_gamedata`` (§T21; §V1).

This is a network-touching adapter used **exclusively** by the CLI ``sync`` job
(never at query time, §V1): it downloads a fixed allowlist of gameplay JSON files
over HTTPS into an isolated staging directory, enforcing size, JSON-depth,
record-count, and redirect limits (PRD §11.2), and then hands the import pipeline
an ordinary local, read-only :class:`LocalSnapshotAdapter` rooted at that staging
directory. The network concern is therefore fully contained here; everything
downstream sees only local files.

The shared HTTPS transport + safety caps live in :mod:`arknights_mcp.sources.http_fetch`
(§V37: one home) so this adapter and the ``penguin_statistics`` adapter apply identical
limits. The HTTP transport is injected (:class:`Fetcher`) so the caps are unit-testable
without live network access; the default :class:`HttpsFetcher` refuses non-HTTPS URLs
and caps redirects and response size.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from arknights_mcp.importers.normalization import is_clean_level_path, normalize_level_id
from arknights_mcp.sources.base import SourceAdapterError, SourceNotFoundError
from arknights_mcp.sources.http_fetch import (
    DEFAULT_LIMITS,
    DownloadBudget,
    DownloadLimits,
    Fetcher,
    HttpsFetcher,
    fetch_json,
)
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

_LOG = logging.getLogger(__name__)

#: Default source id for this adapter (matches the registry entry).
DEFAULT_SOURCE_ID = "arknights_assets_gamedata"

#: Relative paths only under these prefixes may be fetched (allowlist, §V18).
ALLOWED_PREFIXES: tuple[str, ...] = ("gamedata/excel/", "gamedata/levels/")

#: Discovered stage level files may only live under these prefixes -- narrower
#: than ``ALLOWED_PREFIXES`` so a crafted ``stage_table.levelId`` cannot enqueue an
#: arbitrary excel table as a "level" file (L8).
LEVEL_PREFIXES: tuple[str, ...] = ("gamedata/levels/",)


#: The fixed core files fetched every sync (enemy + stage/zone tables). These are
#: mandatory: a missing one fails the whole sync (a bad ``base_url`` must not
#: silently produce an empty build). The §V30 silent-empty guard is scoped to this
#: combat data.
CORE_FILES: tuple[str, ...] = (
    "gamedata/excel/enemy_handbook_table.json",
    "gamedata/levels/enemydata/enemy_database.json",
    "gamedata/excel/zone_table.json",
    "gamedata/excel/stage_table.json",
)

#: The stage table is fetched + parsed serially before level discovery fans out
#: (§V42 ordering): the discovered level paths come from it.
STAGE_TABLE_PATH = "gamedata/excel/stage_table.json"

#: Operator/module excel tables the pipeline importers read (``import_operators`` →
#: ``character_table``/``skill_table``; ``import_modules`` → ``uniequip_table``/
#: ``battle_equip_table``). In-scope for v0.1 (PRD §6.1) but *optional per snapshot*:
#: a combat-only snapshot legitimately lacks them and the pipeline imports the
#: domain empty (``operators.py`` / ``modules.py`` return empty if the table is
#: absent). They are therefore fetched every sync but tolerated-if-absent (404/410
#: skip+warn, B34 precedent) rather than added to the strict :data:`CORE_FILES` set,
#: so a combat-only snapshot still syncs. They MUST be attempted, though — else a
#: real ``sync`` silently builds ``operators=modules=0`` and ``get_operator`` /
#: ``compare_operator_modules`` return empty (B36; §V41). ``uniequip_data`` is read
#: from ``uniequip_table``'s ``equipDict`` and is not a separate file.
#:
#: ``gacha_table`` (§T111/§V62) is the same class: the banner-archive importer reads
#: its ``gachaPoolClient`` from the SAME snapshot as the combat data, but a banner
#: is a standalone FACT and a combat-only snapshot legitimately lacks the table, so
#: it too is fetched every sync yet tolerated-if-absent (404/410 skip+warn) rather
#: than a mandatory :data:`CORE_FILES` entry (§V62; the §V41 introspection test still
#: asserts the banner importer's default path is in this staged set).
SUPPLEMENTARY_FILES: tuple[str, ...] = (
    "gamedata/excel/character_table.json",
    "gamedata/excel/skill_table.json",
    "gamedata/excel/uniequip_table.json",
    "gamedata/excel/battle_equip_table.json",
    "gamedata/excel/gacha_table.json",
)


def _validate_relative_path(
    relative_path: str, *, allowed_prefixes: tuple[str, ...] = ALLOWED_PREFIXES
) -> str:
    """Reject absolute paths, traversal, and paths outside the allowlist (§V18).

    Both the literal path and its percent-decoded form are checked: the remote
    server decodes ``%2f`` back to ``/``, so ``a/..%2f..%2fsecret`` would escape the
    allowlist on the wire even though its literal ``.`` parts look clean (L7).
    """
    decoded = unquote(relative_path)
    for candidate in (relative_path, decoded):
        if candidate.startswith("/") or (len(candidate) > 1 and candidate[1] == ":"):
            raise SourceAdapterError(f"absolute paths are not allowed: {relative_path!r}")
        if any(part == ".." for part in PurePosixPath(candidate).parts):
            raise SourceAdapterError(f"path traversal is not allowed: {relative_path!r}")
        normalized_candidate = PurePosixPath(candidate).as_posix()
        if not any(normalized_candidate.startswith(prefix) for prefix in allowed_prefixes):
            raise SourceAdapterError(f"path not in allowlist: {relative_path!r}")
    return PurePosixPath(relative_path).as_posix()


class ArknightsAssetsAdapter:
    """Network stager for the primary source; produces a local snapshot (§V1)."""

    #: This adapter performs network I/O; it is only ever run from CLI sync (§V1).
    touches_network: bool = True

    def __init__(
        self,
        base_url: str,
        server: str,
        *,
        fetcher: Fetcher | None = None,
        limits: DownloadLimits = DEFAULT_LIMITS,
        source_id: str = DEFAULT_SOURCE_ID,
        budget: DownloadBudget | None = None,
        max_parallel: int = 8,
    ) -> None:
        cleaned = base_url.strip()
        if not cleaned.lower().startswith("https://"):
            raise SourceAdapterError(f"sync base_url must be https://, got {base_url!r}")
        if max_parallel < 1:
            raise SourceAdapterError(f"max_parallel must be >= 1, got {max_parallel}")
        self.base_url: str = cleaned.rstrip("/")
        self.server: str = server
        self.source_id: str = source_id
        self._fetcher: Fetcher = (
            fetcher if fetcher is not None else HttpsFetcher(max_redirects=limits.max_redirects)
        )
        self._limits = limits
        # Bounds the download thread pool (§T79/§V42): 1 forces the serial fallback;
        # never an unbounded fan-out (2257 refs ⊥ 2257 sockets).
        self._max_parallel = max_parallel
        # A per-run budget may be injected so a multi-server sync shares one cap;
        # standalone use falls back to a per-adapter budget from ``limits``.
        self._budget = budget if budget is not None else DownloadBudget(limits.max_total_bytes)

    def _download(self, relative_path: str, staging_root: Path) -> Any:
        """Fetch one allowlisted file into staging (enforcing caps); return parsed JSON."""
        normalized = _validate_relative_path(relative_path)
        url = f"{self.base_url}/{normalized}"
        # The shared fetch-and-cap sequence (budget, per-file size cap, depth/node
        # caps) lives in one home (§V37); the raw bytes are staged for hashing.
        data, parsed = fetch_json(self._fetcher, url, limits=self._limits, budget=self._budget)
        target = staging_root / normalized
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return parsed

    def _discover_level_paths(self, stage_table: Any) -> list[str]:
        """Collect allowlisted level-file paths referenced by the stage table.

        The real ``levelId`` is a Title-case, extension-less reference
        (``Obt/Main/level_main_04-04``); it is rewritten to the actual snapshot
        path before the allowlist check (§V29/§V30) so the level file is actually
        fetched. ``normalize_level_id`` always forces the result under
        ``gamedata/levels/``, so a crafted ``levelId`` still cannot enqueue an excel
        table (L8), and traversal is rejected by ``_validate_relative_path`` at
        download time.
        """
        paths: list[str] = []
        if not isinstance(stage_table, dict):
            return paths
        stages = stage_table.get("stages", {})
        if not isinstance(stages, dict):
            return paths
        seen: set[str] = set()
        for entry in stages.values():
            if not isinstance(entry, dict):
                continue
            level_id = entry.get("levelId")
            if not isinstance(level_id, str):
                continue
            resolved = normalize_level_id(level_id)
            if resolved is None or resolved in seen:
                continue
            seen.add(resolved)
            # Confine discovery to the levels tree post-normalization (§V36): a
            # crafted levelId must not fetch an excel table or escape the tree.
            if is_clean_level_path(resolved):
                paths.append(resolved)
        return sorted(paths)

    def _fetch_one(self, path: str, staging_root: Path, *, strict: bool) -> int:
        """Download one file; return 1 if optional-and-absent upstream, else 0.

        ``strict`` files (core tables) re-raise a :class:`SourceNotFoundError` so a
        bad ``base_url`` fails closed; optional files (supplementary tables, pruned
        level refs) count the 404/410 as a skip. Any other transport failure always
        propagates.
        """
        try:
            self._download(path, staging_root)
            return 0
        except SourceNotFoundError:
            if strict:
                raise
            return 1

    def _download_all(self, paths: Iterable[str], staging_root: Path, *, strict: bool) -> int:
        """Download every path with bounded parallelism; return the missing count.

        Each file is fetched independently: every §V1 gate (allowlist, per-file byte
        cap, JSON depth/node cap) runs per file inside ``_download`` regardless of the
        worker, the shared :class:`DownloadBudget` charge is thread-safe, and each file
        is written to its own normalized path so the staged output does not depend on
        completion order (§V42). ``max_parallel == 1`` runs the serial path (identical
        to the old behavior); the returned missing count is the exact sum across
        workers, and a strict fetch failure re-raises out of the pool (fail-closed).
        """
        items = list(paths)
        if not items:
            return 0
        workers = min(self._max_parallel, len(items))
        if workers <= 1:
            return sum(self._fetch_one(p, staging_root, strict=strict) for p in items)
        missing = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="arkmcp-sync") as pool:
            futures = [pool.submit(self._fetch_one, p, staging_root, strict=strict) for p in items]
            try:
                for future in as_completed(futures):
                    missing += future.result()  # re-raises a strict fetch failure, fail-closed
            except BaseException:
                # A fetch failed (strict 404, cap trip, transport fault): cancel every
                # not-yet-started download so a doomed run stops fanning out work.
                for f in futures:
                    f.cancel()
                raise
        return missing

    def stage(self, staging_root: str | Path) -> LocalSnapshotAdapter:
        """Download the allowlisted snapshot into ``staging_root`` and wrap it.

        Fetches the core files, the operator/module tables, and each stage's level
        file (discovered from the stage table), then returns a local adapter over the
        staging directory. All downloads obey the configured :class:`DownloadLimits`.

        Core files are mandatory: a missing one fails the whole sync (a bad
        ``base_url`` must not silently produce an empty build). Both the
        operator/module tables and the discovered level files are fetched but
        tolerated-if-absent (404/410 skip+warn): a discovered ``levelId`` may point at
        a retired event whose data is pruned (B34), and a combat-only snapshot may
        lack the operator/module tables entirely (B36) — the pipeline imports those
        domains empty (optional per snapshot). Both must still be *attempted*, else a
        real sync silently omits an in-scope domain (§V41). The §V30 gate still fails
        closed if 0 levels import overall.
        """
        root = Path(staging_root)
        root.mkdir(parents=True, exist_ok=True)
        # The stage table is fetched + parsed serially FIRST: level discovery fans
        # out from it, so it must be in hand before the pool starts (§V42 ordering).
        stage_table: Any = self._download(STAGE_TABLE_PATH, root)
        remaining_core = [f for f in CORE_FILES if f != STAGE_TABLE_PATH]
        self._download_all(remaining_core, root, strict=True)
        supp_missing = self._download_all(SUPPLEMENTARY_FILES, root, strict=False)
        if supp_missing:
            _LOG.warning(
                "sync %s: %d of %d operator/module table(s) were absent upstream "
                "(404/410) and were skipped; their domains import empty",
                self.server,
                supp_missing,
                len(SUPPLEMENTARY_FILES),
            )
        level_paths = self._discover_level_paths(stage_table)
        missing = self._download_all(level_paths, root, strict=False)
        if missing:
            _LOG.warning(
                "sync %s: %d of %d referenced level files were absent upstream "
                "(404/410) and were skipped; their stages import with no map/waves",
                self.server,
                missing,
                len(level_paths),
            )
        return LocalSnapshotAdapter(root, self.server, source_id=self.source_id)
