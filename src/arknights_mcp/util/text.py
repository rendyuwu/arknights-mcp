"""Untrusted-string sanitization (SPEC §V18/§V97; PRD 17.6).

Imported strings are untrusted data. Before storage we remove control and format
characters (which can carry prompt-injection payloads such as bidi overrides) and
cap length. Removal preserves the TOKEN BOUNDARY (§V97): a control char stood
between two words in the source, so it leaves a space behind rather than welding
them. Sanitized text is still only ever returned as structured data, never
concatenated into server instructions or tool descriptions.
"""

from __future__ import annotations

import re
import unicodedata

#: Default maximum length for an imported string field.
DEFAULT_MAX_TEXT_LENGTH = 512

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
