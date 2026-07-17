"""Shared ``is_placeholder`` predicate + DRY guard (SPEC §V37; T71).

``_is_placeholder`` was copy-pasted in ``config.py`` (``str | None``) and
``cli.py`` (``str``). It now lives in one home (``util/text.py``) with the
``str | None`` superset signature. These tests pin both the behaviour and the
no-re-duplication guard (§V37).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import arknights_mcp.cli as cli
import arknights_mcp.config as config
from arknights_mcp.util.text import is_placeholder

_SHARED_HOME = "arknights_mcp.util.text"


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
