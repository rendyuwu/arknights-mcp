"""T198/§V99 (B133): item rarity is an int on the same 1-indexed base as operator rarity.

One wire key carried two JSON types AND two numeric bases: ``operators.rarity`` is an
INTEGER 1..6 (the in-game star count) while ``items.rarity`` was TEXT ``"0".."4"`` (a
0-indexed tier). Neither was documented, so a client could not tell a 1-star operator from
a T2 material by the field alone. Both now emit the number a player sees -- an int,
1-indexed -- which is what makes §V99's "same JSON type AND same numeric base, else rename
one" hold without renaming either key.

The conversion is a read-side bridge, so the STORED value is untouched (§V92: no rebuild
owed). That is exactly why this guard is here: nothing in the schema pins the mapping, so
if the bridge is dropped or its offset changes, only a test that reads both sides notices.

Counted against the real corpus rather than assumed (§V29/§V96): the pinned anchors below
are transcribed from the promoted build, not invented. B107 is the precedent for why --
a fixture that seeds the value it then asserts proves only that the fixture was written.
"""

from __future__ import annotations

import sqlite3

import pytest
from tests.support.tool_calls import active_build

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.mcp.tools.drops import build_get_item_drops_spec, build_get_stage_drops_spec
from arknights_mcp.services.drops import _item_rarity_tier

BUILD = active_build()

#: Transcribed from the promoted build (``items`` where server='en'), not invented. Each is
#: an item whose in-game tier is unambiguous, spanning the whole stored 0..4 domain so the
#: offset cannot be right at one end and wrong at the other.
_PINNED_TIERS: tuple[tuple[str, str, int], ...] = (
    ("30011", "Orirock", 1),
    ("30012", "Orirock Cube", 2),
    ("30013", "Orirock Cluster", 3),
    ("30014", "Orirock Concentration", 4),
    ("30135", "D32 Steel", 5),
)

#: The stored domain, counted on the promoted build: 0..4 and nothing else. Pinned so an
#: upstream widening (a T6 tier) fails LOUDLY here rather than silently emitting a 6 the
#: descriptions do not document.
_STORED_DOMAIN = {"0", "1", "2", "3", "4"}


@pytest.fixture
def conn() -> sqlite3.Connection:
    assert BUILD is not None
    return open_read_only(BUILD)


def test_the_bridge_is_the_documented_offset() -> None:
    # Unit-level, so it runs without a build: 0-indexed storage -> 1-indexed tier.
    assert _item_rarity_tier("0") == 1
    assert _item_rarity_tier("4") == 5


def test_an_unreadable_rarity_is_absent_never_a_fabricated_tier() -> None:
    # §V26/§V67: a value that is not an integer is ABSENT (the key is then omitted), never
    # guessed at. A silent 1 here would invent a tier the source never stated.
    assert _item_rarity_tier(None) is None
    assert _item_rarity_tier("") is None
    assert _item_rarity_tier("T3") is None
    assert _item_rarity_tier("2.5") is None


@pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)
def test_stored_domain_is_still_the_counted_one(conn: sqlite3.Connection) -> None:
    stored = {row[0] for row in conn.execute("SELECT DISTINCT rarity FROM items")}
    assert stored - {None} == _STORED_DOMAIN, f"items.rarity domain moved: {sorted(stored)}"


@pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)
def test_every_stored_rarity_maps_into_the_documented_range(conn: sqlite3.Connection) -> None:
    # The descriptions promise "1 to 5". A stored value outside that after conversion would
    # make the emitted domain wider than the documented one (§V104 class).
    for (stored,) in conn.execute("SELECT DISTINCT rarity FROM items WHERE rarity IS NOT NULL"):
        tier = _item_rarity_tier(stored)
        assert tier is not None and 1 <= tier <= 5, f"{stored!r} -> {tier}"


@pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)
@pytest.mark.parametrize(("game_id", "display_name", "tier"), _PINNED_TIERS)
def test_pinned_items_reach_the_wire_as_their_in_game_tier(
    conn: sqlite3.Connection, game_id: str, display_name: str, tier: int
) -> None:
    # End to end through the tool, not the helper: the point is what a client receives.
    env = build_get_item_drops_spec(lambda: conn).handler(server="en", game_id=game_id)
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    item = data["item"]
    assert isinstance(item, dict)
    assert item["display_name"] == display_name, "the pinned id no longer names this item"
    assert item["rarity"] == tier
    # The type half of §V99: an int, never the string the column still stores.
    assert isinstance(item["rarity"], int)


@pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)
def test_both_drop_directions_agree_on_the_scale(conn: sqlite3.Connection) -> None:
    # §V37: one bridge, two views. The stage view's per-drop ``item_rarity`` and the item
    # view's ``rarity`` are the same fact and must not diverge -- a second conversion home
    # is how they would.
    stage_env = build_get_stage_drops_spec(lambda: conn).handler(server="en", stage_code="1-7")
    stage_data = stage_env.to_dict()["data"]
    assert isinstance(stage_data, dict)
    drops = stage_data["drops"]
    assert isinstance(drops, list) and drops
    by_id = {d["item_game_id"]: d for d in drops}
    checked = 0
    for game_id, _name, tier in _PINNED_TIERS:
        drop = by_id.get(game_id)
        if drop is None:
            continue
        assert drop["item_rarity"] == tier
        checked += 1
    assert checked, "1-7 dropped none of the pinned items; the agreement went unchecked"


@pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)
def test_operator_rarity_keeps_its_own_1_indexed_domain(conn: sqlite3.Connection) -> None:
    # The other side of §V99's "same base" requirement. B133 recorded operator rarity as
    # already CORRECT (1..6 star count), so this fix must not have moved it -- the bases
    # agree because items came to operators, not the reverse.
    stored = {row[0] for row in conn.execute("SELECT DISTINCT rarity FROM operators")}
    assert stored - {None} == {1, 2, 3, 4, 5, 6}
