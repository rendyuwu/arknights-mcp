"""Personal account roster parser (ADR 0020).

Turns the ``user`` object of the game server's ``account/syncData`` response into
an allowlisted :class:`AccountRoster`: owned operators (elite, level, potential,
skill level, per-skill mastery, unlocked modules, equipped module, current skin),
owned skins, inventory, and the LMD balance. Everything else in ``user`` --
nickname, player uid, friends, squads, stage progress, gacha state -- is never
read.

The allowlists live here, not in ``field_policy.py``: this is personal data, not
a dataset, and ``FIELD_POLICY_VERSION`` feeds the game-data snapshot hash.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from arknights_mcp.importers.field_policy import apply_allowlist
from arknights_mcp.models.common import MAX_ID_LEN
from arknights_mcp.sources.base import SourceAdapterError
from arknights_mcp.util.text import sanitize_text

ACCOUNT_TRANSFORM_VERSION = "1"

OWNED_CHAR_ALLOWLIST: frozenset[str] = frozenset(
    {
        "charId",
        "evolvePhase",
        "level",
        "potentialRank",
        "mainSkillLvl",
        "skin",
        "skinId",
        "currentEquip",
        "skills",
        "equip",
        "tmpl",
        "currentTmpl",
    }
)
OWNED_SKILL_ALLOWLIST: frozenset[str] = frozenset({"skillId", "unlock", "specializeLevel"})
OWNED_EQUIP_ALLOWLIST: frozenset[str] = frozenset({"locked", "level"})
TMPL_FORM_ALLOWLIST: frozenset[str] = frozenset({"skinId", "skills", "equip", "currentEquip"})

#: The default "no module" slot every operator carries (``importers/modules.py``
#: ``_NON_MODULE_TYPES``). ponytail: id-prefix convention, 0 of 972 imported
#: modules on build 2026-09-27T220724Z start with it; switch to a build
#: ``modules`` lookup if it ever stops holding.
INITIAL_MODULE_PREFIX = "uniequip_001_"


@dataclass(frozen=True)
class OwnedSkill:
    form_id: str
    slot: int
    skill_id: str
    unlocked: bool
    mastery: int


@dataclass(frozen=True)
class OwnedModule:
    form_id: str
    module_id: str
    level: int


@dataclass(frozen=True)
class OwnedOperator:
    char_id: str
    elite: int
    level: int
    potential: int
    skill_level: int
    current_skin_id: str | None
    equipped_module_id: str | None
    skills: tuple[OwnedSkill, ...]
    modules: tuple[OwnedModule, ...]


@dataclass(frozen=True)
class AccountRoster:
    lmd: int
    operators: tuple[OwnedOperator, ...]
    skins: tuple[str, ...]
    inventory: tuple[tuple[str, int], ...]
    skipped: int


def _id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = sanitize_text(value)
    return text if 1 <= len(text) <= MAX_ID_LEN else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _shape_error(path: str) -> SourceAdapterError:
    # The path only, never a value.
    return SourceAdapterError(f"unexpected syncData shape: {path}")


class _Parser:
    def __init__(self) -> None:
        self.skipped = 0

    def skills(self, form_id: str, raw: Any) -> list[OwnedSkill]:
        out: list[OwnedSkill] = []
        if not isinstance(raw, list):
            return out
        for slot, item in enumerate(raw):
            if not isinstance(item, Mapping):
                self.skipped += 1
                continue
            kept = apply_allowlist(item, OWNED_SKILL_ALLOWLIST).kept
            skill_id = _id(kept.get("skillId"))
            mastery = _count(kept.get("specializeLevel"))
            if skill_id is None or mastery is None:
                self.skipped += 1
                continue
            unlocked = (_count(kept.get("unlock")) or 0) > 0
            out.append(OwnedSkill(form_id, slot, skill_id, unlocked, mastery))
        return out

    def modules(self, form_id: str, raw: Any) -> list[OwnedModule]:
        out: list[OwnedModule] = []
        if not isinstance(raw, Mapping):
            return out
        for raw_id, value in raw.items():
            module_id = _id(raw_id)
            if module_id is not None and module_id.startswith(INITIAL_MODULE_PREFIX):
                continue
            if not isinstance(value, Mapping):
                self.skipped += 1
                continue
            kept = apply_allowlist(value, OWNED_EQUIP_ALLOWLIST).kept
            level = _count(kept.get("level"))
            if module_id is None or level is None:
                self.skipped += 1
                continue
            if (_count(kept.get("locked")) or 0) == 0:
                out.append(OwnedModule(form_id, module_id, level))
        return out

    def operator(self, entry: Mapping[str, Any]) -> OwnedOperator | None:
        kept = apply_allowlist(entry, OWNED_CHAR_ALLOWLIST).kept
        char_id = _id(kept.get("charId"))
        elite = _count(kept.get("evolvePhase"))
        level = _count(kept.get("level"))
        potential = _count(kept.get("potentialRank"))
        skill_level = _count(kept.get("mainSkillLvl"))
        if char_id is None or elite is None or level is None:
            return None
        if potential is None or skill_level is None:
            return None

        tmpl = kept.get("tmpl")
        forms: list[tuple[str, Mapping[str, Any]]] = []
        if isinstance(tmpl, Mapping) and tmpl:
            for raw_form_id, form in tmpl.items():
                form_id = _id(raw_form_id)
                if form_id is None or not isinstance(form, Mapping):
                    self.skipped += 1
                    continue
                forms.append((form_id, apply_allowlist(form, TMPL_FORM_ALLOWLIST).kept))
        else:
            forms.append((char_id, kept))

        skills: list[OwnedSkill] = []
        modules: list[OwnedModule] = []
        for form_id, form in forms:
            skills.extend(self.skills(form_id, form.get("skills")))
            modules.extend(self.modules(form_id, form.get("equip")))
        skills.sort(key=lambda s: (s.form_id != char_id, s.form_id, s.slot))
        modules.sort(key=lambda m: (m.form_id != char_id, m.form_id, m.module_id))

        equipped: str | None = None
        if isinstance(tmpl, Mapping):
            current_form = tmpl.get(_id(kept.get("currentTmpl")) or char_id)
            if isinstance(current_form, Mapping):
                equipped = _id(current_form.get("currentEquip"))
        equipped = equipped or _id(kept.get("currentEquip"))
        if equipped is not None and equipped.startswith(INITIAL_MODULE_PREFIX):
            equipped = None

        return OwnedOperator(
            char_id=char_id,
            elite=elite,
            level=level,
            potential=potential + 1,
            skill_level=skill_level,
            current_skin_id=_id(kept.get("skin")) or _id(kept.get("skinId")),
            equipped_module_id=equipped,
            skills=tuple(skills),
            modules=tuple(modules),
        )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def parse_sync_data(user: object) -> AccountRoster:
    """Allowlist a syncData ``user`` object into an :class:`AccountRoster`.

    Raises :class:`SourceAdapterError` naming the dotted path when the required
    skeleton (``status.gold``, ``troop.chars``) is missing; malformed individual
    entries are skipped and counted in ``skipped``.
    """
    if not isinstance(user, Mapping):
        raise _shape_error("user")
    status = user.get("status")
    if not isinstance(status, Mapping):
        raise _shape_error("status")
    lmd = _count(status.get("gold"))
    if lmd is None:
        raise _shape_error("status.gold")
    troop = user.get("troop")
    if not isinstance(troop, Mapping):
        raise _shape_error("troop")
    chars = troop.get("chars")
    if not isinstance(chars, Mapping):
        raise _shape_error("troop.chars")

    parser = _Parser()
    operators: dict[str, OwnedOperator] = {}
    for entry in chars.values():
        owned = parser.operator(entry) if isinstance(entry, Mapping) else None
        if owned is None or owned.char_id in operators:
            parser.skipped += 1
            continue
        operators[owned.char_id] = owned

    character_skins = _mapping(_mapping(user.get("skin")).get("characterSkins"))
    skins = {skin_id for key, owned in character_skins.items() if owned and (skin_id := _id(key))}
    inventory: dict[str, int] = {}
    for key, value in _mapping(user.get("inventory")).items():
        item_id, count = _id(key), _count(value)
        if item_id is not None and count:
            inventory[item_id] = count

    return AccountRoster(
        lmd=lmd,
        operators=tuple(operators[k] for k in sorted(operators)),
        skins=tuple(sorted(skins)),
        inventory=tuple(sorted(inventory.items())),
        skipped=parser.skipped,
    )
