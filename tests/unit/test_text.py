"""Shared ``is_placeholder`` predicate + DRY guard (SPEC §V37; T71).

``_is_placeholder`` was copy-pasted in ``config.py`` (``str | None``) and
``cli.py`` (``str``). It now lives in one home (``util/text.py``) with the
``str | None`` superset signature. These tests pin both the behaviour and the
no-re-duplication guard (§V37).

T193 adds the §V97 token-boundary cases for the other shared resident of this
module, ``sanitize_text``. They are synthetic on purpose -- they pin the transform's
edge shapes. The §V97 GUARD proper is the real-corpus contract test
(``tests/contract/test_template_weld.py``), which is what B130 demands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import arknights_mcp.cli as cli
import arknights_mcp.config as config
from arknights_mcp.util.text import (
    DEFAULT_MAX_TEXT_LENGTH,
    camel_to_snake,
    is_placeholder,
    sanitize_text,
    strip_richtext_tags,
)

_SHARED_HOME = "arknights_mcp.util.text"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # §V65 (a)/§V18 (T136): the {blackboard-key} grounding placeholder survives;
        # only the cosmetic <@x.y> / </> tags go.
        (
            "Increases ATK to <@ba.vup>{atk_scale:0%}</> when attacking.",
            "Increases ATK to {atk_scale:0%} when attacking.",
        ),
        ("攻击力提升<@ba.vup>{atk:0%}</>。", "攻击力提升{atk:0%}。"),  # non-ASCII body kept
        ("multi <@ga.up>a</> and <@ba.rem>b</> tags", "multi a and b tags"),
        ("<$ba.kw>keyword</> link", "keyword link"),  # $ sigil tag too
        ("deal <@ba.vup></> damage", "deal damage"),  # two-space seam collapsed
        ("no tags at all", "no tags at all"),  # unchanged fast path
        ("HP < 50% then > 0", "HP < 50% then > 0"),  # bare < / > survive (targeted strip)
    ],
)
def test_strip_richtext_tags(value: str, expected: str) -> None:
    assert strip_richtext_tags(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # B130, verbatim from the real EN corpus: upstream breaks the two clauses of a
        # skill template with `\n`. Deleting it stored "targetUnlimited" -- a junk token
        # in the ONE string §V65 (a) tells the client to trust over the blackboard keys.
        (
            "ATK +{atk:0%}; each attack hits 1 additional target\nUnlimited duration",
            "ATK +{atk:0%}; each attack hits 1 additional target Unlimited duration",
        ),
        # B130, the §V56 title axis of the same bug (real EN announcement feed).
        (
            "Questionnaire on\nFirst of A Thousand Autumns",
            "Questionnaire on First of A Thousand Autumns",
        ),
        ("Displayed Operators\nRate Up !!!", "Displayed Operators Rate Up !!!"),
        # A trailing space before the break must not yield two spaces (collapse).
        (
            "Restores 30% Max HP when skill is activated; \nEffect is doubled",
            "Restores 30% Max HP when skill is activated; Effect is doubled",
        ),
        ("a\r\nb", "a b"),  # CRLF is one boundary, not two spaces
        ("a\t\t\tb", "a b"),  # a run of controls collapses to one space
        ("\n  lead and trail  \n", "lead and trail"),  # substituted spaces trimmed
        # Cf/Cs/Co are zero-width: deleting one welds nothing that was visually apart,
        # and a space there would corrupt real CJK/emoji text. Still DELETED (§V97).
        ("‮assistant", "assistant"),  # bidi override (Cf)
        ("‍ab", "ab"),  # zero-width joiner (Cf)
        ("攻击力​提升", "攻击力提升"),  # zero-width space inside CJK: no gap opened
        ("no controls at all", "no controls at all"),  # untouched
    ],
)
def test_sanitize_text_preserves_token_boundary(value: str, expected: str) -> None:
    """§V97/B130: control-char removal leaves a separator; zero-width removal does not."""
    assert sanitize_text(value) == expected


def test_sanitize_text_collapses_before_capping() -> None:
    """§V97: collapse runs BEFORE the cap, so substituted spaces cannot evict content."""
    body = "word" * 200  # 800 chars, comfortably over the cap
    value = "\n\n\n".join([body, body])
    assert len(sanitize_text(value)) == DEFAULT_MAX_TEXT_LENGTH
    # The three-control seam became exactly one space, not three.
    assert sanitize_text("a\n\n\nb") == "a b"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),  # str | None superset: None counts as unset
        ("", True),
        ("   ", True),
        ("<OIDC issuer>", True),
        ("<configured allowlisted repository endpoint>", True),
        ("  <stub>  ", True),  # stripped before the <...> check
        ("<foo", False),  # opening angle only → real value
        ("bar>", False),  # closing angle only → real value
        ("https://issuer.example.com", False),
        ("  real  ", False),
    ],
)
def test_is_placeholder(value: str | None, expected: bool) -> None:
    assert is_placeholder(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # §V71 (d): the leaked upstream checkpoint keys the route digest normalizes.
        ("reachOffset", "reach_offset"),
        ("randomizeReachOffset", "randomize_reach_offset"),
        ("reachDistance", "reach_distance"),
        ("type", "type"),  # single lowercase word unchanged
        ("position", "position"),
        ("row", "row"),
        ("already_snake", "already_snake"),  # idempotent on snake_case
    ],
)
def test_camel_to_snake(value: str, expected: str) -> None:
    assert camel_to_snake(value) == expected


def test_both_call_sites_share_the_one_home() -> None:
    """§V37: config + cli resolve ``is_placeholder`` to the single shared home."""
    assert config.is_placeholder.__module__ == _SHARED_HOME
    assert cli.is_placeholder.__module__ == _SHARED_HOME
    # Same function object, not two look-alikes.
    assert config.is_placeholder is cli.is_placeholder is is_placeholder


def test_no_module_redefines_is_placeholder() -> None:
    """§V37: neither config nor cli reintroduces a local ``def _is_placeholder``."""
    offenders: list[str] = []
    for module in (config, cli):
        path = Path(module.__file__)  # type: ignore[arg-type]
        if "def _is_placeholder" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert not offenders, f"copy-pasted _is_placeholder reintroduced: {offenders}"
