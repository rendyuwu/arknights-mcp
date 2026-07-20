"""Stage drop-rate read repository (§V2; §T91/§T103).

Encapsulates the parameterized ``SELECT``s that back the drop services:

* :meth:`DropRepository.drops_for_stage` (§T91) -- every ``stage_drops`` row for a
  stage, joined to its ``items`` identity and its penguin ``source_snapshots`` import
  time (the ``get_stage_drops`` view);
* :meth:`DropRepository.item_by_game_id` + :meth:`DropRepository.drops_for_item`
  (§T103) -- the REVERSE lookup: resolve an item by ``(server, game_id)``, then every
  stage that drops it joined to the stage's ``sanity_cost`` + ``stage_code`` + region +
  provenance (the ``get_item_drops`` view, §V60). This is a new READ only -- no new
  import or migration -- riding the ``idx_stage_drops_item`` index 0009 built for it.

A drop rate is a penguin-sourced FACT with its OWN provenance chain, distinct from
the ``arknights_assets`` game-data fact (§V54), so each row carries the penguin
``snapshot_id`` + ``fetched_at`` + ``expires_at`` (§V53) needed to serve a
stale-aware, attributed drop fact.

Rows are returned as flat, typed dataclasses that mirror the selected columns
1:1; the stale check (``now`` vs ``expires_at``) and the efficiency analysis stay
in the service. The joins are on NOT NULL foreign keys
(``stage_drops -> items``, ``stage_drops -> stages``, ``stage_drops ->
source_snapshots``), so a drop row always carries its item/stage identity + penguin
provenance (§V5/§V54). Every value is bound (§V2).
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


@dataclass(frozen=True)
class ItemRow:
    """One item's identity for the reverse item->stage lookup (§T103).

    Resolves ``(server, game_id)`` to the internal ``item_pk`` that keys
    :meth:`DropRepository.drops_for_item`. ``region`` is carried so the comparison is
    region-attributed (§V5); the item is resolved PER region, so an en item and a cn
    item of the same ``game_id`` are distinct rows.
    """

    item_pk: int
    server: str
    game_id: str
    display_name: str | None
    rarity: str | None
    item_type: str | None


@dataclass(frozen=True)
class ItemStageDropRow:
    """One stage's drop of a fixed item plus the stage facts + penguin provenance (§V60).

    The reverse of :class:`StageDropRow`: there the item varies for a fixed stage;
    here the stage varies for a fixed item. Carries the stage's ``sanity_cost`` +
    ``stage_code`` (the efficiency inputs, §V55) and the penguin ``snapshot_id`` +
    ``fetched_at`` + ``expires_at`` provenance chain (§V53/§V54). ``region`` is the
    drop's own region (§V5). A ``None`` numeric field means the datum was absent, never
    a fabricated zero (§V26).
    """

    stage_game_id: str
    stage_code: str | None
    sanity_cost: int | None
    region: str
    quantity: int | None
    times: int | None
    drop_rate: float | None
    snapshot_id: str
    fetched_at: str
    expires_at: str
    imported_at: str


# Resolve an item by its region + game_id to the internal item_pk (§T103). The
# UNIQUE(server, game_id) index (0009) serves this lookup directly.
_ITEM_BY_GAME_ID_SQL = (
    "SELECT item_pk, server, game_id, display_name, rarity, item_type "
    "FROM items WHERE server = ? AND game_id = ?"
)

# The reverse item->stage comparison (§V60): every stage that drops the item, joined
# to the stage's sanity_cost + stage_code (efficiency inputs) and the penguin
# source_snapshots import time. Region-scoped on the STAGE so an item's comparison
# never mixes en + cn stages (§V5). Rides idx_stage_drops_item (0009). Ordered by
# stage_code then game_id so the payload is deterministic (§V26); the service ranks
# by sanity-per-item (§V60).
_DROPS_BY_ITEM_SQL = (
    "SELECT s.game_id, s.stage_code, s.sanity_cost, "
    "d.region, d.quantity, d.times, d.drop_rate, "
    "d.snapshot_id, d.fetched_at, d.expires_at, ss.imported_at "
    "FROM stage_drops d "
    "JOIN stages s ON s.stage_pk = d.stage_pk "
    "JOIN source_snapshots ss ON ss.snapshot_id = d.snapshot_id "
    "WHERE d.item_pk = ? AND s.server = ? "
    "ORDER BY s.stage_code, s.game_id"
)


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


def _to_item_row(row: Any) -> ItemRow:
    item_pk, server, game_id, display_name, rarity, item_type = row
    return ItemRow(
        item_pk=item_pk,
        server=server,
        game_id=game_id,
        display_name=display_name,
        rarity=rarity,
        item_type=item_type,
    )


def _to_item_stage_drop_row(row: Any) -> ItemStageDropRow:
    (
        stage_game_id,
        stage_code,
        sanity_cost,
        region,
        quantity,
        times,
        drop_rate,
        snapshot_id,
        fetched_at,
        expires_at,
        imported_at,
    ) = row
    return ItemStageDropRow(
        stage_game_id=stage_game_id,
        stage_code=stage_code,
        sanity_cost=sanity_cost,
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
    """Read-only access to the penguin drop-rate cache, both directions (§V2)."""

    def drops_for_stage(self, stage_pk: int) -> list[StageDropRow]:
        """Every drop fact for the stage, ordered by item ``game_id`` (§V26)."""
        return [_to_stage_drop_row(r) for r in self._all(_DROPS_BY_STAGE_SQL, (stage_pk,))]

    def item_by_game_id(self, server: str, game_id: str) -> ItemRow | None:
        """Resolve an item by ``(server, game_id)`` -- the unique key -- or ``None`` (§T103)."""
        row = self._one(_ITEM_BY_GAME_ID_SQL, (server, game_id))
        return _to_item_row(row) if row is not None else None

    def drops_for_item(self, item_pk: int, server: str) -> list[ItemStageDropRow]:
        """Every stage that drops the item in ``server``, with the stage's facts +
        penguin provenance (§V60). Region-scoped so an item's comparison never mixes
        en + cn stages (§V5); ordered by ``stage_code`` then ``game_id`` (§V26)."""
        return [
            _to_item_stage_drop_row(r) for r in self._all(_DROPS_BY_ITEM_SQL, (item_pk, server))
        ]
