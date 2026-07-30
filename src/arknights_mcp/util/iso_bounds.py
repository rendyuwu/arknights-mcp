"""Window-bound FORM: one home for rendering a since/until bound (§V116; §V37).

A ``since``/``until`` bound is compared as TEXT against a stored ISO column, so the
window's collation is decided by the NOTATION the bound is written in -- not by the
instant it denotes. :func:`datetime.fromisoformat` accepts many notations of the same
instant (``20260101`` basic format, ``2026-W27-1`` week dates, ``20260701T000000``,
a space separator, a trailing ``Z``, a non-UTC offset), and every one of them sorts
differently against the stored form:

* an over-sorting lower bound empties the window -- ``since="20260101"`` sorts ABOVE
  every stored ``2026-…`` value (``-`` 0x2d < ``0`` 0x30), so a wide-open window
  returned zero rows on both windowed tools (B163 arm 1);
* an under-sorting upper bound is silently IGNORED -- ``until="20260101"`` sorts below
  nothing, so the full archive came back for a window the caller believed narrowed
  (B163 arm 2, the worse half: a false inclusion reads as a filtered answer).

So a bound is re-rendered here, once, into the ONE canonical notation the stored
columns use, before it reaches SQL or any guard that predicts what the SQL will match
(§V116 (a)). Parsing a bound is not the same as placing it: shape validation was
already in force through both defects (B48/B49), which is why the render is a separate
step rather than a stricter parse.

Two stored forms exist behind the same shared validator, so the render takes the
column's GRANULARITY as an argument (§V116 (b)): ``announcements.date`` holds
``YYYY-MM-DD`` (day-granular -- no time of day at all), while ``banners.open_time``
holds a full ``YYYY-MM-DDThh:mm:ss+00:00`` timestamp. A bound finer than its column is
coarsened INCLUSIVELY -- a day-granular column cannot place a ``10:00`` bound, so
dropping that whole day would assert an exclusion the data cannot support (§V26) --
and the caller is told which bound was widened (:func:`coarsened_window_bounds`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

#: Collation sentinel appended to an UPPER bound before comparing it against a stored
#: timestamp. ``~`` (0x7e) sorts after every character an ISO-8601 timestamp can carry
#: (``T``, ``+``, ``Z``, ``:``, ``-``, ``.``, digits), so a bound written to a coarser
#: unit than the column includes its own last unit -- ``until="2026-07-28"`` keeps a
#: banner opening at ``2026-07-28T11:00:00+00:00`` -- while a strictly later value is
#: still excluded. Single home (§V37): the banner SQL appends it to the bound it
#: compares, and :func:`~arknights_mcp.models.common.inverted_window_reason` appends it
#: to the same bound when it predicts whether the window can match anything at all. Two
#: copies of this sentinel drifting apart IS B163 arm 3: the guard rejected intra-day
#: windows the SQL would have answered.
UNTIL_UPPER_SENTINEL = "~"

#: Granularity of the stored column a bound is compared against: ``date`` for a
#: day-granular ``YYYY-MM-DD`` column (``announcements.date``), ``datetime`` for a full
#: timestamp (``banners.open_time``).
BoundGranularity = Literal["date", "datetime"]

#: Length of the ``YYYY-MM-DD`` prefix every canonical bound starts with.
_DATE_CHARS = 10


def canonical_iso_bound(value: str) -> str:
    """Re-render one bound in the canonical ISO notation, or raise ``ValueError``.

    Canonical means: extended ISO (``-``/``:`` separators, ``T`` between date and time),
    and an offset-aware value converted to UTC so it collates against the stored
    ``+00:00`` timestamps rather than against its own wall clock. The caller's
    GRANULARITY is preserved -- a date stays a date and a datetime stays a datetime --
    because the granularity carries intent that the column, not this function, decides
    what to do with (:func:`window_bound`).

    A naive datetime is rendered without an offset. That is deliberate: it then sorts as
    a prefix of the equivalent stored ``+00:00`` timestamp, which the ``>=`` lower bound
    and the :data:`UNTIL_UPPER_SENTINEL` upper bound both treat as INCLUSIVE, so an
    unqualified bound never excludes the very instant it names.

    Anything no ISO parse can place (``"july"``, ``"2026-01"``, ``"2026-13-01"``) raises
    ``ValueError`` -- the B48/B49 rejection is unchanged, and rejection stays reserved
    for text that denotes no instant at all (§V116 (a)).
    """
    try:
        # A date-only bound must stay date-only, so this is tried first: it accepts the
        # basic (``20260101``) and week-date (``2026-W27-1``) notations and rejects
        # anything carrying a time.
        return date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.isoformat()


def window_bound(value: str | None, *, granularity: BoundGranularity) -> str | None:
    """One canonical bound for a column of ``granularity`` (§V116 (a)/(b)).

    ``None`` (that side of the window left open) passes through. A bound finer than a
    day-granular column is truncated to its calendar day, which is the INCLUSIVE reading
    for both sides: as a lower bound it keeps that day's rows, and as an upper bound it
    keeps them too. The alternative -- comparing ``2026-07-28T10:00:00`` against a
    ``YYYY-MM-DD`` column verbatim -- dropped the whole boundary day (B163 arm 4).
    """
    if value is None:
        return None
    canonical = canonical_iso_bound(value)
    if granularity == "date":
        return canonical[:_DATE_CHARS]
    return canonical


def canonical_window(
    since: str | None, until: str | None, *, granularity: BoundGranularity
) -> tuple[str | None, str | None]:
    """Both bounds rendered for a column of ``granularity`` (§V116 (a)).

    Called by each windowed service before it queries OR guards, so a caller reaching
    the service directly gets the same collation the model gate produces -- one
    contract, both places, exactly as the §V19 page bounds are validated twice.
    """
    return (
        window_bound(since, granularity=granularity),
        window_bound(until, granularity=granularity),
    )


def coarsened_window_bounds(
    since: str | None, until: str | None, *, granularity: BoundGranularity
) -> tuple[str, ...]:
    """Which of the caller's bounds LOST precision to the column's granularity.

    Names (``"since"``/``"until"``) in a fixed order, so the disclosure that rides them
    is deterministic (§V26). Empty when both bounds fit the column: the widening is
    disclosed only when it actually happened, never as boilerplate on every windowed
    call (§V71 (f) -- a caveat that always fires teaches a client nothing).
    """
    coarsened: list[str] = []
    for name, value in (("since", since), ("until", until)):
        if value is None:
            continue
        if window_bound(value, granularity=granularity) != canonical_iso_bound(value):
            coarsened.append(name)
    return tuple(coarsened)
