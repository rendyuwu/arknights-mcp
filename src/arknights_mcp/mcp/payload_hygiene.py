"""What a response payload may carry: null discipline (§V67) + source masks (§V103).

Two rules about the SAME leaves, so they ride the same walk and the same §V37 home:

* **§V67** -- a ``null`` never reaches the wire. A list field is ``[]`` when the source
  confirms none and ABSENT when the source carries no such data; an optional scalar the
  source did not fill is ABSENT. A client cannot decide "none" from "unknown" out of a
  ``null`` (B58), so the key simply is not there.
* **§V103** -- a source string that is a MASK (``？？？``, ``???``, ``-``,
  empty-after-sanitize) is not a fact. Shipped bare it reads as content, and a client LLM
  answers "her talent is ？？？" or invents the hidden mechanic -- fabrication-by-passthrough,
  the mirror of the fabrication-by-omission §V65 exists to stop (B141).

Both run at ONE place, :func:`clean_payload`, called from the single envelope builder --
NOT at each emit site. That is the whole lesson of B135: null discipline had already been
"rolled out" four times (T130 top-level keys, T170 search locators, T180 per-entity
scalars, T184 drop surfaces) and each pass fixed the surface then under review, so
never-reviewed sites kept shipping nulls. Walking every tool's serialized envelope over
the promoted build finds ELEVEN live null paths where B135 named five, and a static audit
of the tool shaping functions finds ~60 further optional fields emitted unconditionally
that merely happen to be filled on today's corpus. A fifth per-surface pass would author
the sixth recurrence. Applying the rule where every response converges makes it hold by
construction, including for tools that do not exist yet.

The mask predicate is a SHAPE rule, not a literal list, and it is COUNTED rather than
guessed (§V96): see :func:`is_source_mask`.

Because the null rule now holds for every tool rather than for the surfaces that had been
swept, the client-facing statement of it stopped hedging:
``_shared.LIST_FIELD_CONVENTION`` used to promise that "a numeric stat the source lacks
may instead be null", which was accurate about the unswept sites and is now false.
Understating a guarantee costs a client exactly what overstating one does -- it keeps
handling a case that cannot arise.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

#: §V103 mask predicate: a value carrying no word character at all. Deliberately a shape
#: rule rather than a list of literals -- a literal list only ever knows the masks someone
#: already saw, and upstream is free to invent a sixth tomorrow.
#:
#: COUNTED over the promoted build (``2026-07-28T170428Z-en-cn``), across EVERY text column,
#: this predicate partitions non-degenerately (§V96): it selects exactly five distinct
#: tokens -- ``-`` (32 rows: 28 token-skill descriptions, 4 Cathy talent descriptions),
#: ``???`` (4: ``guide_01``/``guide_02`` stage codes), ``？？？`` (2: Amiya's E2-locked talent
#: name), ``？？？？？`` (2: that talent's description) and ``??:??:??`` (2: ``st_07-04``'s
#: name) -- and selects nothing else. Real names are never selected in either region:
#: ``\w`` is Unicode-aware, so ``阿米娅`` and ``Amiya`` both carry word characters, as does a
#: code like ``4-4``.
_WORD_CHAR = re.compile(r"\w", re.UNICODE)

#: §V103 scope: the wire keys whose value a client reads as a NAME or as EFFECT TEXT. The
#: rule is a key SHAPE (``…name`` / ``…description`` / ``…title`` / ``…code``), so it covers
#: ``display_name``, ``zone_display_name``, ``event_name``, ``item_display_name``,
#: ``description``, ``title`` and ``stage_code`` without a maintained list.
#:
#: Scoping is load-bearing, not tidiness: unscoped, the same predicate flags
#: ``tile_grid.absent_symbol`` (``"."``) and the tile legend's single-character symbols,
#: which are the server's OWN rendering alphabet -- disclosing those as source masks would
#: be a false statement about upstream, and a limitation nobody can act on trains a client
#: to ignore the ones that matter.
_MASKABLE_KEY = re.compile(r"(^|_)(name|description|title|code)$")

#: §V22/§V66: the disclosure names paths, and a pathological payload could hold many. Name
#: this many, then give the EXACT remaining count (the §T195 "and N more" precedent) -- a
#: bounded string that still tells the client the true size of what it is not seeing.
_MAX_NAMED_MASK_PATHS = 8


def is_source_mask(value: str) -> bool:
    """True when ``value`` is a source mask rather than content (§V103).

    A mask is a string with no word character left after stripping: ``？？？``, ``???``,
    ``-``, ``??:??:??``, or empty-after-sanitize. The check is on the value's SHAPE, so a
    mask token this corpus has never carried is caught the day upstream ships it, which a
    list of known literals cannot do (§V96 -- the domain is counted, never assumed closed).
    """
    return _WORD_CHAR.search(value.strip()) is None


def _clean(node: object, path: str, masks: list[tuple[str, str]]) -> object:
    """Recursively drop ``None`` dict values (§V67) and record mask leaves (§V103)."""
    if isinstance(node, Mapping):
        out: dict[str, object] = {}
        for key, value in node.items():
            name = str(key)
            if value is None:
                # §V67: the key is absent, never null. Nothing else to do -- absence IS the
                # signal, and a limitation naming the field is the SOLE additional one
                # where a §V asks for it (B98: never null AND limitation).
                continue
            child = f"{path}.{name}" if path else name
            if isinstance(value, str) and _MASKABLE_KEY.search(name) and is_source_mask(value):
                masks.append((child, value))
            out[name] = _clean(value, child, masks)
        return out
    if isinstance(node, str):
        # Guarded before Sequence: a str is a Sequence of str and would recurse forever.
        return node
    if isinstance(node, Sequence) and not isinstance(node, bytes | bytearray):
        # A ``None`` ELEMENT is not dropped: removing it would shift every later index and
        # silently break a positional join. It is a §V67 breach of its own, left visible for
        # the contract guard to fail on rather than quietly repaired into a wrong list.
        return [_clean(item, f"{path}[{index}]", masks) for index, item in enumerate(node)]
    return node


def mask_limitation(masks: Sequence[tuple[str, str]]) -> str:
    """The §V103 disclosure for the mask leaves found in one payload.

    Client-facing text, so no internal cites or jargon (§V71 b) -- the cites live in this
    docstring. The wording is deliberately OBSERVATIONAL ("carries no readable text")
    rather than a claim about upstream intent: ``？？？`` is demonstrably a
    locked-until-promotion mask, while ``??:??:??`` may well be a stylised in-game title,
    and the disclosure has to be true either way. What it must stop is the client treating
    the string as the answer, or filling the blank itself.

    The masked value is still emitted. It is what the source says, and replacing it with a
    guess is the fabrication this rule exists to prevent (§V26); the fix is disclosure, not
    substitution.
    """
    named = masks[:_MAX_NAMED_MASK_PATHS]
    listed = ", ".join(f"{path} is {value!r}" for path, value in named)
    remaining = len(masks) - len(named)
    more = f", and {remaining} more field(s) like it" if remaining else ""
    return (
        f"placeholder text from the source: {listed}{more}. A value like this carries no "
        "readable text -- the source itself has none here (content that is locked, unnamed "
        "or not yet published). It is passed through exactly as the source has it and "
        "nothing was inferred from it: do not report it as the real name or effect, and do "
        "not guess what it hides."
    )


def clean_payload(data: Mapping[str, object]) -> tuple[dict[str, object], tuple[str, ...]]:
    """Apply both payload rules to one response body (§V67/§V103).

    Returns the null-free payload plus the limitations it earned -- at most one, the §V103
    mask disclosure, or none when the payload carries no masked name/description.
    """
    masks: list[tuple[str, str]] = []
    cleaned = _clean(data, "", masks)
    assert isinstance(cleaned, dict)  # noqa: S101 -- ``data`` is a Mapping by signature
    return cleaned, ((mask_limitation(masks),) if masks else ())
