"""Untrusted-string sanitization (SPEC §V18; PRD 17.6).

Imported strings are untrusted data. Before storage we strip control and format
characters (which can carry prompt-injection payloads such as bidi overrides)
and cap length. Sanitized text is still only ever returned as structured data,
never concatenated into server instructions or tool descriptions.
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

# Unicode general categories removed from imported strings: control (Cc),
# format (Cf, incl. bidi overrides / zero-width joiners), surrogate (Cs),
# private-use (Co).
_STRIP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co"})


def strip_control_chars(value: str) -> str:
    """Remove control/format/surrogate/private-use characters."""
    return "".join(ch for ch in value if unicodedata.category(ch) not in _STRIP_CATEGORIES)


def sanitize_text(value: str, *, max_length: int = DEFAULT_MAX_TEXT_LENGTH) -> str:
    """Strip control characters, trim surrounding whitespace, and cap length."""
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
