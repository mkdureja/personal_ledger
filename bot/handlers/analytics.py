"""
Analytics handlers: /summary, /summary week, /chart, /streak.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from .common import AUTH_FILTER
from ..config import today_local, local_date_from_utc
from .. import charts

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /summary
# ---------------------------------------------------------------------------
async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Today or weekly text summary."""
    db = context.bot_data["db"]
    user_id = update.effective_user.id
    args = context.args or []

    if args and args[0].lower() == "week":
        await _weekly_summary(update, db, user_id)
    else:
        await _daily_summary(update, db, user_id)


async def _daily_summary(update: Update, db, user_id: int) -> None:
    """Today's summary across all categories."""
    today = today_local()
    lines = [f"📋 **Daily Summary** — {today.strftime('%b %d, %Y')}\n"]

    # Study
    study_logs = await db.get_study_logs(user_id, today, today)
    study_entries = [
        row for row in study_logs
        if local_date_from_utc(datetime.fromisoformat(row["logged_at"])) == today
    ]
    if study_entries:
        total_min = sum(r["duration_min"] for r in study_entries)
        subjects = set(r["subject"] for r in study_entries)
        lines.append(f"📖 **Study**: {total_min} min across {', '.join(subjects)}")
    else:
        lines.append("📖 **Study**: —")

    # Gym
    gym_logs = await db.get_gym_logs(user_id, today, today)
    gym_entries = [
        row for row in gym_logs
        if local_date_from_utc(datetime.fromisoformat(row["logged_at"])) == today
    ]
    if gym_entries:
        exercises = set(r["exercise"] for r in gym_entries)
        lines.append(f"🏋️ **Gym**: {len(gym_entries)} exercise(s) — {', '.join(exercises)}")
    else:
        lines.append("🏋️ **Gym**: —")

    # Diet
    diet_logs = await db.get_diet_logs(user_id, today, today)
    diet_entries = [
        row for row in diet_logs
        if local_date_from_utc(datetime.fromisoformat(row["logged_at"])) == today
    ]
    if diet_entries:
        total_cal = sum(r["calories"] for r in diet_entries if r["calories"])
        meal_count = len(diet_entries)
        incomplete = any(r["calories"] is None for r in diet_entries)
        cal_note = f" ⚠️ (some meals missing calories)" if incomplete else ""
        lines.append(f"🍽️ **Diet**: {meal_count} meal(s), {total_cal} cal{cal_note}")
    else:
        lines.append("🍽️ **Diet**: —")

    # Habits
    habits = await db.get_active_habits(user_id)
    if habits:
        checked = await db.get_checked_habits(user_id, today)
        done = sum(1 for h in habits if h["id"] in checked)
        total = len(habits)
        lines.append(f"✅ **Habits**: {done}/{total} done")
    else:
        lines.append("✅ **Habits**: no habits set up")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _weekly_summary(update: Update, db, user_id: int) -> None:
    """Weekly summary (last 7 days)."""
    today = today_local()
    week_start = today - timedelta(days=6)

    lines = [f"📅 **Weekly Summary** — {week_start.strftime('%b %d')} to {today.strftime('%b %d')}\n"]

    # Study
    study_logs = await db.get_study_logs(user_id, week_start, today)
    study_entries = [
        row for row in study_logs
        if week_start <= local_date_from_utc(datetime.fromisoformat(row["logged_at"])) <= today
    ]
    if study_entries:
        total_min = sum(r["duration_min"] for r in study_entries)
        hours = total_min / 60
        subjects = set(r["subject"] for r in study_entries)
        lines.append(f"📖 **Study**: {hours:.1f} hrs total across {len(subjects)} subject(s)")
    else:
        lines.append("📖 **Study**: no sessions this week")

    # Gym
    gym_logs = await db.get_gym_logs(user_id, week_start, today)
    gym_entries = [
        row for row in gym_logs
        if week_start <= local_date_from_utc(datetime.fromisoformat(row["logged_at"])) <= today
    ]
    if gym_entries:
        # Count distinct days
        gym_days = len(set(
            local_date_from_utc(datetime.fromisoformat(r["logged_at"]))
            for r in gym_entries
        ))
        lines.append(f"🏋️ **Gym**: {len(gym_entries)} exercises across {gym_days} day(s)")
    else:
        lines.append("🏋️ **Gym**: no workouts this week")

    # Diet
    diet_logs = await db.get_diet_logs(user_id, week_start, today)
    diet_entries = [
        row for row in diet_logs
        if week_start <= local_date_from_utc(datetime.fromisoformat(row["logged_at"])) <= today
    ]
    if diet_entries:
        total_cal = sum(r["calories"] for r in diet_entries if r["calories"])
        avg_cal = total_cal / 7
        lines.append(f"🍽️ **Diet**: {total_cal} cal total, ~{avg_cal:.0f} cal/day avg")
    else:
        lines.append("🍽️ **Diet**: no meals logged this week")

    # Habits
    habits = await db.get_active_habits(user_id)
    if habits:
        habit_logs = await db.get_habit_logs_range(user_id, week_start, today)
        total_possible = len(habits) * 7
        total_done = len(habit_logs)
        pct = (total_done / total_possible * 100) if total_possible > 0 else 0
        lines.append(f"✅ **Habits**: {total_done}/{total_possible} ({pct:.0f}%) completed")
    else:
        lines.append("✅ **Habits**: no habits set up")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /chart <category>
# ---------------------------------------------------------------------------
async def chart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and send a chart."""
    db = context.bot_data["db"]
    user_id = update.effective_user.id
    args = context.args or []

    if not args:
        await update.message.reply_text(
            "📊 Usage: `/chart study`, `/chart gym`, `/chart diet`, `/chart habits`",
            parse_mode="Markdown",
        )
        return

    category = args[0].lower()
    today = today_local()
    week_start = today - timedelta(days=6)

    if category == "study":
        await _send_study_chart(update, db, user_id, week_start, today)
    elif category == "gym":
        await _send_gym_chart(update, db, user_id, week_start, today)
    elif category == "diet":
        await _send_diet_chart(update, db, user_id, week_start, today)
    elif category == "habits":
        await _send_habits_chart(update, db, user_id, today)
    else:
        await update.message.reply_text(
            f"❌ Unknown chart category: *{category}*\n"
            "Use: study, gym, diet, or habits",
            parse_mode="Markdown",
        )


async def _send_study_chart(update, db, user_id, start, end) -> None:
    """Generate and send study chart."""
    raw_logs = await db.get_study_logs(user_id, start, end)
    logs = [
        {
            "subject": row["subject"],
            "duration_min": row["duration_min"],
            "local_date": local_date_from_utc(datetime.fromisoformat(row["logged_at"])),
        }
        for row in raw_logs
    ]
    # Filter to actual range
    logs = [l for l in logs if start <= l["local_date"] <= end]

    buf = charts.study_chart(logs, days=7, end_date=end)
    await update.message.reply_photo(buf, caption="📖 Study — Last 7 Days")


async def _send_gym_chart(update, db, user_id, start, end) -> None:
    """Generate and send gym chart."""
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
    logs = [l for l in logs if start <= l["local_date"] <= end]

    buf = charts.gym_chart(logs, days=7, end_date=end)
    await update.message.reply_photo(buf, caption="🏋️ Gym Volume — Last 7 Days")


async def _send_diet_chart(update, db, user_id, start, end) -> None:
    """Generate and send diet chart."""
    raw_logs = await db.get_diet_logs(user_id, start, end)
    logs = [
        {
            "calories": row["calories"],
            "local_date": local_date_from_utc(datetime.fromisoformat(row["logged_at"])),
        }
        for row in raw_logs
    ]
    logs = [l for l in logs if start <= l["local_date"] <= end]

    buf = charts.diet_chart(logs, days=7, end_date=end)
    await update.message.reply_photo(buf, caption="🍽️ Diet — Last 7 Days")


async def _send_habits_chart(update, db, user_id, today) -> None:
    """Generate and send habits heatmap."""
    start = today - timedelta(days=13)
    habits = await db.get_active_habits(user_id)

    if not habits:
        await update.message.reply_text("No active habits to chart.")
        return

    habit_names = [h["habit_name"] for h in habits]
    habit_ids = [h["id"] for h in habits]

    raw_logs = await db.get_habit_logs_range(user_id, start, today)
    logs = [
        {
            "habit_id": row["habit_id"],
            "log_date": row["log_date"],
        }
        for row in raw_logs
    ]

    buf = charts.habits_chart(habit_names, habit_ids, logs, days=14, end_date=today)
    await update.message.reply_photo(buf, caption="✅ Habits — Last 14 Days")


# ---------------------------------------------------------------------------
# /streak
# ---------------------------------------------------------------------------
async def streak_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current streaks for all active habits."""
    db = context.bot_data["db"]
    user_id = update.effective_user.id
    today = today_local()

    habits = await db.get_active_habits(user_id)
    if not habits:
        await update.message.reply_text("No active habits. Use /habits setup to add some!")
        return

    lines = ["🔥 **Current Streaks**\n"]
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

        lines.append(f"{emoji} **{habit['habit_name']}**: {streak} day{'s' if streak != 1 else ''}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Analytics callback handler (from menu)
# ---------------------------------------------------------------------------
async def analytics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle analytics menu button taps."""
    query = update.callback_query
    await query.answer()

    db = context.bot_data["db"]
    user_id = update.effective_user.id
    data = query.data

    # Create a fake Update-like object for reuse
    # We need to reply to the original message
    if data == "analytics_summary":
        context.args = []
        # Redirect to summary_command
        update_copy = update
        update_copy.message = query.message
        await _daily_summary(update_copy, db, user_id)
    elif data == "analytics_week":
        update_copy = update
        update_copy.message = query.message
        await _weekly_summary(update_copy, db, user_id)
    elif data == "analytics_streak":
        # Send streak info as a new message
        today = today_local()
        habits = await db.get_active_habits(user_id)
        if not habits:
            await query.message.reply_text("No active habits.")
            return

        lines = ["🔥 **Current Streaks**\n"]
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
            lines.append(f"{emoji} **{habit['habit_name']}**: {streak} day{'s' if streak != 1 else ''}")
        await query.message.reply_text("\n".join(lines), parse_mode="Markdown")

    elif data.startswith("chart_"):
        cat = data.replace("chart_", "")
        today = today_local()
        week_start = today - timedelta(days=6)

        if cat == "study":
            # Create a minimal object to pass to _send_study_chart
            await _send_chart_from_callback(query, db, user_id, cat, week_start, today)
        elif cat == "gym":
            await _send_chart_from_callback(query, db, user_id, cat, week_start, today)
        elif cat == "diet":
            await _send_chart_from_callback(query, db, user_id, cat, week_start, today)
        elif cat == "habits":
            await _send_habits_chart_from_callback(query, db, user_id, today)


async def _send_chart_from_callback(query, db, user_id, category, start, end) -> None:
    """Send chart from a callback query."""
    if category == "study":
        raw_logs = await db.get_study_logs(user_id, start, end)
        logs = [
            {
                "subject": row["subject"],
                "duration_min": row["duration_min"],
                "local_date": local_date_from_utc(datetime.fromisoformat(row["logged_at"])),
            }
            for row in raw_logs
        ]
        logs = [l for l in logs if start <= l["local_date"] <= end]
        buf = charts.study_chart(logs, days=7, end_date=end)
        await query.message.reply_photo(buf, caption="📖 Study — Last 7 Days")

    elif category == "gym":
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
        logs = [l for l in logs if start <= l["local_date"] <= end]
        buf = charts.gym_chart(logs, days=7, end_date=end)
        await query.message.reply_photo(buf, caption="🏋️ Gym Volume — Last 7 Days")

    elif category == "diet":
        raw_logs = await db.get_diet_logs(user_id, start, end)
        logs = [
            {
                "calories": row["calories"],
                "local_date": local_date_from_utc(datetime.fromisoformat(row["logged_at"])),
            }
            for row in raw_logs
        ]
        logs = [l for l in logs if start <= l["local_date"] <= end]
        buf = charts.diet_chart(logs, days=7, end_date=end)
        await query.message.reply_photo(buf, caption="🍽️ Diet — Last 7 Days")


async def _send_habits_chart_from_callback(query, db, user_id, today) -> None:
    """Send habits chart from a callback query."""
    start = today - timedelta(days=13)
    habits = await db.get_active_habits(user_id)
    if not habits:
        await query.message.reply_text("No active habits to chart.")
        return

    habit_names = [h["habit_name"] for h in habits]
    habit_ids = [h["id"] for h in habits]
    raw_logs = await db.get_habit_logs_range(user_id, start, today)
    logs = [{"habit_id": row["habit_id"], "log_date": row["log_date"]} for row in raw_logs]

    buf = charts.habits_chart(habit_names, habit_ids, logs, days=14, end_date=today)
    await query.message.reply_photo(buf, caption="✅ Habits — Last 14 Days")
