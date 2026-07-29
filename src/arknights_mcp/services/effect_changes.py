"""Talent/trait effect-change emit shaping (§V83/§V66.3/§V71 d; §V37 single home).

The change bundles a module or talent carries are decoded structural JSON, shared
verbatim by two read services -- :mod:`arknights_mcp.services.operators` (``get_operator``
with ``include_modules``) and :mod:`arknights_mcp.services.module_compare`
(``compare_operator_modules``). Everything that reshapes one for the wire lives here so
the two surfaces cannot drift:

* **§V83/§V66** -- :func:`dedup_effect_changes` collapses the duplicate/subset rows the
  source emits for one change, and :func:`hoist_uniform_changes` lifts a bundle that is
  byte-identical at every level onto the parent. Both are byte-lossless.
* **§V83** -- :func:`label_token_effects` names the ``-1`` summon/token sentinel instead
  of leaving a client to guess it.
* **§V71 (d)** -- :func:`normalize_change_keys` is the last step: the source's camelCase
  keys become snake_case and the doubly-encoded unlock phase becomes one encoding.

This module was split out of ``services/operators.py`` under §V38: that module had
reached 767 lines against the 800-line hard cap, and this cluster is a self-contained
responsibility group with two callers -- the same forced split as ``_stage_selector``
(T195) and ``enemy_normalization`` (T210).
"""

from __future__ import annotations

import json

from arknights_mcp.util.coerce import suffix_int

#: The keys that IDENTIFY which talent/trait change a bundle is (§V83): two entries sharing
#: these describe the same change (same talent, same potential gate, same unlock condition);
#: every other key (``blackboard``, ``description``) is value-bearing and may be merged.
_EFFECT_IDENTITY_KEYS: frozenset[str] = frozenset(
    {"talentIndex", "requiredPotentialRank", "unlockCondition"}
)


def _canonical(value: object) -> str:
    """A stable, order-independent string for byte-identity comparison of a decoded value."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


def _empty_effect_value(value: object) -> bool:
    """A value-bearing change field that carries nothing (absent, ``[]``, ``{}``, ``""``)."""
    return value is None or value == [] or value == {} or value == ""


def _effect_identity(entry: dict[str, object]) -> str:
    """The identity key of one change bundle -- its (talentIndex, potential, unlock) triple."""
    return _canonical(
        [entry.get(k) for k in ("talentIndex", "requiredPotentialRank", "unlockCondition")]
    )


def _effect_conflict(a: dict[str, object], b: dict[str, object]) -> bool:
    """True when two same-identity bundles carry DIFFERENT non-empty value fields (§V83).

    A conflict means the entries are genuinely different data (e.g. two distinct
    blackboards under the same talent/potential gate), so they must NOT be merged and
    stay as separate rows -- the dedup is byte-lossless (§V66). An empty field never
    conflicts (it is subsumed by the other's value).
    """
    for key in (set(a) | set(b)) - _EFFECT_IDENTITY_KEYS:
        va, vb = a.get(key), b.get(key)
        if not _empty_effect_value(va) and not _empty_effect_value(vb) and va != vb:
            return True
    return False


def _merge_effect(target: dict[str, object], entry: dict[str, object]) -> None:
    """Fold ``entry``'s non-empty value fields into ``target`` in place (§V83).

    Only fills a field ``target`` lacks or left empty -- a conflicting field is never
    reached (the caller checks :func:`_effect_conflict` first). The merged row is a
    superset of both inputs, so no information is lost (§V66 byte-lossless).
    """
    for key, value in entry.items():
        if key in _EFFECT_IDENTITY_KEYS:
            continue
        if _empty_effect_value(target.get(key)) and not _empty_effect_value(value):
            target[key] = value


def dedup_effect_changes(changes: object) -> object:
    """Collapse duplicate/subset talent/trait change bundles into one row each (§V83/§V66).

    Two bundles sharing an identity -- (``talentIndex``, ``requiredPotentialRank``,
    ``unlockCondition``) -- describe the SAME change; the source sometimes emits it several
    times split across parts (a prose-only copy, a blackboard-only copy, a blackboard+prose
    copy). They are merged into one row carrying the union of their non-empty fields, so a
    single talent no longer emits N near-identical rows (B88). The merge is byte-lossless:
    only non-conflicting entries collapse (:func:`_effect_conflict`); a genuine conflict
    (two distinct non-empty blackboards under one gate) keeps the rows separate. Group
    order follows first appearance; a non-list value is returned unchanged. The single §V37
    home shared by the operator + module-compare read services.
    """
    if not isinstance(changes, list):
        return changes
    groups: list[object] = []
    identities: list[str | None] = []
    for entry in changes:
        if not isinstance(entry, dict):
            groups.append(entry)
            identities.append(None)
            continue
        identity = _effect_identity(entry)
        for i, existing in enumerate(groups):
            if (
                identities[i] == identity
                and isinstance(existing, dict)
                and not _effect_conflict(existing, entry)
            ):
                _merge_effect(existing, entry)
                break
        else:
            groups.append(dict(entry))
            identities.append(identity)
    return groups


def label_token_effects(changes: object) -> object:
    """Label a summon/token talent change (``talentIndex == -1``) with ``applies_to`` (§V83).

    A ``talentIndex`` of ``-1`` is a game-data sentinel for an effect that applies to the
    operator's summon/token rather than the operator itself; emitted bare it forces a client
    to guess (B88), so an ``applies_to: "token"`` label is added (additive, §V21). Every
    other bundle is passed through unchanged; a non-list value is returned as-is. The single
    §V37 home shared by both read services (a trait change carries no ``talentIndex`` so it
    is untouched).
    """
    if not isinstance(changes, list):
        return changes
    labelled: list[object] = []
    for entry in changes:
        if isinstance(entry, dict) and entry.get("talentIndex") == -1 and "applies_to" not in entry:
            labelled.append({**entry, "applies_to": "token"})
        else:
            labelled.append(entry)
    return labelled


def dedup_and_label_changes(changes: object) -> object:
    """Dedup subset/duplicate change rows then label token effects (§V83; §V37 home).

    The emit-shaping pair applied to every per-level talent/trait change list by both read
    services: :func:`dedup_effect_changes` collapses the redundant rows, then
    :func:`label_token_effects` tags a ``-1`` summon/token change. Operates on an
    already-``shape_blackboard``ed (and, for a hoisted trait template, description-stripped)
    list so the two pipelines stay identical after their differing pre-steps.

    §V71 (d)/B140: :func:`normalize_change_keys` runs LAST, so the dedup identity and the
    token label still read the source's own key names while the wire sees only snake_case
    and a single phase encoding.
    """
    return normalize_change_keys(label_token_effects(dedup_effect_changes(changes)))


def hoist_uniform_changes(per_level: list[object | None]) -> object | None:
    """The change bundle every present level shares byte-identically, else ``None`` (§V66.3/§V83).

    Returns the bundle when there are at least two present levels and each carries a
    non-empty, byte-identical change list -- the per-level repeat §V66.3 targets, where the
    bundle is hoisted once to the module and dropped from every level. Returns ``None`` when
    a level carries none or a differing bundle, so the caller keeps the per-level copies and
    loses nothing (byte-lossless). The single §V37 home shared by both read services; sibling
    to :func:`hoist_uniform_template` (which hoists a single description string).
    """
    if len(per_level) < 2 or any(_empty_effect_value(v) for v in per_level):
        return None
    if len({_canonical(v) for v in per_level}) == 1:
        return per_level[0]
    return None


#: §V71 (d)/B140: the source's camelCase change-bundle keys and the snake_case name each
#: one takes on the wire. The envelope is snake_case throughout -- these three shipped
#: ``requiredPotentialRank`` / ``talentIndex`` / ``unlockCondition`` three lines from a
#: sibling ``unlock_phase`` / ``stat_bonus``, so one object mixed two naming conventions
#: and a client had to know which fields came straight off the upstream dump.
#:
#: §V71 (d) NAMED these exact keys and gated the rename on T128's ADR. T128 closed and
#: B69 spent the 0.1 -> 0.2 bump it was riding, so the fix lost its vehicle and the
#: invariant has read "satisfied" ever since -- a declared-but-undelivered clause, which
#: is worse than an undeclared one. It rides T198's own tracked 0.2 -> 0.3 flip instead.
_CHANGE_KEY_RENAMES: dict[str, str] = {
    "talentIndex": "talent_index",
    "requiredPotentialRank": "required_potential_rank",
    "unlockCondition": "unlock_condition",
}

#: The SOURCE key whose nested value carries the second phase encoding, and the key
#: inside it. Named separately from :data:`_CHANGE_KEY_RENAMES` so the rename table and
#: the value-normalization trigger cannot drift apart silently.
_UNLOCK_CONDITION_KEY = "unlockCondition"
_UNLOCK_PHASE_KEY = "phase"


def _normalize_unlock_condition(value: object) -> object:
    """One encoding for the unlock phase inside a change bundle (§V99/§V71 d; B140).

    The same module object encoded its phase TWICE, two ways, three lines apart: an int
    ``unlock_phase: 2`` beside ``unlockCondition: {"level": 60, "phase": "PHASE_2"}``.
    One concept, two types -- exactly the "1 wire key, 1 type, 1 meaning" §V99 requires,
    and a client had to know that ``"PHASE_2"`` and ``2`` are the same fact.

    The nested phase becomes the int, matching its sibling, through the existing §V37
    home :func:`~arknights_mcp.util.coerce.suffix_int` (already how the importer reads
    ``PHASE_<n>``). A value that does NOT parse is left exactly as the source sent it --
    an unrecognized encoding is reported, never silently dropped or guessed at (§V26).
    """
    if not isinstance(value, dict):
        return value
    if _UNLOCK_PHASE_KEY not in value:
        return value
    phase = suffix_int(value[_UNLOCK_PHASE_KEY], "PHASE_")
    if phase is None:
        return value
    return {**value, _UNLOCK_PHASE_KEY: phase}


def normalize_change_keys(changes: object) -> object:
    """Rename the camelCase change-bundle keys and collapse the phase encoding (§V71 d).

    Applied as the LAST step of :func:`dedup_and_label_changes`, deliberately: the dedup
    identity (:func:`_effect_identity`) and the token label both key on the SOURCE names,
    so running the rename after them leaves that logic reading exactly what the source
    sent and keeps this function a pure emit-shaping concern. Key ORDER is preserved so a
    renamed bundle serializes in the same place it always did; a non-list value, and a
    non-dict entry inside a list, pass through untouched.
    """
    if not isinstance(changes, list):
        return changes
    out: list[object] = []
    for entry in changes:
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        renamed: dict[str, object] = {}
        for raw_key, value in entry.items():
            key = str(raw_key)
            name = _CHANGE_KEY_RENAMES.get(key, key)
            renamed[name] = (
                _normalize_unlock_condition(value) if key == _UNLOCK_CONDITION_KEY else value
            )
        out.append(renamed)
    return out
