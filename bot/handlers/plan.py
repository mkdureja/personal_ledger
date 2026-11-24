"""``/plan`` — a weight plan, and how the scale is tracking against it.

The plan is a straight line (see :mod:`bot.weight_plan`); the weigh-ins are the
ordinary daily weights from ``/weight``. Nothing here records a weight — this
section only *reads* them against the line, so there is no second place a number
could be typed and no way for the plan and the weight log to disagree.

Three surfaces, one arithmetic:

* ``/plan`` (and 📉 Plan on Home) — the status text with the chart under it;
* a line under every ``/weight`` receipt while a plan is running, because the
  morning weigh-in is the moment the comparison matters;
* a line on Home.

Every number comes from :func:`bot.weight_plan.plan_status`, so "ahead" cannot
mean one thing on the chart and another in the receipt.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, timedelta

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .. import charts, weight_plan
from ..config import today_local
from ..keyboards import plan_keyboard
from ..weight_plan import (
    PLAN_NEW_USAGE,
    PlanInputError,
    PlanStatus,
    WeightPlan,
    format_rate,
    format_signed_kg,
    verdict_phrase,
)
from ..weight_series import format_kg
from .common import escape_html, reply_html

logger = logging.getLogger(__name__)

_NO_PLAN_TEXT = (
    "📉 <b>No weight plan yet.</b>\n\n"
    "A plan is a straight line from a starting weight at a steady daily change. "
    "Your daily <code>/weight</code> entries are then compared against it, with a "
    "chart.\n\n" + PLAN_NEW_USAGE
)


def _day(value: date) -> str:
    return value.strftime("%b %d")


def _when(day: date, today: date) -> str:
    delta = (day - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    return _day(day)


def plan_summary_line(plan: WeightPlan) -> str:
    """``68.85 → 67.5 kg at −0.05 kg/day · Nov 25 – Dec 22``."""
    return (
        f"{format_kg(plan.start_kg)} → {format_kg(plan.end_kg)} kg at "
        f"{format_rate(plan.daily_change_kg)} · "
        f"{_day(plan.start_date)} – {_day(plan.end_date)}"
    )


def _progress_line(status: PlanStatus) -> str | None:
    """How much of the planned change has happened, by the latest weigh-in."""
    so_far = status.change_so_far_kg
    if so_far is None:
        return None
    plan = status.plan
    line = (
        f"📊 So far: <b>{format_signed_kg(so_far)}</b> of "
        f"{format_signed_kg(plan.total_change_kg)} planned"
    )
    if plan.total_change_kg:
        share = so_far / plan.total_change_kg * 100
        line += f" ({share:.0f}%)"
    return line


@dataclass(frozen=True)
class BudgetView:
    """The calorie side of the plan screen, read once and rendered once."""

    maintenance_kcal: int
    budget_kcal: int
    adjustment_kcal: int
    eaten_today: int
    incomplete_today: bool
    #: Average known calories over plan days before today that have meals.
    average_kcal: float | None
    logged_days: int


_MAINTENANCE_HINT = (
    "<i>🍽️ Add a daily calorie budget: <code>/plan maintenance 2350</code> "
    "(the calories you'd hold your weight on).</i>"
)


def _budget_formula(budget: BudgetView) -> str:
    """``maintenance 2,350 − 385`` — how the budget was reached."""
    adjustment = budget.adjustment_kcal
    if adjustment == 0:
        return f"maintenance {budget.maintenance_kcal:,}"
    sign = "−" if adjustment < 0 else "+"
    return f"maintenance {budget.maintenance_kcal:,} {sign} {abs(adjustment):,}"


def _budget_lines(status: PlanStatus, budget: BudgetView | None) -> list[str]:
    if budget is None:
        return [_MAINTENANCE_HINT]
    lines: list[str] = []
    if status.phase == "running":
        incomplete = " <i>(a meal has no calories)</i>" if budget.incomplete_today else ""
        lines.append(
            "🍽️ Calories today: <b>"
            f"{weight_plan.budget_phrase(budget.eaten_today, budget.budget_kcal)}"
            f"</b>{incomplete}"
        )
        lines.append(f"<i>Budget {budget.budget_kcal:,} = {_budget_formula(budget)}</i>")
    elif status.phase == "upcoming":
        lines.append(
            f"🍽️ From day 1, aim for about <b>{budget.budget_kcal:,} kcal/day</b> "
            f"({_budget_formula(budget)})."
        )
    if budget.logged_days:
        lines.append(
            f"📒 Plan days with meals logged: {budget.logged_days}, averaging "
            f"<b>{budget.average_kcal:,.0f} kcal</b>"
        )
    return lines


def status_text(status: PlanStatus, budget: BudgetView | None = None) -> str:
    """The whole plan screen, as Telegram HTML."""
    plan, today = status.plan, status.today
    title = f"📉 <b>{plan.length_days}-day plan</b>"
    lines: list[str] = []

    if status.phase == "upcoming":
        lines.append(f"{title} — starts {_when(plan.start_date, today)}")
        lines.append(plan_summary_line(plan))
        lines.append("")
        lines.append(
            f"🎯 Day 1 target: <b>{format_kg(plan.start_kg)} kg</b>. Weigh in each "
            "morning with <code>/weight</code> and this screen fills in."
        )
        lines.extend(_budget_lines(status, budget))
        return "\n".join(lines)

    if status.phase == "finished":
        lines.append(f"{title} — finished {_day(plan.end_date)}")
    else:
        lines.append(f"{title} — day {status.day_number} of {plan.length_days}")
    lines.append(plan_summary_line(plan))
    lines.append("")

    if status.phase == "running":
        lines.append(f"🎯 Target today: <b>{format_kg(status.target_today)} kg</b>")

    latest = status.latest
    if latest is None:
        example = format_kg(status.target_today or plan.start_kg)
        lines.append(
            f"⚖️ No weigh-in inside the plan yet — send <code>/weight {example}</code> "
            "or tap ⚖️ Log weight."
        )
    else:
        label = "Today" if latest.day == today else f"Last weigh-in ({_when(latest.day, today)})"
        target_note = (
            "" if latest.day == today and status.phase == "running"
            else f" vs target {format_kg(latest.target_kg)}"
        )
        lines.append(
            f"⚖️ {label}: <b>{format_kg(latest.actual_kg)} kg</b>{target_note} → "
            f"{verdict_phrase(plan, latest)}"
        )
        if status.phase == "running" and latest.day != today:
            lines.append("<i>Not weighed today yet.</i>")

    if status.average is not None:
        lines.append(
            f"📈 7-day trend: {verdict_phrase(plan, status.average)} "
            f"({status.average.measured_days} weigh-ins)"
        )
    elif latest is not None:
        lines.append(
            f"<i>📈 The 7-day trend appears after "
            f"{weight_plan.MIN_DAYS_FOR_AVERAGE} weigh-ins in a week.</i>"
        )

    progress = _progress_line(status)
    if progress:
        lines.append(progress)

    required = status.required_daily_kg
    if required is not None:
        lines.append(
            f"🏁 To finish at {format_kg(plan.end_kg)} kg: "
            f"{format_rate(required)} from here"
        )
    lines.append("")
    lines.extend(_budget_lines(status, budget))
    if status.phase == "finished":
        lines.append("")
        lines.append("Start the next one with <code>/plan new …</code>.")
    return "\n".join(lines)


async def _load(
    db, user_id: int, today: date
) -> tuple[PlanStatus, list[tuple[date, float]]] | None:
    """The active plan's status and the weigh-ins inside it, or ``None``."""
    plan = await db.get_active_weight_plan(user_id)
    if plan is None:
        return None
    rows = await db.get_weight_logs(user_id, plan.start_date, plan.end_date)
    entries = [(row["log_date"], row["weight_kg"]) for row in rows]
    return weight_plan.plan_status(plan, entries, today), entries


async def _budget_view(db, user_id: int, status: PlanStatus) -> BudgetView | None:
    """Read the calorie side of the plan, or ``None`` when no maintenance is set."""
    maintenance = await db.get_maintenance_kcal(user_id)
    if maintenance is None:
        return None
    plan, today = status.plan, status.today
    eaten, incomplete = await db.get_today_calories(user_id, today)
    # Today is still in progress, so the average covers plan days before it.
    last_full_day = min(today - timedelta(days=1), plan.end_date)
    daily = (
        await db.get_daily_calories(user_id, plan.start_date, last_full_day)
        if last_full_day >= plan.start_date
        else {}
    )
    return BudgetView(
        maintenance_kcal=maintenance,
        budget_kcal=weight_plan.daily_budget_kcal(plan, maintenance),
        adjustment_kcal=weight_plan.daily_adjustment_kcal(plan),
        eaten_today=int(eaten),
        incomplete_today=bool(incomplete),
        average_kcal=(sum(daily.values()) / len(daily)) if daily else None,
        logged_days=len(daily),
    )


async def show_plan(message, context, user_id: int) -> None:
    """Send the plan screen: the status as a caption on the chart."""
    db = context.bot_data["db"]
    today = today_local()
    loaded = await _load(db, user_id, today)
    if loaded is None:
        await reply_html(
            message, _NO_PLAN_TEXT, reply_markup=plan_keyboard(has_plan=False)
        )
        return
    status, entries = loaded
    try:
        budget = await _budget_view(db, user_id, status)
    except Exception:
        logger.warning("Could not read the calorie budget", exc_info=False)
        budget = None
    text = status_text(status, budget)
    try:
        buf = await asyncio.to_thread(
            charts.plan_chart, status.plan, entries, today
        )
    except Exception:
        # The numbers are the substance; a chart that fails to draw must not
        # take them down with it.
        logger.exception("Plan chart failed to render; sending text only")
        await reply_html(message, text, reply_markup=plan_keyboard())
        return
    await message.reply_photo(
        buf,
        caption=text,
        parse_mode=ParseMode.HTML,
        reply_markup=plan_keyboard(),
    )


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/plan`` · ``/plan new <first day> <kg> <rate|finish kg> <days>`` · ``/plan end``."""
    db = context.bot_data["db"]
    user = update.effective_user
    message = update.effective_message
    args = list(context.args or [])
    await db.ensure_user(user.id, user.username, user.first_name)

    if not args:
        await show_plan(message, context, user.id)
        return

    action = args[0].casefold()
    if action in ("new", "start", "set"):
        try:
            plan = weight_plan.parse_plan_args(args[1:], today_local())
        except PlanInputError as exc:
            await reply_html(message, f"❌ {exc}")
            return
        result = await db.start_weight_plan(user.id, plan)
        lines = [
            f"✅ <b>{plan.length_days}-day plan saved.</b>",
            plan_summary_line(plan),
        ]
        replaced = result["replaced"]
        if replaced is not None:
            lines.append(
                f"<i>Your previous plan ({escape_html(plan_summary_line(replaced))}) "
                "is ended and kept in history.</i>"
            )
        await reply_html(message, "\n".join(lines))
        await show_plan(message, context, user.id)
        return

    if action in ("maintenance", "maint"):
        await _maintenance(message, db, user.id, args[1:])
        return

    if action in ("end", "stop"):
        ended = await db.end_weight_plan(user.id)
        await reply_html(
            message,
            "🏁 Plan ended — it stays in history. Start another with "
            "<code>/plan new …</code>."
            if ended
            else "There is no active plan to end.",
        )
        return

    await reply_html(
        message,
        "📉 <b>Weight plan</b>\n"
        "<code>/plan</code> — where you stand, with the chart\n"
        "<code>/plan maintenance 2350</code> — set the calories you maintain on; "
        "the plan turns it into a daily budget\n"
        "<code>/plan end</code> — end the current plan\n\n" + PLAN_NEW_USAGE,
    )


async def _maintenance(message, db, user_id: int, args: list[str]) -> None:
    """``/plan maintenance [kcal|off]`` — show, set, or clear maintenance calories."""
    if not args:
        current = await db.get_maintenance_kcal(user_id)
        if current is None:
            await reply_html(message, f"No maintenance set.\n{_MAINTENANCE_HINT}")
        else:
            await reply_html(
                message,
                f"🍽️ Maintenance: <b>{current:,} kcal</b>. Change it with "
                "<code>/plan maintenance 2400</code>, or "
                "<code>/plan maintenance off</code> to stop showing a budget.",
            )
        return

    raw = args[0].strip().lower()
    if raw in ("off", "clear", "none"):
        await db.set_maintenance_kcal(user_id, None)
        await reply_html(message, "🍽️ Maintenance cleared — no calorie budget is shown.")
        return

    try:
        kcal = weight_plan.validated_maintenance_kcal(
            raw.removesuffix("kcal").removesuffix("cal").replace(",", "")
        )
    except PlanInputError as exc:
        await reply_html(message, f"❌ {exc}")
        return
    await db.set_maintenance_kcal(user_id, kcal)

    lines = [f"🍽️ Maintenance set to <b>{kcal:,} kcal</b>."]
    plan = await db.get_active_weight_plan(user_id)
    if plan is not None:
        adjustment = weight_plan.daily_adjustment_kcal(plan)
        lines.append(
            f"With your plan ({format_rate(plan.daily_change_kg)}), the daily budget "
            f"is <b>{weight_plan.daily_budget_kcal(plan, kcal):,} kcal</b> "
            f"({kcal:,} {'−' if adjustment < 0 else '+'} {abs(adjustment):,})."
        )
    await reply_html(message, "\n".join(lines))


# ---------------------------------------------------------------------------
# Lines other screens borrow
# ---------------------------------------------------------------------------
async def weight_receipt_line(db, user_id: int, today: date) -> str | None:
    """One line under a saved weight, while a plan is upcoming or running.

    Never raises: the weight is already saved by the time this runs, and a
    failure here must not make that save look like it failed.
    """
    try:
        loaded = await _load(db, user_id, today)
    except Exception:
        logger.warning("Could not read the weight plan for a receipt", exc_info=False)
        return None
    if loaded is None:
        return None
    status, _entries = loaded
    plan = status.plan
    if status.phase == "upcoming":
        return (
            f"📉 Your plan starts {_when(plan.start_date, today)} at "
            f"<b>{format_kg(plan.start_kg)} kg</b>."
        )
    if status.phase != "running" or status.latest is None:
        return None
    if status.latest.day != today:
        return None
    return (
        f"📉 Plan day {status.day_number}/{plan.length_days}: target "
        f"<b>{format_kg(status.target_today)} kg</b> → "
        f"{verdict_phrase(plan, status.latest)}"
    )


async def calories_line(db, user_id: int, today: date) -> str | None:
    """``🍽️ Calories today: 1,200 of 1,965 · 765 left`` while a budget applies.

    A budget applies only while a plan is running *and* maintenance is set; any
    other state returns ``None`` so the screens that borrow this line stay
    exactly as they were. Called after a meal is already saved, so it never
    raises — a failed read must not make the save look like it failed.
    """
    try:
        maintenance = await db.get_maintenance_kcal(user_id)
        if maintenance is None:
            return None
        plan = await db.get_active_weight_plan(user_id)
        if plan is None or not plan.contains(today):
            return None
        eaten, _incomplete = await db.get_today_calories(user_id, today)
    except Exception:
        logger.warning("Could not read the calorie budget", exc_info=False)
        return None
    budget = weight_plan.daily_budget_kcal(plan, maintenance)
    return f"🍽️ Calories today: {weight_plan.budget_phrase(int(eaten), budget)}"


async def home_line(db, user_id: int, today: date) -> str | None:
    """A short plan line for Home, or ``None`` with no plan. Never raises."""
    try:
        loaded = await _load(db, user_id, today)
    except Exception:
        logger.warning("Could not read the weight plan for Home", exc_info=False)
        return None
    if loaded is None:
        return None
    status, _entries = loaded
    plan = status.plan
    if status.phase == "upcoming":
        return f"📉 Plan: starts {_when(plan.start_date, today)}"
    if status.phase == "finished":
        return "📉 Plan: finished — /plan for the result"
    line = (
        f"📉 Plan: day {status.day_number}/{plan.length_days} · target "
        f"{format_kg(status.target_today)} kg"
    )
    if status.latest is not None and status.latest.day == today:
        line += f" · {verdict_phrase(plan, status.latest)}"
    return line
