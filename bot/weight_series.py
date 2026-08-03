"""Turning a sparse list of weigh-ins into a daily series you can plot.

Body weight is logged by hand, so the record is *sparse by nature*: a day gets
skipped, a scale is out of reach, someone travels. Every consumer here — the
chart, the Home line, the weekly summary — needs a value per day anyway, so the
gap-filling rule has to live in exactly one place and be the same rule
everywhere. That is this module.

Two decisions are worth stating plainly, because both could reasonably have gone
the other way:

**Gaps carry forward, but not indefinitely.** A missed day inherits the last
number actually measured, which is what makes a 7-day average meaningful when
you weigh six times a week. Past :data:`CARRY_LIMIT_DAYS` the carry stops and
the series goes empty until the next real weigh-in. Filling forever would draw a
flat line across a month of silence, and a flat line reads as "weight held
steady" — which is a claim about a body, not about a missing record. The chart
must never make that claim on its own.

**A carried value is never mistaken for a measured one.** Every point says which
it is, so the renderer can show measured days differently, and so a caller can
still count how often the scale was actually used.

This module is deliberately pure: standard library only, no I/O, no Telegram, no
database, no configuration. ``tests/test_weight_series.py`` enforces that by AST,
the same way ``bot.nutrition_resolution`` is enforced — the rule is only useful
if it cannot drift into a handler.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

#: How many days a measurement keeps standing in for the days after it.
#:
#: Ten days covers a holiday or a stretch of forgetting without letting a long
#: silence render as a plateau. Chosen with a household ledger in mind, where
#: weighing is a daily habit and a week-plus gap is genuinely exceptional.
CARRY_LIMIT_DAYS = 10

#: The averaging window. Seven days spans exactly one of every weekday, so the
#: weekend/weekday swing that dominates day-to-day noise cancels out instead of
#: showing up as a trend.
ROLLING_WINDOW_DAYS = 7

#: Accepted range for a body weight, in kg. A typo guard — it exists to catch a
#: missing decimal point (``724``) or a stray minus, not to have an opinion about
#: anybody's body. Everything between these is somebody's real weight.
MIN_WEIGHT_KG = 20.0
MAX_WEIGHT_KG = 400.0

#: The nudge grid offered next to the last known weight. Household scales read to
#: 0.1 kg, so the grid steps in 0.1: a coarser step would only ever match a
#: reading that happened to land on it, which is the opposite of a fast tap.
#: Two steps either side keeps the row to five buttons — wider coverage would
#: crowd the row for days that need typing anyway.
NUDGE_STEP_KG = 0.1
NUDGE_STEPS = (-2, -1, 0, 1, 2)


def nudge_values(
    last_kg: float | None,
    *,
    step_kg: float = NUDGE_STEP_KG,
    steps: Sequence[int] = NUDGE_STEPS,
) -> list[float]:
    """The tappable weights to offer around ``last_kg``, in ascending order.

    The grid is anchored on ``last_kg`` **rounded to the step**, so a typed
    ``72.35`` still produces the clean 72.2 … 72.6 row a scale would show rather
    than an unreadable 72.15 … 72.55. Values outside the accepted range are
    dropped instead of clamped — two buttons logging the same weight would be a
    tap that quietly does something other than it says.

    Empty when there is no previous weight: with nothing to nudge from, the
    caller should ask for a number instead of inventing a starting point.
    """
    if last_kg is None:
        return []
    anchor = round(round(float(last_kg) / step_kg) * step_kg, 2)
    values = [round(anchor + index * step_kg, 2) for index in steps]
    return [
        value for value in values if MIN_WEIGHT_KG <= value <= MAX_WEIGHT_KG
    ]


@dataclass(frozen=True)
class WeightPoint:
    """One day of the series.

    ``weight_kg`` is ``None`` when the day has no usable value at all — either it
    precedes the first weigh-in, or the last one is older than the carry limit.
    """

    day: date
    weight_kg: float | None
    #: True only when the scale was actually read on this day.
    measured: bool = False
    #: For a filled day, the day its value was carried from; ``None`` otherwise.
    carried_from: date | None = None

    @property
    def carried(self) -> bool:
        """Whether this day's value stands in for a day that was not weighed."""
        return self.weight_kg is not None and not self.measured


def _measurements(entries: Iterable[tuple[date, float]]) -> dict[date, float]:
    """Collapse raw ``(day, kg)`` pairs into one value per day.

    The database enforces one row per user per day, so a duplicate here means a
    caller merged two sources. Last one wins rather than raising: a chart that
    refuses to render is worse than a chart that picks a value, and the storage
    layer is where uniqueness is actually guaranteed.
    """
    measured: dict[date, float] = {}
    for day, weight in entries:
        if weight is None:
            continue
        value = float(weight)
        if value <= 0:
            continue
        measured[day] = value
    return measured


def daily_series(
    entries: Iterable[tuple[date, float]],
    start: date,
    end: date,
    *,
    carry_limit_days: int = CARRY_LIMIT_DAYS,
) -> list[WeightPoint]:
    """One :class:`WeightPoint` per day from ``start`` to ``end`` inclusive.

    ``entries`` may be unsorted and may extend outside the window. Measurements
    *before* ``start`` matter and are used: if the last weigh-in was two days
    before the window opens, the window's first days carry it. Dropping them
    would make a chart's left edge depend on where the window happened to be cut.

    Days after the carry limit — and days before the first measurement of all —
    get ``weight_kg=None``. There is no value for them, and inventing one is the
    single thing this module exists to avoid.
    """
    if end < start:
        return []

    measured = _measurements(entries)
    limit = max(0, int(carry_limit_days))

    # The most recent measurement at or before the window opens, if any: this is
    # what the first days of the window carry.
    earlier = [day for day in measured if day < start]
    last_day: date | None = max(earlier) if earlier else None

    points: list[WeightPoint] = []
    day = start
    while day <= end:
        if day in measured:
            last_day = day
            points.append(
                WeightPoint(day=day, weight_kg=measured[day], measured=True)
            )
        elif last_day is not None and (day - last_day).days <= limit:
            points.append(
                WeightPoint(
                    day=day,
                    weight_kg=measured[last_day],
                    measured=False,
                    carried_from=last_day,
                )
            )
        else:
            points.append(WeightPoint(day=day, weight_kg=None))
        day += timedelta(days=1)
    return points


def rolling_average(
    series: Sequence[WeightPoint],
    window: int = ROLLING_WINDOW_DAYS,
) -> list[float | None]:
    """The trailing ``window``-day mean for each point, aligned to ``series``.

    A day with no value of its own gets ``None``: the trend line breaks exactly
    where the data does, so a gap in the record is visible as a gap rather than
    smoothed over by the days around it.

    Early days average fewer than ``window`` values — everything available so
    far. That is the honest reading of "the average so far" and it keeps the line
    starting at the first weigh-in instead of a week later, which would look like
    missing data.
    """
    if window < 1:
        raise ValueError("window must be at least 1 day")

    averages: list[float | None] = []
    for index, point in enumerate(series):
        if point.weight_kg is None:
            averages.append(None)
            continue
        values = [
            other.weight_kg
            for other in series[max(0, index - window + 1) : index + 1]
            if other.weight_kg is not None
        ]
        averages.append(sum(values) / len(values) if values else None)
    return averages


@dataclass(frozen=True)
class WeightTrend:
    """What a weight series says, in the few numbers a summary line needs."""

    latest_kg: float | None = None
    latest_day: date | None = None
    #: True when ``latest_kg`` was measured on ``latest_day`` rather than carried.
    latest_measured: bool = False
    average_kg: float | None = None
    #: The same average one window earlier, for a like-for-like comparison.
    previous_average_kg: float | None = None
    measured_days: int = 0

    @property
    def change_kg(self) -> float | None:
        """Movement between the two windows, or ``None`` without both."""
        if self.average_kg is None or self.previous_average_kg is None:
            return None
        return self.average_kg - self.previous_average_kg


def summarize(
    series: Sequence[WeightPoint],
    window: int = ROLLING_WINDOW_DAYS,
) -> WeightTrend:
    """Reduce a series to its latest value and its window-over-window change.

    The change compares two *averages*, never two individual days: day-to-day
    weight moves on water and timing, so "down 0.4 since yesterday" is mostly
    noise dressed up as progress. Comparing this window's mean to the previous
    window's mean is the smallest honest statement available.
    """
    if not series:
        return WeightTrend()

    known = [point for point in series if point.weight_kg is not None]
    if not known:
        return WeightTrend()

    latest = known[-1]
    current_values = [
        point.weight_kg for point in known[-window:] if point.weight_kg is not None
    ]
    previous_slice = known[-2 * window : -window]
    previous_values = [
        point.weight_kg for point in previous_slice if point.weight_kg is not None
    ]

    return WeightTrend(
        latest_kg=latest.weight_kg,
        latest_day=latest.day,
        latest_measured=latest.measured,
        average_kg=sum(current_values) / len(current_values) if current_values else None,
        previous_average_kg=(
            sum(previous_values) / len(previous_values) if previous_values else None
        ),
        measured_days=sum(1 for point in series if point.measured),
    )


def format_kg(value: float | None) -> str:
    """Render a weight for display: two decimals at most, no trailing zeros."""
    if value is None:
        return "—"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_change(value: float | None) -> str:
    """Render a signed change, with an explicit sign so direction is unmissable."""
    if value is None:
        return "—"
    rendered = format_kg(abs(value))
    if rendered == "0":
        return "no change"
    return f"{'+' if value > 0 else '−'}{rendered} kg"
