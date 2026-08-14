"""Cap-aware payload shedding (§V120; B167).

§V22 says what the response cap *is* and how it is measured. §V120 says what an
over-cap response **emits**, which is the clause ``_enforce_cap`` never had: it
dropped the whole ``data`` payload and flipped to ``partial`` regardless of whether
the request carried a knob that could have bounded the payload instead. After T216
re-based the cap onto the full result frame (§V119 e), two legal ``get_banners``
windows started answering with nothing at all -- while ``page_size<=80`` returned the
same rows fine. A client asking for the max window was told nothing exists (B167).

This module is the shrink half of that rule. It is deliberately free of any
``envelopes`` import so the chokepoint can drive it without an import cycle: it works
on a plain ``(payload, limitations)`` pair and asks a caller-supplied predicate whether
a candidate fits. :mod:`arknights_mcp.mcp.envelopes` builds that predicate from
:func:`~arknights_mcp.mcp.envelopes.wire_size`, so the bytes a shed is judged against
are the bytes the result ships.

The shape of a plan (§V120 b):

* it is **ordered**, heaviest-counted part first -- for ``get_banners`` that is
  ``image_refs`` (55-71% of row bytes on every real page) before the rows themselves,
  measured over the promoted build rather than guessed;
* it is **declared per tool** but applied at the ONE ``build_envelope`` chokepoint,
  because a rule rolled out per emit site is a rule that misses the surface nobody
  audits (T196's five per-surface null rollouts);
* each part is shed **whole** at a given depth rather than partially across rows: a
  page where some rows kept their ``image_refs`` and others did not would make an
  absent key mean two different things in one response (§V67).

A step that is not paginated (a client-flagged section) flips the result to
``partial``; a paginated trim stays ``ok`` (§V120 d).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ShedFrame:
    """The part of a response a shed step may rewrite: payload + limitations.

    Limitations ride along because shedding is not only a deletion. ``get_banners``
    hoists ``image_refs_base_url``, the ``image_refs_legend``, and the derived-link
    limitation exactly when the page emits refs (§V63/§V66 -- one predicate, one home);
    a step that removed the refs and left those three behind would ship a base URL for
    paths that are gone and a caveat about links the response does not contain. So a
    step returns both halves and owns the whole edit.
    """

    payload: Mapping[str, object]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class ShedStep:
    """One ordered move in a tool's §V120 shed plan.

    ``depths`` reports how many progressively deeper sheds this part offers for a given
    frame (``0`` = nothing of this part is present to shed, so the step is skipped).
    ``apply`` renders depth ``i``; depth ``0`` is the shallowest shed and each higher
    index must remove strictly more, which is what lets :func:`shed_to_fit` binary-search
    for the shallowest depth that fits instead of scanning every row count.

    ``part`` names what leaves, and is the declared identity a plan is audited by.
    ``paginated`` is the §V120 (d) status split: a trimmed page is a smaller *legal*
    window, so it stays ``ok``; a section the client explicitly asked for and did not
    get is ``partial``. Collapsing both into ``partial`` means nothing, and collapsing
    both into ``ok`` lets a missing section go silent.
    """

    part: str
    paginated: bool
    depths: Callable[[ShedFrame], int]
    apply: Callable[[ShedFrame, int], ShedFrame]


#: A tool's ordered shed plan. Empty = this tool declares none, so an over-cap response
#: falls through to the §V22 fail-closed withhold (the floor, not the first answer).
ShedPlan = tuple[ShedStep, ...]


def _shallowest_fitting(
    step: ShedStep, frame: ShedFrame, depth_count: int, fits: Callable[[ShedFrame], bool]
) -> tuple[ShedFrame, bool]:
    """Binary-search this step for the shallowest depth that fits.

    Returns ``(frame, fitted)``. When no depth fits, the deepest variant is returned so
    the next step in the plan starts from everything this one could shed -- the plan is
    cumulative, not a set of independent attempts.

    The search is sound because ``apply`` is monotone by contract (a deeper index removes
    strictly more, so it cannot grow the frame). Shedding is a rare path, but a linear
    walk down a 100-row page would serialize the payload a hundred times; this does it
    about seven.
    """
    low, high = 0, depth_count - 1
    best: ShedFrame | None = None
    while low <= high:
        mid = (low + high) // 2
        candidate = step.apply(frame, mid)
        if fits(candidate):
            best = candidate
            high = mid - 1
        else:
            low = mid + 1
    if best is not None:
        return best, True
    return step.apply(frame, depth_count - 1), False


def shed_to_fit(
    frame: ShedFrame, plan: ShedPlan, fits: Callable[[ShedFrame], bool]
) -> tuple[ShedFrame, bool] | None:
    """Shrink ``frame`` along ``plan`` until ``fits`` accepts it (§V120 a/b).

    Walks the plan in its declared order, carrying each step's deepest result into the
    next, and returns as soon as a candidate fits. The returned flag is "a
    non-paginated part was shed", which the caller turns into the §V120 (d) status
    split.

    Returns ``None`` when the plan is exhausted and the frame is still over -- the
    caller then falls back to the §V22 fail-closed withhold. That path is the floor for
    a response with no knob to narrow, not the answer to every over-cap reply.
    """
    current = frame
    section_shed = False
    for step in plan:
        depth_count = step.depths(current)
        if depth_count <= 0:
            continue
        current, fitted = _shallowest_fitting(step, current, depth_count, fits)
        section_shed = section_shed or not step.paginated
        if fitted:
            return current, section_shed
    return None
