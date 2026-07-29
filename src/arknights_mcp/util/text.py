"""Untrusted-string sanitization (SPEC §V18/§V97/§V109; PRD 17.6).

Imported strings are untrusted data. Before storage we remove control and format
characters (which can carry prompt-injection payloads such as bidi overrides) and
cap length. Removal preserves the TOKEN BOUNDARY (§V97): a control char stood
between two words in the source, so it leaves a space behind rather than welding
them. §V65 (a) effect templates additionally have their rich-text markup stripped
BEFORE the cap, and are capped at their own ceiling (:func:`clean_template_text`,
§V109). Sanitized text is still only ever returned as structured data, never
concatenated into server instructions or tool descriptions.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

#: Default maximum length for an imported string field -- the name/label class.
DEFAULT_MAX_TEXT_LENGTH = 512

#: Maximum length for a §V65 (a) effect-description TEMPLATE (§V109). A template is
#: the grounding path, not a label, so it gets its own ceiling: the real EN corpus at
#: the pinned commit peaks at 740 chars post-strip (skill/talent) and 547 (module
#: trait/talent changes), so 1024 clears it with headroom. Under the old 512 the cap
#: cut 349 of 12057 EN templates mid-sentence (B154).
MAX_TEMPLATE_LENGTH = 1024

#: Arknights in-game rich-text tags that wrap effect-template text: an opening
#: color/keyword tag carrying a sigil + dotted key (``<@ba.vup>`` / ``<$ba.kw>``) and
#: the bare close ``</>``. They are cosmetic markup, never grounding -- the
#: ``{blackboard-key}`` placeholders are (§V65 (a)). Matched narrowly (the ``@``/``$``
#: sigil + a dotted key, or the bare close) so a literal ``<`` / ``>`` elsewhere in the
#: text survives -- §V18 wants a targeted strip, never a blanket ``<...>``.
_RICHTEXT_TAG = re.compile(r"<[@$][\w.]+>|</>")

# Unicode general category control (Cc). These occupy a VISUAL BREAK in the source
# text -- upstream uses `\n` as a real clause separator -- so each is replaced with a
# single space, never deleted in place (§V97/B130). Deleting welded the two words
# either side into a junk token that survived into the DB, the wire, and FTS:
# `"...additional target\nUnlimited duration"` stored as `"...targetUnlimited"`. The
# non-whitespace Cc (NUL, ESC) ride the same rule: a space is harmless there and the
# split never has to reason about which control char upstream meant as a separator.
_SPACE_CATEGORIES = frozenset({"Cc"})

# Unicode general categories DELETED from imported strings: format (Cf, incl. bidi
# overrides / zero-width joiners / soft hyphen), surrogate (Cs), private-use (Co).
# Unlike Cc these are zero-width by construction, so removing one welds nothing that
# was visually apart -- and substituting a space would instead corrupt legitimate
# CJK/emoji text that relies on a zero-width joiner (§V97 applies to the token
# BOUNDARY; a zero-width char is not a boundary).
_DROP_CATEGORIES = frozenset({"Cf", "Cs", "Co"})

#: A run of two or more literal spaces -- collapsed after the Cc substitution so a
#: `" \n"` seam yields one space, not two (§V97 "single space + collapse + trim").
_SPACE_RUN = re.compile(r" {2,}")


def strip_control_chars(value: str) -> str:
    """Replace control chars with a space, drop zero-width format/surrogate/private-use.

    Token boundaries survive (§V97): a removed ``\\n``/``\\r``/``\\t`` leaves a space
    behind, so the words either side stay separate words. Runs of spaces the
    substitution creates are collapsed to one; trimming and the length cap belong to
    :func:`sanitize_text`.
    """
    out: list[str] = []
    for ch in value:
        category = unicodedata.category(ch)
        if category in _SPACE_CATEGORIES:
            out.append(" ")
        elif category not in _DROP_CATEGORIES:
            out.append(ch)
    return _SPACE_RUN.sub(" ", "".join(out))


def sanitize_text(value: str, *, max_length: int = DEFAULT_MAX_TEXT_LENGTH) -> str:
    """Sanitize control characters, trim surrounding whitespace, and cap length.

    Collapse happens before the cap (§V97), so the substituted spaces cannot push
    real content past ``max_length``.
    """
    cleaned = strip_control_chars(value).strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]
    return cleaned


def strip_richtext_tags(value: str) -> str:
    """Strip Arknights rich-text tags from an effect template, keep the inner text.

    ``Increases ATK to <@ba.vup>{atk_scale:0%}</> when attacking.`` becomes
    ``Increases ATK to {atk_scale:0%} when attacking.`` -- the ``{...}`` grounding
    placeholders (§V65 (a)) survive; only the cosmetic ``<@x.y>`` / ``</>`` markup goes
    (§V18). A double space a removed tag leaves behind is collapsed; a string with no
    ``<`` is returned unchanged. The single §V37 home for the tag strip shared by the
    skill/talent (operator) and module template imports.
    """
    if "<" not in value:
        return value
    stripped = _RICHTEXT_TAG.sub("", value)
    # A standalone tag can leave a two-space seam; collapse only runs of literal
    # spaces (never newlines) and trim, matching sanitize_text's posture.
    return re.sub(r" {2,}", " ", stripped).strip()


def clean_template_text(value: str) -> str:
    """A §V65 (a) effect-description TEMPLATE as clean grounding text (§V109).

    The single §V37 home for the template pipeline shared by the skill, talent, and
    module-change imports, and the one place the ORDER is fixed: the rich-text tags go
    **first**, the length cap **second**. Capping first spends the budget on
    ``<@ba.vup>``/``</>`` markup the client never sees, and -- because the strip then
    shortens the result back under the cap -- the truncation leaves no ``len == cap``
    fingerprint to detect it by. That silently cut 349 of 12057 real EN templates
    mid-sentence (B154); a halved mechanic reads as a complete sentence, which is worse
    than the bare blackboard keys §V65 (a) exists to replace.

    Caller passes the RAW source string, never an ``apply_allowlist`` output: a value
    that already went through the allowlist has been capped at
    ``DEFAULT_MAX_TEXT_LENGTH`` with its tags still in place, which is the bug.
    """
    return sanitize_text(strip_richtext_tags(value), max_length=MAX_TEMPLATE_LENGTH)


#: Boundary between a lowercase/digit and an uppercase letter in a lowerCamelCase
#: identifier -- the single split point for :func:`camel_to_snake`.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def camel_to_snake(name: str) -> str:
    """Normalize a lowerCamelCase wire key to snake_case (§V71 (d)).

    ``reachOffset`` -> ``reach_offset``, ``randomizeReachOffset`` ->
    ``randomize_reach_offset``; a single lowercase word (``type``, ``position``,
    ``row``) is returned unchanged, as is an already-snake_case key. The single
    §V37 home for the shaping-layer camelCase strip: upstream keys leak in
    camelCase, but the wire contract is snake_case, and the rename happens where
    the envelope is built (§V71 (d)) -- the importer and stored fragments keep the
    source key names.
    """
    return _CAMEL_BOUNDARY.sub("_", name).lower()


def is_placeholder(value: str | None) -> bool:
    """A value is unset for validation purposes if empty or a ``<...>`` stub.

    Single shared home (§V37) for the placeholder check used by ``config`` (OIDC
    descriptor validation, §V9/§V10) and ``cli`` (sync base_url guard, §V5). The
    ``str | None`` signature is the superset of the two former copies: ``None``
    counts as unset.
    """
    if value is None:
        return True
    stripped = value.strip()
    return not stripped or (stripped.startswith("<") and stripped.endswith(">"))


def template_text(value: Any) -> str | None:
    """Effect-description TEMPLATE as clean grounding text (§V18/§V65 (a)/§V109).

    Read from the RAW source level/candidate, **not** the ``apply_allowlist`` output:
    the allowlist caps at ``DEFAULT_MAX_TEXT_LENGTH`` with the rich-text tags still in
    place, so the budget goes to markup and the template is cut mid-sentence (B154).
    :func:`clean_template_text` strips the tags first (``<@ba.vup>{atk_scale:0%}</>``
    -> ``{atk_scale:0%}``, keeping the ``{blackboard-key}`` grounding placeholders),
    then sanitizes and caps at the template ceiling (§V109). The key is still on the
    allowlist -- reading it directly only bypasses the cap, never the policy. A
    blank-after-clean or non-string value yields ``None`` -- never an empty template.

    Lives here rather than in an importer (§T202/§V37): the operator and skill
    importers both read templates this way, and a private copy in each is how the
    ``_as_dict`` duplicate this module's sibling already owned went unnoticed.
    """
    if not isinstance(value, str):
        return None
    return clean_template_text(value) or None
