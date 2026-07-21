"""Banner-archive read repository (§V2; §T114).

Encapsulates the parameterized ``SELECT`` that backs :func:`get_banners`: every
``banners`` row for a region, LEFT-joined to its typed ``banner_featured_ops`` (and,
per featured op, LEFT-joined to ``operators`` for a resolved operator name) and to its
``record_provenance`` -> ``source_snapshots`` chain so each banner carries its OWN
provenance (§V5/§V17). An optional ``since``/``until`` window narrows the set by the
banner's ISO ``open_time`` at the SQL layer (parameterized, §V2).

The scope is METADATA-ONLY (§V62, extends §V16/§V56): the row shape is exactly the
schedule/identity columns the 0013 schema can hold (``game_id``/``display_name``/
``open_time``/``end_time``/``rule_type``) plus the typed featured-op ids -- there is no
gacha summary/detail/html/image column to select, so a prose leak is impossible at this
layer. ``region`` is carried explicitly so a banner stands alone (§V5); en and cn are
never mixed (the WHERE gates on the requested region).

Rows are returned flat (one row per featured op, or one row with NULL op fields for a
banner with no typed featured-op via the LEFT JOIN); the service groups them into
banners, pages, and derives provenance. Every value is bound (§V2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arknights_mcp.db.repositories.base import Repository


@dataclass(frozen=True)
class BannerFeaturedOpRow:
    """One typed featured operator on a banner (§V62), or a sentinel for none.

    A banner with no typed featured-op (a NORMAL/SINGLE/DOUBLE/LINKAGE standard banner)
    yields a single row whose ``char_id`` is ``None`` (the LEFT JOIN produced no featured
    op). ``operator_name`` is the SOFT-resolved operator's display name when the featured
    char id matched an operator present in the same snapshot (``resolved = 1``), else
    ``None`` with the raw ``char_id`` surfaced (§V62 -- an unresolvable featured-op is
    never fabricated).
    """

    char_id: str | None
    resolved: int | None
    operator_name: str | None


@dataclass(frozen=True)
class BannerRow:
    """One ``banners`` row + one featured-op leaf + the banner's provenance stamps.

    ``display_name``/``open_time``/``end_time``/``rule_type`` are nullable metadata (a
    raw pool entry may omit any, §V62). ``snapshot_id`` + ``imported_at`` are the
    banner's own provenance chain (§V5/§V17). There is deliberately no gacha prose field
    -- the 0013 schema cannot hold one (§V16/§V62). The featured-op columns are flattened
    onto the row by the LEFT JOIN; the service regroups the leaves per banner.
    """

    banner_pk: int
    game_id: str
    display_name: str | None
    open_time: str | None
    end_time: str | None
    rule_type: str | None
    region: str
    snapshot_id: str
    imported_at: str
    featured_op: BannerFeaturedOpRow


# Every banner for a region, LEFT-joined to its typed featured ops (+ the resolved
# operator name) and its own provenance chain (§V5/§V17). The since/until window is
# applied at the SQL layer via the "(? IS NULL OR b.open_time >= ?)" idiom: a NULL bound
# leaves that side open; a set bound narrows by the stored ISO open_time string
# (lexicographic compare is date-order-correct for ISO-8601). A banner with a NULL
# open_time is excluded once EITHER bound is set (it cannot be placed in the window), but
# kept when the window is fully open. The LEFT JOIN to banner_featured_ops keeps a
# standard banner (no featured op) in the result with NULL op columns; the further LEFT
# JOIN to operators resolves the featured op's display name when it is present. Ordered
# by open_time DESC (newest first), then game_id, then char_id so every banner's leaves
# are contiguous + the payload is deterministic (§V26); NULL open_time sorts last under
# DESC.
_BANNERS_SQL = (
    "SELECT b.banner_pk, b.game_id, b.display_name, b.open_time, b.end_time, "
    "b.rule_type, b.region, p.snapshot_id, ss.imported_at, "
    "f.char_id, f.resolved, o.display_name "
    "FROM banners b "
    "JOIN record_provenance p ON p.provenance_id = b.provenance_id "
    "JOIN source_snapshots ss ON ss.snapshot_id = p.snapshot_id "
    "LEFT JOIN banner_featured_ops f ON f.banner_pk = b.banner_pk "
    "LEFT JOIN operators o ON o.operator_pk = f.operator_pk "
    "WHERE b.region = ? "
    "AND (? IS NULL OR (b.open_time IS NOT NULL AND b.open_time >= ?)) "
    "AND (? IS NULL OR (b.open_time IS NOT NULL AND b.open_time <= ?)) "
    "ORDER BY b.open_time DESC, b.game_id, f.char_id"
)


def _to_banner_row(row: Any) -> BannerRow:
    (
        banner_pk,
        game_id,
        display_name,
        open_time,
        end_time,
        rule_type,
        region,
        snapshot_id,
        imported_at,
        char_id,
        resolved,
        operator_name,
    ) = row
    return BannerRow(
        banner_pk=banner_pk,
        game_id=game_id,
        display_name=display_name,
        open_time=open_time,
        end_time=end_time,
        rule_type=rule_type,
        region=region,
        snapshot_id=snapshot_id,
        imported_at=imported_at,
        featured_op=BannerFeaturedOpRow(
            char_id=char_id,
            resolved=resolved,
            operator_name=operator_name,
        ),
    )


class BannerRepository(Repository):
    """Read-only access to the banner archive (§V2)."""

    def banners_for_region(
        self, region: str, *, since: str | None = None, until: str | None = None
    ) -> list[BannerRow]:
        """Every banner for ``region`` within the optional ``since``/``until``
        open-time window, newest first, one row per featured-op leaf (§V26). Region-
        scoped so en/cn are never mixed (§V5); both bounds parameterized (§V2)."""
        return [
            _to_banner_row(r) for r in self._all(_BANNERS_SQL, (region, since, since, until, until))
        ]
