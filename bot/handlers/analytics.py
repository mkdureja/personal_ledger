"""Analytics handlers: /summary, /summary week, /chart, /streak."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from telegram import Message, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .common import authorized_callback, escape_html, reply_html
from .. import charts
from ..config import local_date_from_utc, today_local
from ..keyboards import MAX_ACTIVE_HABITS

logger = logging.getLogger(__name__)

_CHART_CATEGORIES = frozenset({"study", "gym", "diet", "habits"})
_ANALYTICS_ACTIONS = frozenset(
    {"analytics_summary", "analytics_week", "analytics_streak"}
    | {f"chart_{category}" for category in _CHART_CATEGORIES}
)
_MAX_DYNAMIC_NAME_LENGTH = 100
_MAX_NAME_LIST_LENGTH = 1_000
_MAX_HTML_MESSAGE_LENGTH = 3_800


def _safe_name(value: object) -> str:
    """Bound and escape a stored display name, including legacy oversized rows."""
    text = str(value)
    if len(text) > _MAX_DYNAMIC_NAME_LENGTH:
        text = text[: _MAX_DYNAMIC_NAME_LENGTH - 1] + "…"
    return escape_html(text)


def _format_name_list(names: list[str]) -> str:
    """Format a deterministic name list without exceeding Telegram limits."""
    rendered: list[str] = []
    length = 0
    for index, name in enumerate(names):
        safe = _safe_name(name)
        separator_length = 2 if rendered else 0
        if rendered and length + separator_length + len(safe) > _MAX_NAME_LIST_LENGTH:
            rendered.append(f"… (+{len(names) - index} more)")
            break
        rendered.append(safe)
        length += separator_length + len(safe)
    return ", ".join(rendered)


async def _reply_html_line_chunks(
    message: Message,
    header: str,
    lines: list[str],
) -> None:
    """Send complete HTML lines in messages safely below Telegram's limit."""
    chunks: list[str] = []
    current = header
    for line in lines:
        candidate = f"{current}\n{line}"
        if len(candidate) > _MAX_HTML_MESSAGE_LENGTH and current != header:
            chunks.append(current)
            current = f"{header} (continued)\n{line}"
        else:
            current = candidate
    chunks.append(current)
    for chunk in chunks:
        await reply_html(message, chunk)


# ---------------------------------------------------------------------------
# /summary
# ---------------------------------------------------------------------------
async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send today's or the last seven days' text summary."""
    db = context.bot_data["db"]
    user_id = update.effective_user.id
    args = context.args or []

    if args and args[0].lower() == "week":
        await _weekly_summary(update.effective_message, db, user_id)
    else:
        await _daily_summary(update.effective_message, db, user_id)


async def _daily_summary(message: Message, db, user_id: int) -> None:
    """Send today's summary across all categories to ``message``."""
    today = today_local()
    lines = [f"📋 <b>Daily Summary</b> — {today.strftime('%b %d, %Y')}\n"]

    study_logs = await db.get_study_logs(user_id, today, today)
    study_entries = [
        row
        for row in study_logs
        if local_date_from_utc(datetime.fromisoformat(row["logged_at"])) == today
    ]
    if study_entries:
        total_min = sum(row["duration_min"] for row in study_entries)
        subjects = sorted(
            {str(row["subject"]) for row in study_entries}, key=str.casefold
        )
        safe_subjects = _format_name_list(subjects)
        lines.append(f"📖 <b>Study</b>: {total_min} min across {safe_subjects}")
    else:
        lines.append("📖 <b>Study</b>: —")

    gym_logs = await db.get_gym_logs(user_id, today, today)
    gym_entries = [
        row
        for row in gym_logs
        if local_date_from_utc(datetime.fromisoformat(row["logged_at"])) == today
    ]
    if gym_entries:
        exercises = sorted(
            {str(row["exercise"]) for row in gym_entries}, key=str.casefold
        )
        safe_exercises = _format_name_list(exercises)
        lines.append(
            f"🏋️ <b>Gym</b>: {len(gym_entries)} exercise(s) — {safe_exercises}"
        )
    else:
        lines.append("🏋️ <b>Gym</b>: —")

    diet_logs = await db.get_diet_logs(user_id, today, today)
    diet_entries = [
        row
        for row in diet_logs
        if local_date_from_utc(datetime.fromisoformat(row["logged_at"])) == today
    ]
    if diet_entries:
        total_cal = sum(
            row["calories"]
            for row in diet_entries
            if row["calories"] is not None
        )
        incomplete = any(row["calories"] is None for row in diet_entries)
        cal_note = " ⚠️ (some meals missing calories)" if incomplete else ""
        lines.append(
            f"🍽️ <b>Diet</b>: {len(diet_entries)} meal(s), {total_cal} cal{cal_note}"
        )
    else:
        lines.append("🍽️ <b>Diet</b>: —")

    habits = await db.get_active_habits(user_id)
    if habits:
        checked = await db.get_checked_habits(user_id, today)
        done = sum(1 for habit in habits if habit["id"] in checked)
        lines.append(f"✅ <b>Habits</b>: {done}/{len(habits)} done")
    else:
        lines.append("✅ <b>Habits</b>: no habits set up")

    await reply_html(message, "\n".join(lines))


async def _weekly_summary(message: Message, db, user_id: int) -> None:
    """Send a summary for the last seven local calendar days."""
    today = today_local()
    week_start = today - timedelta(days=6)
    lines = [
        f"📅 <b>Weekly Summary</b> — "
        f"{week_start.strftime('%b %d')} to {today.strftime('%b %d')}\n"
    ]

    study_logs = await db.get_study_logs(user_id, week_start, today)
    study_entries = [
        row
        for row in study_logs
        if week_start
        <= local_date_from_utc(datetime.fromisoformat(row["logged_at"]))
        <= today
    ]
    if study_entries:
        total_min = sum(row["duration_min"] for row in study_entries)
        subjects = {row["subject"] for row in study_entries}
        lines.append(
            f"📖 <b>Study</b>: {total_min / 60:.1f} hrs total across "
            f"{len(subjects)} subject(s)"
        )
    else:
        lines.append("📖 <b>Study</b>: no sessions this week")

    gym_logs = await db.get_gym_logs(user_id, week_start, today)
    gym_entries = [
        row
        for row in gym_logs
        if week_start
        <= local_date_from_utc(datetime.fromisoformat(row["logged_at"]))
        <= today
    ]
    if gym_entries:
        gym_days = len(
            {
                local_date_from_utc(datetime.fromisoformat(row["logged_at"]))
                for row in gym_entries
            }
        )
        lines.append(
            f"🏋️ <b>Gym</b>: {len(gym_entries)} exercises across {gym_days} day(s)"
        )
    else:
        lines.append("🏋️ <b>Gym</b>: no workouts this week")

    diet_logs = await db.get_diet_logs(user_id, week_start, today)
    diet_entries = [
        row
        for row in diet_logs
        if week_start
        <= local_date_from_utc(datetime.fromisoformat(row["logged_at"]))
        <= today
    ]
    if diet_entries:
        total_cal = sum(
            row["calories"]
            for row in diet_entries
            if row["calories"] is not None
        )
        lines.append(
            f"🍽️ <b>Diet</b>: {total_cal} cal total, ~{total_cal / 7:.0f} cal/day avg"
        )
    else:
        lines.append("🍽️ <b>Diet</b>: no meals logged this week")

    habits = await db.get_active_habits(user_id)
    if habits:
        active_ids = {habit["id"] for habit in habits}
        habit_logs = await db.get_habit_logs_range(user_id, week_start, today)
        total_done = sum(
            1 for row in habit_logs if row["habit_id"] in active_ids
        )
        total_possible = len(active_ids) * 7
        pct = total_done / total_possible * 100 if total_possible else 0
        lines.append(
            f"✅ <b>Habits</b>: {total_done}/{total_possible} "
            f"({pct:.0f}%) completed"
        )
    else:
        lines.append("✅ <b>Habits</b>: no habits set up")

    await reply_html(message, "\n".join(lines))


# ---------------------------------------------------------------------------
# /chart <category>
# ---------------------------------------------------------------------------
async def chart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and send a chart."""
    args = context.args or []
    message = update.effective_message

    if not args:
        await reply_html(
            message,
            "📊 Usage: <code>/chart study</code>, <code>/chart gym</code>, "
            "<code>/chart diet</code>, <code>/chart habits</code>",
        )
        return

    category = args[0].lower()
    if category not in _CHART_CATEGORIES:
        await reply_html(
            message,
            f"❌ Unknown chart category: <b>{escape_html(category)}</b>\n"
            "Use: study, gym, diet, or habits",
        )
        return

    await _send_chart(message, context.bot_data["db"], update.effective_user.id, category)


async def _send_chart(message: Message, db, user_id: int, category: str) -> None:
    """Route a validated category to its shared chart renderer."""
    today = today_local()
    week_start = today - timedelta(days=6)

    if category == "study":
        await _send_study_chart(message, db, user_id, week_start, today)
    elif category == "gym":
        await _send_gym_chart(message, db, user_id, week_start, today)
    elif category == "diet":
        await _send_diet_chart(message, db, user_id, week_start, today)
    else:
        await _send_habits_chart(message, db, user_id, today)


async def _send_study_chart(
    message: Message, db, user_id: int, start, end
) -> None:
    """Generate and send a study chart without blocking the event loop."""
    raw_logs = await db.get_study_logs(user_id, start, end)
    logs = [
        {
            "subject": row["subject"],
            "duration_min": row["duration_min"],
            "local_date": local_date_from_utc(datetime.fromisoformat(row["logged_at"])),
        }
        for row in raw_logs
    ]
    logs = [log for log in logs if start <= log["local_date"] <= end]

    buf = await asyncio.to_thread(charts.study_chart, logs, days=7, end_date=end)
    await message.reply_photo(buf, caption="📖 Study — Last 7 Days")


async def _send_gym_chart(message: Message, db, user_id: int, start, end) -> None:
    """Generate and send a gym chart without blocking the event loop."""
    raw_logs = await db.get_gym_logs(user_id, start, end)
    logs = [
        {
            "sets": row["sets"],
            "reps": row["reps"],
            "weight_kg": row["weight_kg"],
            "local_date": local_date_from_utc(datetime.fromisoformat(row["logged_at"])),
        }
        for row in raw_logs
    ]
    logs = [log for log in logs if start <= log["local_date"] <= end]

    buf = await asyncio.to_thread(charts.gym_chart, logs, days=7, end_date=end)
    await message.reply_photo(buf, caption="🏋️ Gym Volume — Last 7 Days")


async def _send_diet_chart(message: Message, db, user_id: int, start, end) -> None:
    """Generate and send a diet chart without blocking the event loop."""
    raw_logs = await db.get_diet_logs(user_id, start, end)
    logs = [
        {
            "calories": row["calories"],
            "local_date": local_date_from_utc(datetime.fromisoformat(row["logged_at"])),
        }
        for row in raw_logs
    ]
    logs = [log for log in logs if start <= log["local_date"] <= end]

    buf = await asyncio.to_thread(charts.diet_chart, logs, days=7, end_date=end)
    await message.reply_photo(buf, caption="🍽️ Diet — Last 7 Days")


async def _send_habits_chart(message: Message, db, user_id: int, today) -> None:
    """Generate and send a habit heatmap without blocking the event loop."""
    start = today - timedelta(days=13)
    habits = await db.get_active_habits(user_id)
    if not habits:
        await message.reply_text("No active habits to chart.")
        return

    raw_logs = await db.get_habit_logs_range(user_id, start, today)
    logs = [
        {"habit_id": row["habit_id"], "log_date": row["log_date"]}
        for row in raw_logs
    ]

    pages = [
        habits[index : index + MAX_ACTIVE_HABITS]
        for index in range(0, len(habits), MAX_ACTIVE_HABITS)
    ]
    for page_number, page_habits in enumerate(pages, start=1):
        habit_names = [habit["habit_name"] for habit in page_habits]
        habit_ids = [habit["id"] for habit in page_habits]
        buf = await asyncio.to_thread(
            charts.habits_chart,
            habit_names,
            habit_ids,
            logs,
            days=14,
            end_date=today,
        )
        page_note = (
            f" (page {page_number}/{len(pages)})" if len(pages) > 1 else ""
        )
        await message.reply_photo(
            buf, caption=f"✅ Habits — Last 14 Days{page_note}"
        )


# ---------------------------------------------------------------------------
# /streak
# ---------------------------------------------------------------------------
async def streak_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current streaks for all active habits."""
    await _send_streaks(
        update.effective_message,
        context.bot_data["db"],
        update.effective_user.id,
    )


async def _send_streaks(message: Message, db, user_id: int) -> None:
    """Build and send streak output for commands and callbacks."""
    today = today_local()
    habits = await db.get_active_habits(user_id)
    if not habits:
        await message.reply_text("No active habits. Use /habits setup to add some!")
        return

    lines = ["🔥 <b>Current Streaks</b>\n"]
    for habit in habits:
        streak = await db.get_streak(user_id, habit["id"], today)
        if streak == 0:
            emoji = "⬜"
        elif streak < 7:
            emoji = "🔥"
        elif streak < 30:
            emoji = "🔥🔥"
        else:
            emoji = "🔥🔥🔥"
        lines.append(
            f"{emoji} <b>{_safe_name(habit['habit_name'])}</b>: {streak} "
            f"day{'s' if streak != 1 else ''}"
        )

    await _reply_html_line_chunks(message, lines[0], lines[1:])


# ---------------------------------------------------------------------------
# Analytics callback handler (from menu)
# ---------------------------------------------------------------------------
@authorized_callback
async def analytics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle analytics menu button taps without mutating ``Update``."""
    query = update.callback_query
    data = query.data or ""
    if data not in _ANALYTICS_ACTIONS:
        await query.answer("This analytics button is no longer valid.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            logger.debug("Could not retire stale analytics keyboard", exc_info=True)
        return

    await query.answer()
    db = context.bot_data["db"]
    user_id = update.effective_user.id
    message = query.message

    if data == "analytics_summary":
        await _daily_summary(message, db, user_id)
    elif data == "analytics_week":
        await _weekly_summary(message, db, user_id)
    elif data == "analytics_streak":
        await _send_streaks(message, db, user_id)
    else:
        await _send_chart(message, db, user_id, data.removeprefix("chart_"))
