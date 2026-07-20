"""T69: CI-only ``kengxxiao_gamedata`` CN cross-validator (§V29, §V30, §C).

T68 verified the inferred field mappings against *one* live upstream
(``arknights_assets_gamedata`` EN). This closes the remaining gap: it checks the
same mapped stats against a **second, independent** CN dump
(``kengxxiao_gamedata``), so a value we read is confirmed by two projects, not one
source's serialization quirk. For every enemy present in both pinned CN snapshots
it asserts the mapped ``maxHp``/``baseAttackTime``/``massLevel``/``motion`` agree
(§V29).

Kengxxiao's CN ``enemy_database.json`` is a ``{"enemies": [{"Key", "Value"}]}`` KV
list, not the id-keyed dict ``arknights_assets`` uses, so it goes through its own
bridge (``normalize_kengxxiao_enemy_database``, §V30) before comparison.

§C posture: kengxxiao is CI-only, never a runtime dependency, never overrides the
primary source (this test only *reads* and compares), and nothing fetched is
committed — both snapshots live under pytest's ``tmp_path`` (outside the repo
tree) and are discarded when the test ends (§V16, fetch → compare → discard).

CI-only: gated behind ``ARKMCP_KENGXXIAO_XVAL`` (set by the dedicated CI job), so
the default offline ``pytest -q`` skips the whole module.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from arknights_mcp.sources.http_fetch import HttpsFetcher
from arknights_mcp.sources.kengxxiao_validator import cross_check_raw_enemy_databases

_XVAL_ENV = os.environ.get("ARKMCP_KENGXXIAO_XVAL", "")
pytestmark = pytest.mark.skipif(
    _XVAL_ENV in ("", "0", "false", "False"),
    reason="CN cross-validator (needs network); set ARKMCP_KENGXXIAO_XVAL=1 (CI only)",
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Primary CN source, pinned to the same commit T68/B6 verified the real shape
#: against (its ``cn/`` region counterpart of the EN file T68 imports).
ARKNIGHTS_ASSETS_COMMIT = "413a81a3ff3e968089b1d6d302473f7b38c36dda"
PRIMARY_CN_URL = (
    "https://raw.githubusercontent.com/ArknightsAssets/ArknightsGamedata/"
    f"{ARKNIGHTS_ASSETS_COMMIT}/cn/gamedata/levels/enemydata/enemy_database.json"
)

#: kengxxiao CN validator, pinned per §T69. If raw.githubusercontent rejects the
#: abbreviated SHA, expand it to the full 40-char commit; the pin is what keeps the
#: assertion deterministic.
KENGXXIAO_CN_COMMIT = "6b6ac60f"
KENGXXIAO_CN_URL = (
    "https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/"
    f"{KENGXXIAO_CN_COMMIT}/zh_CN/gamedata/levels/enemydata/enemy_database.json"
)

#: These enemy DBs are a few MiB; the cap clears them with headroom.
MAX_FILE_BYTES = 64 * 1024 * 1024

#: Sanity floor: the two CN snapshots must genuinely overlap, else a broken URL or
#: shape would trivially "pass" with an empty comparison.
MIN_SHARED_ENEMIES = 50
#: The two projects are independently versioned (different pinned commits), so a
#: handful of rebalanced enemies may legitimately drift; require the overwhelming
#: majority of compared cells to agree rather than exact equality.
MIN_AGREEMENT = 0.80


def _fetch_json(url: str, dest: Path) -> Any:
    """Fetch ``url`` into ``dest`` (a tmp file) via the production HTTPS fetcher, load it.

    Writing under the tmp dir (never the repo) makes the §V16 fetch → discard
    posture explicit and asserts the raw snapshot cannot leak into the tree.
    """
    fetcher = HttpsFetcher()
    data = fetcher.fetch(url, max_bytes=MAX_FILE_BYTES)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    assert REPO_ROOT not in dest.resolve().parents  # §V16: outside the repo tree
    return json.loads(data)


def test_cn_sources_agree_on_shared_enemy_stats(tmp_path: Path) -> None:
    """Primary CN vs kengxxiao CN → shared enemy stats agree (§V29, §V30)."""
    primary_raw = _fetch_json(PRIMARY_CN_URL, tmp_path / "primary" / "enemy_database.json")
    kengxxiao_raw = _fetch_json(KENGXXIAO_CN_URL, tmp_path / "kengxxiao" / "enemy_database.json")

    report = cross_check_raw_enemy_databases(primary_raw, kengxxiao_raw)

    assert report.compared_enemies >= MIN_SHARED_ENEMIES, (
        f"too few shared CN enemies ({report.compared_enemies}); check the pinned URLs / shapes"
    )
    assert report.compared_cells > 0, "no comparable stat cells across the two CN sources"
    assert report.agreement_rate >= MIN_AGREEMENT, report.describe()
