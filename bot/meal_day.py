"""One day's food, item by item — the pure half.

``/summary`` answers "how much did I eat today" and ``/recent`` answers "did my
save land". Neither answers the question this module exists for: *what* did I
eat on a given day, and what did each thing cost. The per-item macros have been
stored since structured meals landed; nothing ever read them back.

Kept free of Telegram, the database, and configuration so the layout can be
tested directly, the way :mod:`bot.weight_series` and :mod:`bot.monitor_targets`
are. The handler supplies rows and a local-date resolver; everything here is a
function of its arguments.

**Why a list and not a table.** Telegram only aligns columns inside a monospace
block, which is about 30-34 characters wide on a phone. Six columns (name,
amount, calories, P, C, F) would leave roughly twelve characters for the food
name — "Baked chicken…" — so the table you can read costs you the thing you are
scanning for. Two lines per item keeps the full name and still groups the
numbers under it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

#: Same glyphs the meal picker uses, so a day reads like the screen that wrote it.
MEAL_EMOJI = {"breakfast": "🌅", "lunch": "🌞", "dinner": "🌙", "snack": "🍿"}
#: The order a day is read in. Not chronological by log time, deliberately:
#: people log in bursts — a whole morning entered at 2pm — and sorting by the
#: timestamp then puts breakfast after lunch, which reads as an error in the
#: data rather than in the sort. Within a meal type the log order still decides.
MEAL_ORDER = ("breakfast", "lunch", "dinner", "snack")

_MACROS = (("protein_g", "P"), ("carbs_g", "C"), ("fat_g", "F"))

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


class DayInputError(ValueError):
    """A user-facing reason a requested day could not be read."""


@dataclass(frozen=True)
class DayTotals:
    """A day's totals, with the coverage that produced them.

    ``missing`` names the fields for which at least one item had no value.
    Unknown is never folded into zero: a meal logged without macros makes the
    day's protein *unknown*, not smaller, and saying which is the whole point of
    keeping the distinction.
    """

    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float
    missing: frozenset[str]


def parse_day(text: str, today: date) -> date:
    """Resolve a typed day to a date, or raise :class:`DayInputError`.

    Accepts nothing (today), ``today``/``yesterday``, an ISO date, and the two
    short forms people actually type — ``11 aug`` and ``aug 11``. A bare number
    is refused rather than guessed: "11" is the 11th to one person and eleven
    days ago to another.

    Reading is harmless, so any past date is allowed — unlike the check-off
    screens, which only write to today or yesterday. A future date is refused,
    because there is nothing there and an empty answer would look like data loss.
    """
    raw = (text or "").strip().casefold()
    if not raw or raw == "today":
        return today
    if raw == "yesterday":
        return today - timedelta(days=1)

    day = _parse_iso(raw) or _parse_day_month(raw, today)
    if day is None:
        raise DayInputError(
            "Try <code>/meals</code>, <code>/meals yesterday</code>, "
            "<code>/meals 11 aug</code>, or <code>/meals 2026-08-11</code>."
        )
    if day > today:
        raise DayInputError("That day hasn't happened yet.")
    return day


def _parse_iso(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _parse_day_month(raw: str, today: date) -> date | None:
    """``11 aug`` / ``aug 11``, with the year inferred as the most recent one."""
    parts = raw.replace(",", " ").split()
    if len(parts) != 2:
        return None
    number, month_name = parts
    if not number.isdigit():
        number, month_name = month_name, number
    if not number.isdigit():
        return None
    month = _MONTHS.get(month_name[:3])
    if month is None:
        return None
    try:
        candidate = date(today.year, month, int(number))
    except ValueError:
        return None
    # December read in January means last December, not eleven months away.
    if candidate > today:
        try:
            candidate = date(today.year - 1, month, int(number))
        except ValueError:
            return None
    return candidate


def _value(row: Mapping[str, Any], field: str) -> float | None:
    raw = row.get(field)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def day_totals(rows: Sequence[Mapping[str, Any]]) -> DayTotals:
    """Sum known values across meals, recording which fields had gaps."""
    sums = {field: 0.0 for field in ("calories", *(f for f, _ in _MACROS))}
    missing: set[str] = set()
    for row in rows:
        for field in sums:
            value = _value(row, field)
            if value is None:
                missing.add(field)
            else:
                sums[field] += value
    return DayTotals(
        calories=sums["calories"],
        protein_g=sums["protein_g"],
        carbs_g=sums["carbs_g"],
        fat_g=sums["fat_g"],
        missing=frozenset(missing),
    )


def format_number(value: float | None) -> str:
    """A number without a pointless decimal, or an em dash when unknown."""
    if value is None:
        return "—"
    rounded = round(float(value), 1)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:g}"


def _nutrition_line(row: Mapping[str, Any]) -> str:
    """``142 cal · P 23 · C 11 · F 1`` for one item or meal."""
    calories = _value(row, "calories")
    parts = [f"{format_number(calories)} cal"]
    parts.extend(
        f"{label} {format_number(_value(row, field))}" for field, label in _MACROS
    )
    return " · ".join(parts)


def totals_line(totals: DayTotals) -> str:
    """The day's headline numbers, flagged when something was unknown."""
    parts = [f"{format_number(totals.calories)} cal"]
    parts.extend(
        f"{label} {format_number(getattr(totals, field))}"
        for field, label in _MACROS
    )
    line = " · ".join(parts)
    if totals.missing:
        line = f"{line} (known values)"
    return line


def meal_block(
    meal: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    *,
    local_time: str,
    escape,
) -> str:
    """One meal: its heading, then one entry per item with what it cost.

    A meal saved before structured items existed — or one whose children were
    lost — still renders from the header's own description, because a day that
    silently omits a meal is worse than one that shows it without a breakdown.
    """
    meal_type = str(meal.get("meal_type") or "")
    emoji = MEAL_EMOJI.get(meal_type, "🍽️")
    title = escape(meal_type.title() or "Meal")
    lines = [f"{emoji} <b>{title}</b> · {escape(local_time)} — {_nutrition_line(meal)}"]
    if items:
        for item in items:
            lines.append(f" · {escape(str(item.get('display_name') or 'item'))}")
            lines.append(f"   {_nutrition_line(item)}")
    else:
        lines.append(f" · {escape(str(meal.get('food_items') or 'no detail recorded'))}")
    return "\n".join(lines)


def day_heading(day: date, totals: DayTotals, meal_count: int) -> str:
    """``🍽️ Wed 13 Aug 2026`` plus the day's totals, or the empty-day line."""
    stamp = day.strftime("%a %d %b %Y")
    if not meal_count:
        return f"🍽️ <b>{stamp}</b>\nNothing logged."
    meals = "meal" if meal_count == 1 else "meals"
    return (
        f"🍽️ <b>{stamp}</b> — {meal_count} {meals}\n{totals_line(totals)}"
    )


def sort_key(row: Mapping[str, Any], parse_timestamp) -> tuple:
    """Breakfast, lunch, dinner, snack — then log order inside each.

    See :data:`MEAL_ORDER` for why the meal type outranks the clock: a day
    entered in one sitting would otherwise open with whatever was typed first.
    """
    parsed = parse_timestamp(row.get("logged_at"))
    meal_type = str(row.get("meal_type") or "")
    order = MEAL_ORDER.index(meal_type) if meal_type in MEAL_ORDER else len(MEAL_ORDER)
    return (order, parsed or datetime.min, row.get("id") or 0)
