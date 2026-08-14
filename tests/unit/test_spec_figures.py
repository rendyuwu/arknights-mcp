"""§V121 (f): ``SPEC.md``'s own figures, re-derived rather than believed (T218 b).

§V121 (a)-(e) put a counting duty on the spec's measured-sounding claims, and until this
module nothing executed it -- ``SPEC.md`` appeared in zero files under ``tests/``. Five
figures shipped wrong in rows filed *after* §V121 landed, a sweep of the whole file found
two more, and B170's own fix line asserted a machine check that did not exist:
``FRAME_PRESSURE`` does re-measure every tool's frame, but its subject is thirteen code
constants, so no number written in prose was ever checked by anything (B172).

Offline by construction: no network, no promoted build, no registry call. The three
figure kinds it re-derives are the three the repo can answer on its own.

**The anchor is the backtick, and the domain is the anchor** (§V121 f iii). Placing every
digit run in 297 KB of prose is not implementable, and a guard asked to do it gets
hollowed out or deleted. So the parser recognises three anchor kinds --

* ``tests/**/test_*.py N`` -> the file's pytest COLLECTED count, the convention every row
  already matches (a ``def test_`` tally would silently diverge under ``parametrize``);
* ``<module>.py N`` -> the file's line count, resolved under ``src/``;
* ```` `<registered tool>` N ```` -> that tool's :data:`PRESSURE_BY_TOOL` peak frame.

-- and *within an anchor's reach nothing is skipped*: a figure form the parser cannot
place raises :class:`SpecFigureError`, in the same style as
:func:`~arknights_mcp.mcp.cap_pressure.classify_inputs`. A guard that silently ignores an
unparsed number is precisely how the wrong counts shipped, so leniency here would rebuild
the hole this module exists to close.

Coverage is asserted **both ways** (§V121 f iv): every counted peak in
:data:`FRAME_PRESSURE` must appear tagged beside its tool in at least one row, so a tool's
figure cannot go untagged, and :func:`test_the_parser_still_finds_figures` fails if the
extraction ever stops finding them -- a guard that matches nothing passes everything
(§V117).

Three consequences of the anchor rule are corrections the corpus needed anyway, because
each was a figure §V121 (c) already required to name its basis or its unit:

* ```` `<tool>` N ```` is ambiguous on the live corpus: frame bytes in §T217 (c), tool
  DESCRIPTION characters in §T206/§T207. Bare means frame bytes; any other unit is named
  (``ch`` is the corpus's own token). An unrecognised unit word therefore reads as a frame
  claim and *fails*, which is the fail-closed direction.
* a bare module basename is not re-checkable -- ``stage.py`` resolves two ways under
  ``src/``, ``banners.py`` five, ``_shared.py`` three -- so an ambiguous anchor raises.
* a figure that was true at some past commit carries ``@T<n>`` and is pinned in
  :data:`HISTORICAL_FIGURES`. The pin is what stops "mark it historical" from becoming a
  rug: a new exemption cannot appear without an edit here, and an unused pin fails too.

**What this does NOT check** is declared rather than left implied, because an undeclared
blind spot reads as "every figure checked" (§V121 f vi). Out of the parser's domain, all
of it by construction and none of it by accident: percentages and ratios of every kind;
per-shape frames that are not a tool's peak (``214849`` at ``page_size=90``, ``198036``,
the superseded ``199324``); figures measured on a build other than
:data:`~arknights_mcp.mcp.cap_pressure.CAP_PRESSURE_BASIS` (B167's ``220071``/``214789``
on ``2026-08-13T161051Z-en-cn``); payload-byte accounting such as ``73064 of 104559``,
which is a different unit from the frame and is measured two ways in the corpus; gate
totals ("gate 2660 passed"); guard counts written without a file anchor ("19 unit");
description-character figures; timings; and any figure written before its anchor, which
in this corpus means a delta ("N added to `<file>`") rather than a total -- that form is
rejected outright rather than guessed at.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess  # noqa: S404 -- pytest's own collector, on this repo, no shell
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pytest

from arknights_mcp.mcp.cap_pressure import FRAME_PRESSURE, PRESSURE_BY_TOOL

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "SPEC.md"
SRC_ROOT = REPO_ROOT / "src"
TESTS_ROOT = REPO_ROOT / "tests"

#: Units a figure may name. Only the tool-frame kind consults them: a named unit means the
#: number is not a frame claim, and a bare number is. Anything else a writer puts there
#: leaves the figure reading as frame bytes, so it fails rather than escapes.
NAMED_UNITS = frozenset({"ch", "B", "s", "%"})

#: Digit suffixes that make a number prose rather than a figure. Explicit, because
#: ``test_sentinel_answer.py 1st execution`` otherwise parses as a count of one. Any
#: *other* alphabetic suffix raises instead of being guessed at.
ORDINAL_SUFFIXES = frozenset({"st", "nd", "rd", "th"})

#: Trailing punctuation that ends a figure rather than continuing it.
FIGURE_PUNCTUATION = frozenset(",.;:)]")

#: Figures that were true at a past commit and are recorded with an ``@T<n>`` basis
#: instead of being re-derived, because re-deriving would falsify what that task shipped
#: (§V121 c). Pinned so a new exemption needs a decision here, and checked both ways: an
#: unused pin fails, and a basis-marked figure with no pin fails.
HISTORICAL_FIGURES: Mapping[tuple[str, str], int] = {
    # T201 shipped 47; T212 reshaped the file at 160b7a5 and it collects 44 today.
    ("tests/unit/test_window_bounds.py", "T201"): 47,
    # A transient mid-task peak that breached §V38's 800 cap; the split landed it at 790.
    ("mcp/tools/_shared.py", "T195"): 878,
    # 791 at 6bdef3d, the commit the row describes.
    ("mcp/tools/_shared.py", "T196"): 791,
}

#: Floors for :func:`test_the_parser_still_finds_figures`. A regex that quietly stops
#: matching is a guard that cannot fail (§V117), and this file's whole value is that it
#: matches. Floors rather than exact counts so ordinary spec growth does not churn them.
MIN_FIGURES: Mapping[str, int] = {"test_count": 15, "module_lines": 8, "tool_frame": 12}

#: How far ahead of a counted peak the coverage check looks for its tool's name.
COVERAGE_WINDOW = 200


class SpecFigureError(ValueError):
    """A figure inside an anchor's reach that the parser cannot place (§V121 f iii).

    Loud rather than lenient, for the reason
    :class:`~arknights_mcp.mcp.cap_pressure.CapPressureError` is: the lenient version --
    skip what you do not recognise -- is how a wrong figure ships under a green gate.
    """


class FigureKind(Enum):
    """What a backticked anchor makes the number after it a claim about."""

    TEST_COUNT = "test_count"
    MODULE_LINES = "module_lines"
    TOOL_FRAME = "tool_frame"


@dataclass(frozen=True, slots=True)
class Figure:
    """One anchored figure as written in ``SPEC.md``.

    ``value`` is the last number of the chain: ``636→644`` records a change and
    ``47 ⇒ 55`` records a correction, and in both the live claim is the right-hand side.
    ``basis`` is the ``@T<n>`` marker when the figure is historical.
    """

    kind: FigureKind
    anchor: str
    value: int
    basis: str | None
    unit: str | None
    line: int

    def where(self) -> str:
        return f"SPEC.md:{self.line} `{self.anchor}` {self.value}"


_FIGURE = re.compile(
    r"`(?P<anchor>[^`\n]+)`"
    r" (?P<chain>\d+(?: *[→⇒] *\d+)*)"
    r"(?P<glued>[^\s`]*)"
)
_REVERSED = re.compile(r"(?<![\w.])(?P<value>\d+) `(?P<anchor>[^`\n]+\.py)`")
_QUALIFIERS = re.compile(r"(?: @(?P<basis>[A-Za-z0-9_.\-]+))?(?: (?P<unit>[A-Za-z%]+))?")


def classify_anchor(anchor: str) -> FigureKind | None:
    """Which kind of claim a number after ``anchor`` is, or ``None`` when out of domain.

    The out-of-domain answer is the parser's declared boundary, not a shrug: a stage id, a
    payload key or a request shape carries numbers this repo cannot re-derive, and the
    module docstring names every such form. What must never be ``None`` is an anchor that
    *is* one of the three kinds.
    """
    head, _, tail = anchor.rpartition(":")
    if head and tail.isdigit():
        return None  # a `path.py:147` reference, not a claim about the file's size
    if anchor.endswith(".py"):
        name = anchor.rsplit("/", 1)[-1]
        return FigureKind.TEST_COUNT if name.startswith("test_") else FigureKind.MODULE_LINES
    if anchor in PRESSURE_BY_TOOL:
        return FigureKind.TOOL_FRAME
    return None


def parse_figures(text: str) -> list[Figure]:
    """Every anchored figure in ``text``, or a raise on one that cannot be placed."""
    figures: list[Figure] = []
    for match in _FIGURE.finditer(text):
        anchor = match.group("anchor")
        kind = classify_anchor(anchor)
        if kind is None:
            continue
        line = text.count("\n", 0, match.start()) + 1
        glued = match.group("glued")
        if glued in ORDINAL_SUFFIXES:
            continue  # "1st execution" is prose; recognised, not guessed
        if glued and not set(glued) <= FIGURE_PUNCTUATION:
            raise SpecFigureError(
                f"SPEC.md:{line} `{anchor}` {match.group('chain')}{glued}: the parser "
                f"cannot place the suffix {glued!r}. Name the unit or reword -- a figure "
                "form left unread is how the wrong counts shipped (§V121 f iii)"
            )
        chain = [int(part) for part in re.findall(r"\d+", match.group("chain"))]
        qualifiers = _QUALIFIERS.match(text, match.end())
        assert qualifiers is not None  # every group is optional, so this always matches
        unit = qualifiers.group("unit")
        figures.append(
            Figure(
                kind=kind,
                anchor=anchor,
                value=chain[-1],
                basis=qualifiers.group("basis"),
                unit=unit if unit in NAMED_UNITS else None,
                line=line,
            )
        )
    for reversed_match in _REVERSED.finditer(text):
        line = text.count("\n", 0, reversed_match.start()) + 1
        raise SpecFigureError(
            f"SPEC.md:{line} {reversed_match.group('value')} "
            f"`{reversed_match.group('anchor')}`: a figure written BEFORE its anchor is "
            "ambiguous -- in this corpus it has meant a delta ('N added to <file>'), not "
            "the file's total. Say which (§V121 c)"
        )
    return figures


def resolve(anchor: str, root: Path) -> Path:
    """The one file ``anchor`` names under ``root``, or a raise.

    Suffix match, so a row may qualify a path as far as it needs to and no further. Zero
    matches means the figure outlived its file; two or more means the anchor is not
    re-checkable, which is the state ``stage.py``, ``banners.py`` and ``_shared.py`` were
    all in before T218 (a).
    """
    candidates = sorted(
        path for path in root.rglob("*.py") if path.as_posix().endswith(f"/{anchor}")
    )
    if not candidates:
        raise SpecFigureError(
            f"`{anchor}` names no file under {root.relative_to(REPO_ROOT)}/ -- the figure "
            "outlived the file it measures"
        )
    if len(candidates) > 1:
        found = ", ".join(path.relative_to(REPO_ROOT).as_posix() for path in candidates)
        raise SpecFigureError(
            f"`{anchor}` resolves {len(candidates)} ways ({found}): a bare basename is not "
            "re-checkable, so qualify the path (§V121 c/f v)"
        )
    return candidates[0]


@functools.cache
def collected_counts(paths: tuple[str, ...]) -> Mapping[str, int]:
    """``{repo-relative path: pytest collected count}`` for ``paths``, in one subprocess.

    Collection rather than a ``def test_`` tally: ``parametrize`` makes the two diverge,
    and the collected count is the convention every row in the file already matches. This
    only ever *collects*, so a row naming this very file cannot recurse.
    """
    if not paths:
        return {}
    process = subprocess.run(  # noqa: S603 -- fixed argv, no shell, repo-local paths
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            *paths,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, "ARKMCP_SPEC_FIGURE_COLLECT": "1"},
        check=False,
    )
    if process.returncode != 0:
        tail = "\n".join(process.stdout.strip().splitlines()[-8:])
        raise SpecFigureError(f"collecting {', '.join(paths)} failed:\n{tail}")
    counts = dict.fromkeys(paths, 0)
    for line in process.stdout.splitlines():
        path, separator, _ = line.partition("::")
        if separator and path in counts:
            counts[path] += 1
    return counts


def spec_figures() -> Sequence[Figure]:
    return parse_figures(SPEC_PATH.read_text(encoding="utf-8"))


def _of_kind(kind: FigureKind) -> list[Figure]:
    return [figure for figure in spec_figures() if figure.kind is kind]


def _check_historical(figure: Figure, derived: int) -> bool:
    """``True`` when ``figure`` is pinned historical and so not compared against today."""
    if figure.basis is None:
        return False
    pinned = HISTORICAL_FIGURES.get((figure.anchor, figure.basis))
    if pinned is None:
        raise SpecFigureError(
            f"{figure.where()} @{figure.basis}: a figure exempted from re-derivation must "
            f"be pinned in HISTORICAL_FIGURES (it reads {derived} today). An unpinned "
            "basis marker is an exemption nobody decided on (§V121 f v)"
        )
    assert pinned == figure.value, (
        f"{figure.where()} @{figure.basis} disagrees with its HISTORICAL_FIGURES pin "
        f"{pinned}: the pin exists so the exemption cannot drift silently"
    )
    return True


# --- §V121 (f) (ii): the three kinds the repo can re-derive on its own ------------


def test_every_test_count_figure_matches_pytest_collection() -> None:
    figures = _of_kind(FigureKind.TEST_COUNT)
    resolved = {
        figure: resolve(figure.anchor, TESTS_ROOT).relative_to(REPO_ROOT).as_posix()
        for figure in figures
    }
    counts = collected_counts(tuple(sorted(set(resolved.values()))))
    for figure, path in resolved.items():
        collected = counts[path]
        if _check_historical(figure, collected):
            continue
        assert figure.value == collected, (
            f"{figure.where()} claims {figure.value} tests, {path} collects {collected}. "
            "Re-derive it, or name the basis it was true at (§V121 c)"
        )


def test_every_module_line_figure_matches_the_filesystem() -> None:
    for figure in _of_kind(FigureKind.MODULE_LINES):
        path = resolve(figure.anchor, SRC_ROOT)
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if _check_historical(figure, lines):
            continue
        assert figure.value == lines, (
            f"{figure.where()} claims {figure.value} lines, "
            f"{path.relative_to(REPO_ROOT)} has {lines} (§V38's own figures included)"
        )


def test_every_tool_frame_figure_matches_the_counted_pin() -> None:
    for figure in _of_kind(FigureKind.TOOL_FRAME):
        if figure.unit is not None:
            continue  # a named unit says this is not a frame claim (`get_stage` 2881 ch)
        pinned = PRESSURE_BY_TOOL[figure.anchor].peak_frame_bytes
        assert figure.value == pinned, (
            f"{figure.where()} reads as this tool's peak frame, but FRAME_PRESSURE counts "
            f"{pinned} on {PRESSURE_BY_TOOL[figure.anchor].counted[:0] or ''}the promoted "
            "build. Either the figure is stale, or it is a different unit and must name "
            "it (§V121 c/f v)"
        )


# --- §V121 (f) (iv): coverage both ways, and a guard that can still fail ----------


def test_every_counted_peak_is_tagged_in_a_row() -> None:
    # The direction B172 could not have caught by re-deriving what is written: a tool
    # whose counted peak appears in SPEC.md nowhere has a figure that cannot go wrong
    # because it was never made. Same anti-drift shape as §V120 (f) itself.
    text = SPEC_PATH.read_text(encoding="utf-8")
    for row in FRAME_PRESSURE:
        peak = str(row.peak_frame_bytes)
        marker = f"`{row.tool}`"
        tagged = any(
            marker in text[max(0, match.start() - COVERAGE_WINDOW) : match.start()]
            for match in re.finditer(re.escape(peak), text)
        )
        assert tagged, (
            f"FRAME_PRESSURE counts {row.tool} at {peak} and no row in SPEC.md names that "
            "figure beside the tool: the pin is untagged, so the spec can describe this "
            "surface with any number at all (§V121 f iv)"
        )


def test_the_parser_still_finds_figures() -> None:
    # §V117: a guard that matches nothing passes everything. If a regex or an anchor rule
    # is narrowed later, this fails before the checks above start reporting green on an
    # empty set.
    found = {kind.value: len(_of_kind(kind)) for kind in FigureKind}
    for kind, floor in MIN_FIGURES.items():
        assert found[kind] >= floor, (
            f"the parser found {found[kind]} {kind} figures, below the floor {floor}: the "
            "extraction stopped seeing the corpus it is supposed to check"
        )


def test_no_historical_pin_is_stale() -> None:
    used = {(figure.anchor, figure.basis) for figure in spec_figures() if figure.basis is not None}
    unused = set(HISTORICAL_FIGURES) - used
    assert not unused, (
        f"HISTORICAL_FIGURES pins {sorted(unused)} that no figure in SPEC.md claims: an "
        "exemption outliving its figure is an exemption nobody is looking at"
    )


# --- §V121 (f) (iii): every raising arm fired, from synthetic text (§V113 b) ------


def test_an_unplaceable_suffix_raises() -> None:
    with pytest.raises(SpecFigureError, match="cannot place the suffix"):
        parse_figures("guards: `tests/unit/test_cap_shed.py` 20x (driven through ok())")


def test_an_ordinal_is_not_a_figure() -> None:
    # The trap the naive reading falls into: without this rule, "1st" parses as one test.
    assert parse_figures("live-upstream GREEN (`test_sentinel_answer.py` 1st execution)") == []


def test_an_ambiguous_module_basename_raises() -> None:
    with pytest.raises(SpecFigureError, match="resolves 5 ways"):
        resolve("banners.py", SRC_ROOT)


def test_a_module_figure_that_outlived_its_file_raises() -> None:
    with pytest.raises(SpecFigureError, match="names no file"):
        resolve("mcp/tools/_stage_shredder.py", SRC_ROOT)


def test_a_basis_marker_without_a_pin_raises() -> None:
    figure = Figure(
        kind=FigureKind.MODULE_LINES,
        anchor="mcp/shed.py",
        value=999,
        basis="T999",
        unit=None,
        line=1,
    )
    with pytest.raises(SpecFigureError, match="must be pinned in HISTORICAL_FIGURES"):
        _check_historical(figure, derived=137)


def test_a_basis_marked_figure_disagreeing_with_its_pin_fails() -> None:
    figure = Figure(
        kind=FigureKind.TEST_COUNT,
        anchor="tests/unit/test_window_bounds.py",
        value=46,
        basis="T201",
        unit=None,
        line=1,
    )
    with pytest.raises(AssertionError, match="disagrees with its HISTORICAL_FIGURES pin"):
        _check_historical(figure, derived=44)


def test_a_figure_written_before_its_anchor_raises() -> None:
    with pytest.raises(SpecFigureError, match="written BEFORE its anchor"):
        parse_figures("guards 9, ⊥ new files (8 `tests/unit/test_deploy_examples.py`)")


def test_a_path_line_reference_is_not_a_figure_anchor() -> None:
    # B171's own row quotes the wrong figures beside `test_cap_shed.py:92`. A line
    # reference says nothing about the file's size, so it is out of domain by rule.
    assert parse_figures("`tests/unit/test_cap_shed.py:92` 215990→65110 ⇒ 220131→68444") == []


def test_a_chain_claims_its_right_hand_side() -> None:
    # `636→644` records a change and `47 ⇒ 55` a correction; the live claim is the last.
    (figure,) = parse_figures("(§V38 relief: `mcp/tools/stage.py` 636→644 — already near)")
    assert (figure.kind, figure.value) == (FigureKind.MODULE_LINES, 644)


def test_a_tool_figure_with_a_named_unit_is_not_a_frame_claim() -> None:
    (named,) = parse_figures("`get_stage` 2881 ch + a numbered cap")
    (bare,) = parse_figures("`get_stage` 302656 = 151.3% of cap")
    assert (named.unit, bare.unit) == ("ch", None)


def test_a_tool_figure_with_an_unrecognised_unit_reads_as_a_frame_claim() -> None:
    # Fail-closed: inventing a unit word does not exempt a number, it leaves it being
    # checked as frame bytes -- so the mistake surfaces instead of passing.
    (figure,) = parse_figures("`get_stage` 2881 chars + a numbered cap")
    assert figure.unit is None
    assert figure.value != PRESSURE_BY_TOOL["get_stage"].peak_frame_bytes


def test_the_spec_parses_without_an_unplaceable_figure() -> None:
    # The whole-file run of the raising arms above: every anchored form in SPEC.md today
    # is one the parser can place.
    assert spec_figures()
