"""Stage drop-rate read repository (§V2; §T91).

Encapsulates the parameterized ``SELECT`` that backs the ``get_stage_drops``
service: every ``stage_drops`` row for a stage, joined to its ``items`` identity
and its penguin ``source_snapshots`` import time. A drop rate is a
penguin-sourced FACT with its OWN provenance chain, distinct from the
``arknights_assets`` game-data fact (§V54), so each row carries the penguin
``snapshot_id`` + ``fetched_at`` + ``expires_at`` (§V53) needed to serve a
stale-aware, attributed drop fact.

Rows are returned as flat, typed dataclasses that mirror the selected columns
1:1; the stale check (``now`` vs ``expires_at``) and the efficiency analysis stay
in the service. The two joins are on NOT NULL foreign keys
(``stage_drops -> items``, ``stage_drops -> source_snapshots``), so a drop row
always carries its item identity + penguin provenance (§V5/§V54). Every value is
bound (§V2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arknights_mcp.db.repositories.base import Repository


@dataclass(frozen=True)
class StageDropRow:
    """One aggregated ``(stage, item)`` drop fact plus its penguin provenance (§V54).

    ``drop_rate`` is penguin's expected quantity per run (``quantity / times``);
    ``times`` is the sampled-run count (the §V55 sample size). ``snapshot_id`` +
    ``fetched_at`` + ``expires_at`` are the penguin cache stamps (§V53); ``region``
    is carried explicitly so a drop fact stands alone (§V5). A ``None`` numeric
    field means the datum was absent, never a fabricated zero (§V26).
    """

    item_game_id: str
    item_display_name: str | None
    item_rarity: str | None
    item_type: str | None
    region: str
    quantity: int | None
    times: int | None
    drop_rate: float | None
    snapshot_id: str
    fetched_at: str
    expires_at: str
    imported_at: str


# A drop is a penguin FACT with its own provenance chain (§V54): join to the item
# identity and to the penguin source_snapshots row for the import time. Ordered by
# the item game_id so the payload is deterministic (§V26) and reproducible.
_DROPS_BY_STAGE_SQL = (
    "SELECT i.game_id, i.display_name, i.rarity, i.item_type, "
    "d.region, d.quantity, d.times, d.drop_rate, "
    "d.snapshot_id, d.fetched_at, d.expires_at, ss.imported_at "
    "FROM stage_drops d "
    "JOIN items i ON i.item_pk = d.item_pk "
    "JOIN source_snapshots ss ON ss.snapshot_id = d.snapshot_id "
    "WHERE d.stage_pk = ? "
    "ORDER BY i.game_id"
)


def _to_stage_drop_row(row: Any) -> StageDropRow:
    (
        item_game_id,
        item_display_name,
        item_rarity,
        item_type,
        region,
        quantity,
        times,
        drop_rate,
        snapshot_id,
        fetched_at,
        expires_at,
        imported_at,
    ) = row
    return StageDropRow(
        item_game_id=item_game_id,
        item_display_name=item_display_name,
        item_rarity=item_rarity,
        item_type=item_type,
        region=region,
        quantity=quantity,
        times=times,
        drop_rate=drop_rate,
        snapshot_id=snapshot_id,
        fetched_at=fetched_at,
        expires_at=expires_at,
        imported_at=imported_at,
    )


class DropRepository(Repository):
    """Read-only access to a stage's penguin drop-rate cache (§V2)."""

    def drops_for_stage(self, stage_pk: int) -> list[StageDropRow]:
        """Every drop fact for the stage, ordered by item ``game_id`` (§V26)."""
        return [_to_stage_drop_row(r) for r in self._all(_DROPS_BY_STAGE_SQL, (stage_pk,))]
