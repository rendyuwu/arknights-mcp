"""§T59 (M7) dependency + project-code license audit.

§C fixes the license boundary: **Apache-2.0 project code only**; the ``NOTICE``
excludes imported data + game content. §V16 forbids a release from carrying game
content. This audit proves, fail-closed:

* every third-party package pinned in ``uv.lock`` resolves to a license that is
  compatible with Apache-2.0 redistribution (permissive: MIT/BSD/Apache/PSF/ISC
  and the file-scoped weak-copyleft MPL-2.0), and **no strong copyleft**
  (GPL/AGPL/LGPL, etc.) sneaks in;
* the project's own code is Apache-2.0 (``pyproject`` classifier + installed
  dist metadata + the ``LICENSE`` text);
* the ``NOTICE`` scopes the Apache grant to code only, excludes imported data +
  game content, and attributes the third-party rights holders.

Licenses are read from the *installed* distributions via
:mod:`importlib.metadata` (``License-Expression`` → ``License`` field → trove
classifiers). The set of packages to audit is read from ``uv.lock`` so a newly
added dependency is picked up automatically and, if its license cannot be
resolved to an allowlisted identifier, fails this suite.

Note on the two Windows-only deps (``colorama``, ``pywin32``, gated by
``sys_platform == 'win32'`` in the lock): they are not installed on this Linux
gate, so their licenses are supplied from a small verified fallback map. Both
are permissive.
"""

from __future__ import annotations

import re
import tomllib
from importlib import metadata
from importlib.metadata import PackageNotFoundError
from typing import Any

import pytest
from tests.support import REPO_ROOT

PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"

# --- License allowlist (Apache-2.0-compatible for redistribution) ------------

#: Permissive licenses: no reciprocal obligation, freely combinable with and
#: redistributable under Apache-2.0.
_PERMISSIVE: frozenset[str] = frozenset(
    {
        "MIT",
        "MIT-0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "0BSD",
        "Apache-2.0",
        "PSF-2.0",
    }
)

#: File-scoped weak copyleft. MPL-2.0 is NOT GPL-family copyleft: its reciprocity
#: is per-file and it is explicitly compatible with Apache-2.0. These deps are
#: used unmodified and are not even bundled in the release artifact (the wheel is
#: code + metadata only -- see test_release_audit), so redistribution is clean.
_WEAK_COPYLEFT_COMPATIBLE: frozenset[str] = frozenset({"MPL-2.0"})

#: The full set of license identifiers acceptable for an Apache-2.0 distribution.
_ALLOWED: frozenset[str] = _PERMISSIVE | _WEAK_COPYLEFT_COMPATIBLE

#: Substrings that mark a strong/network copyleft license incompatible with an
#: Apache-2.0 release. "GPL" alone catches GPL/AGPL/LGPL. MPL-2.0 contains none
#: of these markers, so it is not tripped here.
_COPYLEFT_MARKERS: tuple[str, ...] = ("GPL", "AGPL", "LGPL", "EUPL", "SSPL", "CDDL")

#: Case-insensitive normalization of the many spellings license metadata uses
#: (SPDX ids, trove-classifier prose, legacy free text) onto canonical SPDX ids.
_ALIASES: dict[str, str] = {
    "mit": "MIT",
    "mit license": "MIT",
    "mit-0": "MIT-0",
    "mit no attribution": "MIT-0",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "apache": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "mpl-2.0": "MPL-2.0",
    "mpl 2.0": "MPL-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "psf": "PSF-2.0",
    "psf-2.0": "PSF-2.0",
    "python-2.0": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "isc": "ISC",
    "isc license (iscl)": "ISC",
    "0bsd": "0BSD",
}

#: Windows-only deps (``sys_platform == 'win32'`` in uv.lock) not installed on
#: this gate. Licenses verified from upstream project metadata; both permissive.
_KNOWN_PLATFORM_LICENSES: dict[str, frozenset[str]] = {
    "colorama": frozenset({"BSD-3-Clause"}),
    "pywin32": frozenset({"PSF-2.0"}),
}


def _canon(name: str) -> str:
    """PyPI-canonical distribution name (lowercase, ``_``/``.`` → ``-``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _normalize(raw: str) -> str:
    """Map one license spelling to a canonical SPDX id, else return it stripped
    (so an unrecognized token stays visible and fails the allowlist)."""
    return _ALIASES.get(raw.strip().lower(), raw.strip())


def _split_expr(expr: str) -> list[str]:
    """Split an SPDX-ish expression (``A OR B``, ``A AND B``) into its terms."""
    parts = re.split(r"\b(?:OR|AND)\b", expr, flags=re.IGNORECASE)
    return [t for t in (p.strip().strip("()").strip() for p in parts) if t]


def _resolve_license_tokens(name: str) -> frozenset[str]:
    """Resolve the set of canonical license ids for a distribution.

    Reads (in order of specificity) ``License-Expression``, the legacy
    ``License`` field, then the trove ``License ::`` classifiers. Falls back to
    :data:`_KNOWN_PLATFORM_LICENSES` for platform-gated deps not installed here.
    Returns an empty set only when nothing could be resolved -- callers treat
    that as an audit failure (fail-closed).
    """
    try:
        md = metadata.metadata(name)
    except PackageNotFoundError:
        return _KNOWN_PLATFORM_LICENSES.get(_canon(name), frozenset())

    tokens: set[str] = set()

    expr = md.get("License-Expression")
    if expr:
        tokens.update(_normalize(term) for term in _split_expr(expr))

    lic = md.get("License")
    if lic and lic.strip():
        first_line = lic.strip().splitlines()[0].strip()
        # A short first line is an id ("MIT", "Apache 2.0"); a long one is the
        # full license text pasted into the field -- ignore that.
        if first_line and len(first_line) <= 60:
            tokens.update(_normalize(term) for term in _split_expr(first_line))

    for raw in md.get_all("Classifier") or []:
        classifier = str(raw)
        if not classifier.startswith("License") or "::" not in classifier:
            continue
        segment = classifier.split("::")[-1].strip()
        if segment and segment.lower() != "osi approved":
            tokens.add(_normalize(segment))

    return frozenset(tokens)


def _is_copyleft(token: str) -> bool:
    upper = token.upper()
    return any(marker in upper for marker in _COPYLEFT_MARKERS)


def _locked_package_names() -> list[str]:
    lock = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    return sorted(pkg["name"] for pkg in lock.get("package", []))


def _pyproject() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _norm(text: str) -> str:
    """Lower-cased, whitespace-collapsed view so hard-wrapped prose still matches
    multi-word phrase assertions."""
    return " ".join(text.split()).lower()


#: Every distribution the lock pins -- the audit universe (collected once).
_LOCKED_PACKAGES: tuple[str, ...] = tuple(_locked_package_names())


@pytest.mark.parametrize("package", _LOCKED_PACKAGES)
def test_dependency_license_is_apache_compatible(package: str) -> None:
    # §C: an Apache-2.0 release may only carry permissive or Apache-compatible
    # (MPL-2.0) deps. Fail-closed: an unresolved or unrecognized license fails.
    tokens = _resolve_license_tokens(package)
    assert tokens, (
        f"could not resolve a license for {package!r}; install it or add a "
        f"verified entry to _KNOWN_PLATFORM_LICENSES (fail-closed)"
    )

    copyleft = sorted(t for t in tokens if _is_copyleft(t))
    assert not copyleft, (
        f"{package} carries strong-copyleft license(s) {copyleft} -- "
        f"incompatible with an Apache-2.0 release (§C/§V16)"
    )

    unknown = sorted(t for t in tokens if t not in _ALLOWED)
    assert not unknown, (
        f"{package} has unrecognized/incompatible license token(s) {unknown} "
        f"(resolved tokens: {sorted(tokens)}); add to the allowlist only after "
        f"a manual Apache-2.0-compatibility review"
    )


def test_no_copyleft_in_any_dependency() -> None:
    # Headline safety roll-up (§C): scan the whole locked set at once so a single
    # GPL/AGPL/LGPL dep surfaces here with the full offender list.
    offenders: dict[str, list[str]] = {}
    for package in _LOCKED_PACKAGES:
        tokens = _resolve_license_tokens(package)
        copyleft = sorted(t for t in tokens if _is_copyleft(t))
        if copyleft:
            offenders[package] = copyleft
    assert not offenders, f"copyleft dependencies present (Apache-incompatible): {offenders}"


def test_every_locked_package_resolves_to_a_known_license() -> None:
    # Enumerate the full dependency -> license table and prove none is opaque.
    # This is what makes the audit an audit: no dep escapes classification.
    table = {pkg: sorted(_resolve_license_tokens(pkg)) for pkg in _LOCKED_PACKAGES}
    unresolved = sorted(pkg for pkg, toks in table.items() if not toks)
    assert not unresolved, (
        f"unaudited dependencies (no resolvable license): {unresolved}; full table: {table}"
    )
    # Sanity: the audit universe is the real lock, not an empty list.
    assert "mcp" in table and "sqlalchemy" in table and "pydantic" in table


def test_project_code_is_apache_2_0() -> None:
    # §C: the project's OWN code is Apache-2.0. Assert it three ways.
    project = _pyproject()["project"]
    classifiers = [str(c) for c in project.get("classifiers", [])]
    assert any("Apache Software License" in c for c in classifiers), (
        "pyproject must declare the Apache-2.0 OSI classifier"
    )

    license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text and "Version 2.0" in license_text, (
        "LICENSE must be the Apache License, Version 2.0"
    )

    installed = _resolve_license_tokens("arknights-mcp")
    assert installed == frozenset({"Apache-2.0"}), (
        f"installed project metadata must resolve to Apache-2.0, got {sorted(installed)}"
    )


def test_notice_scopes_license_and_attributes_third_parties() -> None:
    # §C / §V16: NOTICE scopes the Apache grant to code only, excludes imported
    # data + game content, and attributes the third-party rights holders.
    norm = _norm((REPO_ROOT / "NOTICE").read_text(encoding="utf-8"))

    # Apache grant scoped to project code only.
    assert "applies only" in norm and "source code" in norm

    # Imported data + game content explicitly excluded from the grant.
    assert "does not apply" in norm or "does not relicense" in norm
    assert "imported" in norm
    assert "game content" in norm

    # Third-party rights holders attributed (Apache-2.0 §4(d) / trademark notice).
    assert "hypergryph" in norm
    assert "yostar" in norm
