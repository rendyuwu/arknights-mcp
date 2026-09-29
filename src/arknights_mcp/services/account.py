"""Account roster services (ADR 0020): the owner's synced account, read-only.

The roster comes from the account database (:class:`~arknights_mcp.db.account.AccountStore`,
read per call through a SELECT-only role); names, rarity, profession, module types and
skins are enriched from the promoted game build. No network, no write.
Both transports call these functions.

Provenance: the account row (``snapshot_id`` + ``synced_at``) always comes
first, then the build snapshots whose rows enriched the answer.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from arknights_mcp.db.account import (
    ACCOUNT_SERVER,
    AccountStore,
    AccountStoreError,
    StoredRoster,
)
from arknights_mcp.db.repositories.metadata import MetadataRepository
from arknights_mcp.db.repositories.operators import OperatorRepository, OperatorRow
from arknights_mcp.importers.account import AccountRoster, OwnedOperator
from arknights_mcp.models.common import PAGE_SIZE_DEFAULT
from arknights_mcp.services.base_skills import (
    BASE_SKILL_DOMAIN_MISSING_LIMITATION,
    COLLAB_LIMITATION,
    base_skill_entries,
)
from arknights_mcp.services.operators import operator_identity
from arknights_mcp.services.stages import (
    SectionPage,
    StageProvenance,
    _section_page,
    _validate_page,
)

AccountStatus = Literal[
    "ok",
    "not_found",
    "unsupported_server",
    "account_unavailable",
    "schema_incompatible",
    "unknown_faction",
]

ACCOUNT_SNAPSHOT_LIMITATION = (
    "This is the account as it was at synced_at, not live game state. Progress made in "
    "game since then is not shown."
)
ACCOUNT_UNKNOWN_OPERATOR_LIMITATION = (
    "Some owned operators are not in this build, for example an operator newer than the "
    "build, so only their game_id and account progress are shown."
)
ACCOUNT_EMPTY_ROSTER_LIMITATION = (
    "No owned operator matches these filters. Loosen or drop them to widen the list."
)
ROSTER_BASE_SKILL_NOTE = (
    "base_skills lists, per slot, the entry in effect for this account (in_effect true) and "
    "any stage not unlocked yet (in_effect false). A stage that a later unlocked stage "
    "replaced is left out."
)
ACCOUNT_UNNAMED_PART_LIMITATION = (
    "Some skill or module names are not in this build, so only their ids are shown. Skills "
    "and modules of an alternate form (form_id) are never named."
)
ACCOUNT_SKINS_LIMITATION = (
    "skins lists the owned outfits this build names for the operator. An owned outfit the "
    "build does not name is not listed."
)
ACCOUNT_EMPTY_INVENTORY_LIMITATION = "The synced account holds no items."
ACCOUNT_UNNAMED_ITEM_LIMITATION = (
    "Some items show only their item_id because this build has no name for them."
)


@dataclass(frozen=True)
class AccountResult:
    """One account tool's outcome. ``data`` is the wire payload (``page`` excluded);
    it is empty unless ``status == "ok"``."""

    status: AccountStatus
    server: str
    data: dict[str, object]
    page: SectionPage | None
    provenance: tuple[StageProvenance, ...]
    limitations: tuple[str, ...]


def _failed(status: AccountStatus, server: str) -> AccountResult:
    return AccountResult(status, server, {}, None, (), ())


def _stored(
    account: AccountStore | None, server: str
) -> tuple[StoredRoster, AccountRoster] | AccountStatus:
    if server != ACCOUNT_SERVER:
        return "unsupported_server"
    if account is None:
        return "account_unavailable"
    try:
        stored = account.load(ACCOUNT_SERVER)
    except AccountStoreError:
        return "account_unavailable"
    if stored is None:
        return "account_unavailable"
    if stored.roster is None:
        return "schema_incompatible"
    return stored, stored.roster


def _provenance(
    stored: StoredRoster, builds: list[OperatorRow | None]
) -> tuple[StageProvenance, ...]:
    build_snapshots = sorted({(b.snapshot_id, b.imported_at) for b in builds if b is not None})
    return (
        StageProvenance(stored.snapshot_id, stored.synced_at),
        *(StageProvenance(s, at) for s, at in build_snapshots),
    )


def _identity(op: OwnedOperator, build: OperatorRow | None) -> dict[str, object]:
    row = operator_identity(op.char_id, build)
    row.update(elite=op.elite, level=op.level, potential=op.potential, skill_level=op.skill_level)
    return row


def get_my_roster(
    game: sqlite3.Connection,
    account: AccountStore | None,
    *,
    server: str,
    min_rarity: int | None = None,
    min_elite: int | None = None,
    room_type: str | None = None,
    faction: str | None = None,
    collab: bool | None = None,
    page: int = 1,
    page_size: int = PAGE_SIZE_DEFAULT,
) -> AccountResult:
    gate = _stored(account, server)
    if not isinstance(gate, tuple):
        return _failed(gate, server)
    stored, roster = gate
    p, size = _validate_page(page, page_size)

    repo = OperatorRepository(game)
    faction_id: str | None = None
    if faction is not None:
        faction_id = repo.resolve_faction(server, faction)
        if faction_id is None:
            return _failed("unknown_faction", server)
    filtered = room_type is not None or faction_id is not None or collab is not None
    allowed = (
        {
            r.operator_pk
            for r in repo.filter_operators(
                server, room_type=room_type, faction_id=faction_id, collab=collab
            )
        }
        if filtered
        else set()
    )
    enriched = [(op, repo.operator_by_game_id(server, op.char_id)) for op in roster.operators]
    matching = [
        (op, build)
        for op, build in enriched
        if (
            min_rarity is None
            or (build is not None and build.rarity is not None and build.rarity >= min_rarity)
        )
        and (min_elite is None or op.elite >= min_elite)
        and (not filtered or (build is not None and build.operator_pk in allowed))
    ]
    matching.sort(
        key=lambda pair: (
            -((pair[1].rarity if pair[1] is not None else None) or 0),
            -pair[0].elite,
            -pair[0].level,
            pair[0].char_id,
        )
    )
    window = matching[(p - 1) * size : p * size]
    skills = (
        repo.base_skills([build.operator_pk for _, build in window if build is not None])
        if room_type is not None
        else {}
    )

    rows: list[dict[str, object]] = []
    for op, build in window:
        row = _identity(op, build)
        row["masteries"] = [s.mastery for s in op.skills if s.form_id == op.char_id]
        base_modules = [m for m in op.modules if m.form_id == op.char_id]
        types = (
            {m.game_id: m.module_type for m in repo.modules(build.operator_pk)}
            if build is not None and base_modules
            else {}
        )
        modules: list[dict[str, object]] = []
        for m in base_modules:
            entry: dict[str, object] = {"module_id": m.module_id}
            if types.get(m.module_id) is not None:
                entry["module_type"] = types[m.module_id]
            entry["level"] = m.level
            modules.append(entry)
        row["modules"] = modules
        if op.equipped_module_id is not None:
            row["equipped_module_id"] = op.equipped_module_id
        if room_type is not None and build is not None:
            row["base_skills"] = base_skill_entries(
                skills.get(build.operator_pk, []),
                room_type=room_type,
                progress=(op.elite, op.level),
            )
        rows.append(row)

    limitations = [ACCOUNT_SNAPSHOT_LIMITATION]
    if any(build is None for _, build in window):
        limitations.append(ACCOUNT_UNKNOWN_OPERATOR_LIMITATION)
    if not matching:
        limitations.append(ACCOUNT_EMPTY_ROSTER_LIMITATION)
    if filtered and not allowed and not repo.has_base_skill_domain():
        limitations.append(BASE_SKILL_DOMAIN_MISSING_LIMITATION)
    if any(row.get("base_skills") for row in rows):
        limitations.append(ROSTER_BASE_SKILL_NOTE)
    if collab is not None:
        limitations.append(COLLAB_LIMITATION)
    data: dict[str, object] = {"server": server, "synced_at": stored.synced_at}
    if room_type is not None:
        data["room_type"] = room_type
    if faction_id is not None:
        data["faction_id"] = faction_id
    data["operators"] = rows
    return AccountResult(
        status="ok",
        server=server,
        data=data,
        page=_section_page(p, size, len(matching)),
        provenance=_provenance(stored, [build for _, build in matching]),
        limitations=tuple(limitations),
    )


def get_my_operator(
    game: sqlite3.Connection,
    account: AccountStore | None,
    *,
    server: str,
    game_id: str,
) -> AccountResult:
    gate = _stored(account, server)
    if not isinstance(gate, tuple):
        return _failed(gate, server)
    stored, roster = gate
    op = next((o for o in roster.operators if o.char_id == game_id), None)
    if op is None:
        return _failed("not_found", server)

    repo = OperatorRepository(game)
    build = repo.operator_by_game_id(server, game_id)
    skill_names = (
        {s.game_id: s.display_name for s in repo.skills(build.operator_pk)} if build else {}
    )
    build_modules = {m.game_id: m for m in repo.modules(build.operator_pk)} if build else {}

    unnamed = False
    skills: list[dict[str, object]] = []
    for s in op.skills:
        base = s.form_id == game_id
        entry: dict[str, object] = {"skill_id": s.skill_id}
        name = skill_names.get(s.skill_id) if base else None
        if name is not None:
            entry["display_name"] = name
        entry.update(mastery=s.mastery, unlocked=s.unlocked)
        if not base:
            entry["form_id"] = s.form_id
        unnamed = unnamed or name is None
        skills.append(entry)

    modules: list[dict[str, object]] = []
    for m in op.modules:
        base = m.form_id == game_id
        module_row = build_modules.get(m.module_id) if base else None
        name = module_row.display_name if module_row is not None else None
        entry = {"module_id": m.module_id}
        if name is not None:
            entry["display_name"] = name
        if module_row is not None and module_row.module_type is not None:
            entry["module_type"] = module_row.module_type
        entry.update(level=m.level, equipped=m.module_id == op.equipped_module_id)
        if not base:
            entry["form_id"] = m.form_id
        unnamed = unnamed or name is None
        modules.append(entry)

    operator = _identity(op, build)
    if op.current_skin_id is not None:
        operator["current_skin_id"] = op.current_skin_id
    operator["skills"] = skills
    operator["modules"] = modules

    limitations = [ACCOUNT_SNAPSHOT_LIMITATION]
    if build is None:
        limitations.append(ACCOUNT_UNKNOWN_OPERATOR_LIMITATION)
    else:
        owned = set(roster.skins)
        skins: list[dict[str, object]] = []
        for skin in repo.skins(server, build.operator_pk):
            if skin.skin_id not in owned:
                continue
            skin_entry: dict[str, object] = {"skin_id": skin.skin_id}
            if skin.display_name is not None:
                skin_entry["display_name"] = skin.display_name
            if skin.tmpl_id and skin.tmpl_id != skin.char_id:
                skin_entry["alt_form"] = True
            skins.append(skin_entry)
        operator["skins"] = skins
    if unnamed:
        limitations.append(ACCOUNT_UNNAMED_PART_LIMITATION)
    if "skins" in operator:
        limitations.append(ACCOUNT_SKINS_LIMITATION)
    return AccountResult(
        status="ok",
        server=server,
        data={"server": server, "synced_at": stored.synced_at, "operator": operator},
        page=None,
        provenance=_provenance(stored, [build]),
        limitations=tuple(limitations),
    )


def get_my_inventory(
    game: sqlite3.Connection,
    account: AccountStore | None,
    *,
    server: str,
    page: int = 1,
    page_size: int = PAGE_SIZE_DEFAULT,
) -> AccountResult:
    gate = _stored(account, server)
    if not isinstance(gate, tuple):
        return _failed(gate, server)
    stored, roster = gate
    p, size = _validate_page(page, page_size)

    inventory = roster.inventory
    window = inventory[(p - 1) * size : p * size]
    names = OperatorRepository(game).item_display_names(server, [item for item, _ in window])
    items: list[dict[str, object]] = []
    for item_id, count in window:
        entry: dict[str, object] = {"item_id": item_id}
        if item_id in names:
            entry["display_name"] = names[item_id]
        entry["count"] = count
        items.append(entry)

    limitations = [ACCOUNT_SNAPSHOT_LIMITATION]
    if not inventory:
        limitations.append(ACCOUNT_EMPTY_INVENTORY_LIMITATION)
    if any(item_id not in names for item_id, _ in window):
        limitations.append(ACCOUNT_UNNAMED_ITEM_LIMITATION)
    provenance = [StageProvenance(stored.snapshot_id, stored.synced_at)]
    if names:
        snapshots = MetadataRepository(game).all_snapshots()
        provenance.extend(
            StageProvenance(s.snapshot_id, s.imported_at)
            for s in sorted(snapshots, key=lambda s: s.snapshot_id)
            if s.server == server
        )
    return AccountResult(
        status="ok",
        server=server,
        data={
            "server": server,
            "synced_at": stored.synced_at,
            "lmd": roster.lmd,
            "items": items,
        },
        page=_section_page(p, size, len(inventory)),
        provenance=tuple(provenance),
        limitations=tuple(limitations),
    )
