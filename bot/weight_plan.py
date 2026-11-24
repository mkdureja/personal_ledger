"""A weight plan: a straight line you mean to follow, and how the scale compares.

A plan is four numbers — a first day, the weight it starts from, a constant daily
change, and a length in days. Everything else is derived: the target on any day,
the finish line, and how a weigh-in compares with the line on the day it was
taken. Storing the rate rather than the finish weight keeps "0.05 kg a day" exact;
the finish is arithmetic, so it can never drift away from the line that reaches it.

Two decisions shape what the comparison is allowed to say:

**A single day's reading is shown, but the verdict prefers the average.** Body
weight moves several tenths of a kilogram a day on water and timing alone, while
a typical plan moves a few hundredths. A day-against-day comparison is reported
because it is what people look at first, but once there are enough weigh-ins the
headline compares the *average of the measured days* with the *average target
over those same days* — a like-for-like pair, so neither side is smoothed over a
window the other did not see.

**Behind by less than the day-to-day swing is not "behind".** Inside
:data:`ON_TRACK_BAND_KG` a reading is reported with its number but without a
warning; past it, the warning is earned. Ahead is never a warning.

Days *carried forward* by :mod:`bot.weight_series` never count as weigh-ins here.
A plan comparison is a claim about what the scale said; a carried value is a
statement about the record, not the body.

Pure by design — standard library and :mod:`bot.weight_series` only, no I/O, no
Telegram, no database. ``tests/test_weight_plan.py`` enforces that by AST, the same
way the series module is enforced, so the chart, Home, the weight receipt and
``/plan`` cannot come to mean four different things by "on track".
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from .weight_series import MAX_WEIGHT_KG, MIN_WEIGHT_KG, format_kg

#: Longest plan accepted. A year covers every plan a person writes down; longer is
#: almost certainly a typo for a weight or a rate.
MAX_PLAN_DAYS = 366

#: Largest daily change accepted, either direction. One kilogram a day is far
#: beyond any deliberate plan, so anything above it is a misplaced decimal point.
MAX_DAILY_CHANGE_KG = 1.0

#: Behind the line by no more than this is inside ordinary day-to-day movement
#: and is not flagged. Chosen against a plan that moves 0.05 kg a day, where a
#: single morning's water swing is several days of plan.
ON_TRACK_BAND_KG = 0.3

#: Measured days needed in the averaging window before the average comparison is
#: shown. Below three, "the average" is one or two readings with a grander name.
MIN_DAYS_FOR_AVERAGE = 3

#: The averaging window, in days, ending at the latest weigh-in.
AVERAGE_WINDOW_DAYS = 7

#: A gap smaller than this rounds to zero at display precision, so it is "on
#: target" rather than "0 kg ahead".
_DISPLAY_EPSILON_KG = 0.005

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


class PlanInputError(ValueError):
    """A user-facing reason a plan could not be read or accepted."""


@dataclass(frozen=True)
class WeightPlan:
    """A straight line from ``start_kg`` on ``start_date``, ``length_days`` long.

    Day 1 is ``start_date`` and sits exactly at ``start_kg``; day ``n`` is
    ``start_kg + daily_change_kg × (n − 1)``. So a 28-day plan from 68.85 kg at
    −0.05 kg/day ends on day 28 at 67.50 kg.
    """

    start_date: date
    start_kg: float
    #: Signed: negative loses weight, positive gains, zero holds.
    daily_change_kg: float
    length_days: int

    @property
    def end_date(self) -> date:
        """The plan's last day (day ``length_days``)."""
        return self.start_date + timedelta(days=self.length_days - 1)

    @property
    def end_kg(self) -> float:
        """The target on the last day."""
        return self._target_for_day_number(self.length_days)

    @property
    def total_change_kg(self) -> float:
        """Signed change from day 1 to the last day."""
        return self.end_kg - self.start_kg

    @property
    def direction(self) -> int:
        """−1 for a loss plan, +1 for a gain plan, 0 for holding steady."""
        if self.daily_change_kg < 0:
            return -1
        if self.daily_change_kg > 0:
            return 1
        return 0

    def day_number(self, day: date) -> int:
        """Day 1 is the start. Zero or negative before it; past the end after."""
        return (day - self.start_date).days + 1

    def contains(self, day: date) -> bool:
        """Whether ``day`` falls inside the plan, both ends included."""
        return self.start_date <= day <= self.end_date

    def target_on(self, day: date) -> float | None:
        """The planned weight on ``day``, or ``None`` outside the plan."""
        if not self.contains(day):
            return None
        return self._target_for_day_number(self.day_number(day))

    def _target_for_day_number(self, number: int) -> float:
        # Rounded to the gram: float arithmetic would otherwise turn 67.5 into
        # 67.50000000000001, and a target is a number someone reads off a scale.
        return round(self.start_kg + self.daily_change_kg * (number - 1), 3)


def make_plan(
    start_date: date,
    start_kg: float,
    daily_change_kg: float,
    length_days: int,
) -> WeightPlan:
    """Build a plan, refusing values no one would mean.

    The bounds are typo guards in the same spirit as the weight range itself:
    they catch a missing decimal point, not an ambitious plan.
    """
    start_kg = float(start_kg)
    daily_change_kg = float(daily_change_kg)
    length_days = int(length_days)
    if not (MIN_WEIGHT_KG <= start_kg <= MAX_WEIGHT_KG):
        raise PlanInputError(
            f"A starting weight must be between {MIN_WEIGHT_KG:g} and "
            f"{MAX_WEIGHT_KG:g} kg."
        )
    if not (1 <= length_days <= MAX_PLAN_DAYS):
        raise PlanInputError(
            f"A plan must run between 1 and {MAX_PLAN_DAYS} days."
        )
    if abs(daily_change_kg) > MAX_DAILY_CHANGE_KG:
        raise PlanInputError(
            f"A daily change above {MAX_DAILY_CHANGE_KG:g} kg looks like a typo — "
            "for example <code>-0.05</code> is 50 g a day."
        )
    plan = WeightPlan(start_date, start_kg, daily_change_kg, length_days)
    if not (MIN_WEIGHT_KG <= plan.end_kg <= MAX_WEIGHT_KG):
        raise PlanInputError("That plan would finish outside a plausible weight.")
    return plan


# ---------------------------------------------------------------------------
# Comparing the scale with the line
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanReading:
    """An actual weight set against the plan's target for the same day(s)."""

    actual_kg: float
    target_kg: float
    #: The day it describes — for an average, the latest day in its window.
    day: date
    #: How many weigh-ins it rests on (1 for a single reading).
    measured_days: int = 1

    @property
    def gap_kg(self) -> float:
        """``actual − target``. Its meaning depends on the plan's direction."""
        return self.actual_kg - self.target_kg


def lead_kg(plan: WeightPlan, reading: PlanReading) -> float:
    """How far ahead of the line ``reading`` is. Negative means behind.

    On a loss plan being *under* the line is ahead; on a gain plan being over it
    is. A hold plan has no ahead — any distance from the line is distance behind.
    """
    gap = reading.gap_kg
    if plan.direction < 0:
        return -gap
    if plan.direction > 0:
        return gap
    return -abs(gap)


#: The four verdicts, best first.
AHEAD = "ahead"
ON_TARGET = "on_target"
CLOSE = "close"
BEHIND = "behind"


def verdict(plan: WeightPlan, reading: PlanReading) -> str:
    """Classify a reading: ahead, on target, close behind, or behind.

    ``close`` is behind by no more than :data:`ON_TRACK_BAND_KG` — inside the
    normal daily swing, so it carries its number but no warning.
    """
    # Judged at the gram, the precision it is displayed at: float subtraction
    # turns 70.2 − 69.9 into 0.30000000000000282, and a reading shown as
    # "0.3 kg behind" must not be classed as past a 0.3 kg band.
    lead = round(lead_kg(plan, reading), 3)
    if abs(lead) < _DISPLAY_EPSILON_KG:
        return ON_TARGET
    if lead > 0:
        return AHEAD
    if -lead <= ON_TRACK_BAND_KG:
        return CLOSE
    return BEHIND


def verdict_phrase(plan: WeightPlan, reading: PlanReading) -> str:
    """One short, signed phrase for a reading: ``✅ 0.05 kg ahead`` and so on."""
    kind = verdict(plan, reading)
    amount = format_kg(abs(lead_kg(plan, reading)))
    if kind == ON_TARGET:
        return "✅ on target"
    if kind == AHEAD:
        return f"✅ {amount} kg ahead"
    if kind == CLOSE:
        return f"🟡 {amount} kg behind (within the daily swing)"
    return f"⚠️ {amount} kg behind"


@dataclass(frozen=True)
class PlanStatus:
    """Everything a screen needs to say where a plan stands on ``today``."""

    plan: WeightPlan
    today: date
    #: ``upcoming`` before day 1, ``running`` through the last day, then ``finished``.
    phase: str
    #: The most recent weigh-in inside the plan (on or before today).
    latest: PlanReading | None
    #: Average of measured days vs the average target over those same days.
    average: PlanReading | None
    #: Weigh-ins inside the plan so far.
    measured_days: int

    @property
    def day_number(self) -> int:
        """Today's day number, clamped to the plan (0 before it starts)."""
        return max(0, min(self.plan.day_number(self.today), self.plan.length_days))

    @property
    def target_today(self) -> float | None:
        return self.plan.target_on(self.today)

    @property
    def days_to_start(self) -> int:
        return max(0, (self.plan.start_date - self.today).days)

    @property
    def change_so_far_kg(self) -> float | None:
        """Latest weigh-in minus the plan's starting weight (signed)."""
        if self.latest is None:
            return None
        return self.latest.actual_kg - self.plan.start_kg

    @property
    def required_daily_kg(self) -> float | None:
        """The daily change that would land exactly on the finish from here.

        Measured from the latest weigh-in, so a reading above the line raises
        the pace needed and one below it lowers it. ``None`` when there is no
        weigh-in yet or no day left to change over.
        """
        if self.latest is None or self.phase == "finished":
            return None
        days_left = (self.plan.end_date - self.latest.day).days
        if days_left <= 0:
            return None
        return (self.plan.end_kg - self.latest.actual_kg) / days_left

    @property
    def headline(self) -> PlanReading | None:
        """The reading a one-line verdict should rest on: the average if earned."""
        return self.average or self.latest


def _measured(entries: Iterable[tuple[date, float]]) -> dict[date, float]:
    """One value per day, ignoring anything that is not a positive number."""
    measured: dict[date, float] = {}
    for day, weight in entries:
        if weight is None:
            continue
        value = float(weight)
        if value > 0:
            measured[day] = value
    return measured


def plan_status(
    plan: WeightPlan,
    entries: Iterable[tuple[date, float]],
    today: date,
) -> PlanStatus:
    """Compare the measured weigh-ins in ``entries`` with ``plan`` as of ``today``.

    ``entries`` may be unsorted and may extend outside the plan; only measured
    days inside the plan and on or before ``today`` are used.
    """
    if today < plan.start_date:
        phase = "upcoming"
    elif today <= plan.end_date:
        phase = "running"
    else:
        phase = "finished"

    last_day = min(today, plan.end_date)
    inside = {
        day: kg
        for day, kg in _measured(entries).items()
        if plan.start_date <= day <= last_day
    }
    if not inside:
        return PlanStatus(plan, today, phase, None, None, 0)

    latest_day = max(inside)
    latest = PlanReading(
        actual_kg=inside[latest_day],
        target_kg=plan.target_on(latest_day),
        day=latest_day,
    )

    window_start = latest_day - timedelta(days=AVERAGE_WINDOW_DAYS - 1)
    window = sorted(day for day in inside if day >= window_start)
    average = None
    if len(window) >= MIN_DAYS_FOR_AVERAGE:
        average = PlanReading(
            actual_kg=sum(inside[day] for day in window) / len(window),
            target_kg=sum(plan.target_on(day) for day in window) / len(window),
            day=latest_day,
            measured_days=len(window),
        )

    return PlanStatus(plan, today, phase, latest, average, len(inside))


def trend_points(
    plan: WeightPlan,
    entries: Iterable[tuple[date, float]],
    today: date,
) -> list[tuple[date, float | None]]:
    """The 7-day trend, drawn *against the plan*, for each day up to the latest weigh-in.

    A plain trailing average lags a sloped line by half its window — on a plan
    losing 0.05 kg a day, about 0.15 kg — so a perfectly on-plan week would plot
    as "behind". Instead each day's value is the plan's target that day plus the
    average gap (actual − target) over the measured days in the trailing window.
    On a flat plan that is exactly the plain average; on a sloped one it is the
    same comparison :func:`plan_status` reports, so the chart and the text can
    never disagree about whether the trend is ahead.

    ``None`` where the window holds fewer than :data:`MIN_DAYS_FOR_AVERAGE`
    weigh-ins, so a gap in the record is a gap in the line.
    """
    last_day = min(today, plan.end_date)
    inside = {
        day: kg
        for day, kg in _measured(entries).items()
        if plan.start_date <= day <= last_day
    }
    if not inside:
        return []

    points: list[tuple[date, float | None]] = []
    day = plan.start_date
    latest = max(inside)
    while day <= latest:
        window_start = day - timedelta(days=AVERAGE_WINDOW_DAYS - 1)
        window = [m for m in inside if window_start <= m <= day]
        if len(window) >= MIN_DAYS_FOR_AVERAGE:
            gap = sum(inside[m] - plan.target_on(m) for m in window) / len(window)
            points.append((day, round(plan.target_on(day) + gap, 3)))
        else:
            points.append((day, None))
        day += timedelta(days=1)
    return points


# ---------------------------------------------------------------------------
# Calorie budget
# ---------------------------------------------------------------------------
#: Energy in one kilogram of body-weight change — the usual rule of thumb. It is
#: an approximation (early water loss moves faster than this), which is exactly
#: why the maintenance figure it is paired with is meant to be re-estimated from
#: real intake against real weight after a week or two, not trusted forever.
KCAL_PER_KG = 7700

#: Typo guards for a stated maintenance figure, matching the schema CHECK.
MIN_MAINTENANCE_KCAL = 1000
MAX_MAINTENANCE_KCAL = 6000


def validated_maintenance_kcal(value: float) -> int:
    """A whole-calorie maintenance figure inside the guards, or PlanInputError."""
    try:
        kcal = int(round(float(value)))
    except (TypeError, ValueError):
        raise PlanInputError("Maintenance must be a number of calories.") from None
    if not (MIN_MAINTENANCE_KCAL <= kcal <= MAX_MAINTENANCE_KCAL):
        raise PlanInputError(
            f"Maintenance must be between {MIN_MAINTENANCE_KCAL:,} and "
            f"{MAX_MAINTENANCE_KCAL:,} kcal — for example "
            "<code>/plan maintenance 2350</code>."
        )
    return kcal


def daily_adjustment_kcal(plan: WeightPlan) -> int:
    """The daily calorie change the plan's rate implies: −385 for −0.05 kg/day."""
    return int(round(plan.daily_change_kg * KCAL_PER_KG))


def daily_budget_kcal(plan: WeightPlan, maintenance_kcal: int) -> int:
    """Calories a day that would move weight along the plan: maintenance + adjustment."""
    return int(maintenance_kcal) + daily_adjustment_kcal(plan)


def budget_phrase(eaten_kcal: int, budget_kcal: int) -> str:
    """``1,200 of 1,965 · 765 left`` — or ``2,100 of 1,965 · 135 over``."""
    left = budget_kcal - eaten_kcal
    tail = f"{left:,} left" if left >= 0 else f"{-left:,} over"
    return f"{eaten_kcal:,} of {budget_kcal:,} · {tail}"


def format_rate(value: float) -> str:
    """A signed daily change, to the gram: ``−0.05 kg/day``."""
    rounded = round(value, 3)
    if rounded == 0:
        return "0 kg/day"
    text = f"{abs(rounded):.3f}".rstrip("0").rstrip(".")
    return f"{'+' if rounded > 0 else '−'}{text} kg/day"


def format_signed_kg(value: float) -> str:
    """A signed weight change for prose: ``−0.15 kg``, ``+0.2 kg``, ``0 kg``."""
    text = format_kg(abs(value))
    if text == "0":
        return "0 kg"
    return f"{'+' if value > 0 else '−'}{text} kg"


# ---------------------------------------------------------------------------
# Reading "/plan new …"
# ---------------------------------------------------------------------------
PLAN_NEW_USAGE = (
    "Start a plan with: <code>/plan new &lt;first day&gt; &lt;start kg&gt; "
    "&lt;kg per day or finish kg&gt; &lt;days&gt;</code>\n"
    "For example <code>/plan new 25 nov 68.85 -0.05 28</code> or "
    "<code>/plan new tomorrow 68.85 67.5 28</code>.\n"
    "A daily change needs its sign: <code>-0.05</code> to lose, "
    "<code>+0.05</code> to gain."
)


def parse_plan_args(args: Sequence[str], today: date) -> WeightPlan:
    """Read ``<first day> <start kg> <rate|finish kg> <days>`` into a plan.

    The last three tokens are numbers; everything before them is the first day,
    so ``25 nov`` and ``2026-11-25`` both work. The third number is read by
    size: below the plausible-weight floor it is a daily change, above it a
    finish weight (from which the constant daily change is derived).

    A daily change must say which way it goes. ``0.05`` alone is refused rather
    than assumed to be a loss: guessing the direction of someone's plan would
    draw a line going the wrong way with full confidence.
    """
    tokens = [token for token in (args or []) if token.strip()]
    if len(tokens) < 4:
        raise PlanInputError(PLAN_NEW_USAGE)

    day_tokens, (start_raw, third_raw, days_raw) = tokens[:-3], tokens[-3:]
    start_day = _parse_plan_day(" ".join(day_tokens), today)
    start_kg = _parse_number(start_raw, "starting weight")
    length = _parse_days(days_raw)

    third = third_raw.strip().lower().removesuffix("/day").removesuffix("kg")
    third_value = _parse_number(third, "daily change or finish weight")
    if abs(third_value) < MIN_WEIGHT_KG:
        if not third.startswith(("-", "+", "−")) and third_value != 0:
            raise PlanInputError(
                f"Is <b>{html.escape(third_raw)}</b> a loss or a gain? Send "
                f"<code>-{html.escape(third)}</code> to lose or "
                f"<code>+{html.escape(third)}</code> to gain per day."
            )
        rate = third_value
    else:
        if length < 2:
            raise PlanInputError("A one-day plan has no line to draw to a finish.")
        rate = round((third_value - start_kg) / (length - 1), 6)
    return make_plan(start_day, start_kg, rate, length)


def _parse_number(raw: str, what: str) -> float:
    cleaned = raw.strip().lower().replace("−", "-")
    for suffix in ("kgs", "kg"):
        cleaned = cleaned.removesuffix(suffix)
    if "." not in cleaned and cleaned.count(",") == 1:
        cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        raise PlanInputError(
            f"I couldn't read <b>{html.escape(raw)}</b> as a {what}.\n"
            f"{PLAN_NEW_USAGE}"
        ) from None
    if value != value or value in (float("inf"), float("-inf")):
        raise PlanInputError(
            f"I couldn't read <b>{html.escape(raw)}</b> as a {what}."
        )
    return value


def _parse_days(raw: str) -> int:
    cleaned = raw.strip().lower()
    for suffix in ("days", "day", "d"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    if not cleaned.isdigit():
        raise PlanInputError(
            f"I couldn't read <b>{html.escape(raw)}</b> as a number of days.\n"
            f"{PLAN_NEW_USAGE}"
        )
    return int(cleaned)


def _parse_plan_day(raw: str, today: date) -> date:
    """``today``, ``tomorrow``, an ISO date, ``25 nov`` or ``nov 25``.

    A plan usually starts soon, either side of today, so a day-and-month is read
    as its *nearest* occurrence: ``25 nov`` typed on 24 Nov is tomorrow, and
    typed on 2 Jan is the November just gone.
    """
    text = raw.strip().casefold()
    if text in ("", "today"):
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    parts = text.replace(",", " ").split()
    if len(parts) == 2:
        number, month_name = parts
        if not number.isdigit():
            number, month_name = month_name, number
        month = _MONTHS.get(month_name[:3])
        if number.isdigit() and month is not None:
            candidates = []
            for year in (today.year - 1, today.year, today.year + 1):
                try:
                    candidates.append(date(year, month, int(number)))
                except ValueError:
                    continue
            if candidates:
                return min(candidates, key=lambda day: abs((day - today).days))
    raise PlanInputError(
        f"I couldn't read <b>{html.escape(raw) or '(nothing)'}</b> as the plan's "
        "first day. "
        "Use <code>today</code>, <code>tomorrow</code>, <code>25 nov</code> or "
        "<code>2026-11-25</code>."
    )
