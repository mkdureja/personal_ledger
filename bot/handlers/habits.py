"""
/habits handler — Setup, daily check-off, and yesterday toggle.

/habits        → show today's checklist
/habits setup  → add/remove habits
"""

from __future__ import annotations

import logging
from datetime import timedelta

from telegram import Message, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from .common import AUTH_FILTER, cancel_handler, timeout_handler
from ..keyboards import habit_checklist_keyboard, habit_setup_keyboard
from ..config import CONVERSATION_TIMEOUT, today_local

logger = logging.getLogger(__name__)

# Conversation state for setup
ADDING_HABIT = 0


# ---------------------------------------------------------------------------
# /habits — entry point
# ---------------------------------------------------------------------------
async def habits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Handle /habits and /habits setup."""
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)

    args = context.args or []

    if args and args[0].lower() == "setup":
        return await _show_setup(update.message, context, user.id)

    # Default: show today's checklist
    await show_habits_checklist(update.message, context, user.id)
    return ConversationHandler.END


async def show_habits_checklist(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    target_date=None,
) -> None:
    """Display the habit checklist for a given date."""
    db = context.bot_data["db"]
    if target_date is None:
        target_date = today_local()

    habits = await db.get_active_habits(user_id)
    if not habits:
        await message.reply_text(
            "📋 You have no habits set up yet.\n"
            "Use /habits setup to add some!",
        )
        return

    checked = await db.get_checked_habits(user_id, target_date)
    is_today = target_date == today_local()

    habit_list = [{"id": h["id"], "habit_name": h["habit_name"]} for h in habits]

    checked_count = sum(1 for h in habits if h["id"] in checked)
    total = len(habits)

    await message.reply_text(
        f"✅ **Habits** — {checked_count}/{total} done\n"
        f"Tap to check off:",
        reply_markup=habit_checklist_keyboard(habit_list, checked, target_date, is_today),
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Habit checklist callbacks (check/uncheck/toggle day)
# ---------------------------------------------------------------------------
async def habit_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark a habit as done."""
    query = update.callback_query
    await query.answer()

    # Parse: habit_check_{habit_id}_{date}
    parts = query.data.split("_")
    habit_id = int(parts[2])
    log_date_str = parts[3]

    from datetime import date
    log_date = date.fromisoformat(log_date_str)

    db = context.bot_data["db"]
    user_id = update.effective_user.id

    await db.check_habit(user_id, habit_id, log_date)
    await _refresh_checklist(query, context, user_id, log_date)


async def habit_uncheck_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Un-mark a habit."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    habit_id = int(parts[2])
    log_date_str = parts[3]

    from datetime import date
    log_date = date.fromisoformat(log_date_str)

    db = context.bot_data["db"]
    user_id = update.effective_user.id

    await db.uncheck_habit(user_id, habit_id, log_date)
    await _refresh_checklist(query, context, user_id, log_date)


async def habit_toggle_day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle between today and yesterday."""
    query = update.callback_query
    await query.answer()

    if query.data == "habit_toggle_yesterday":
        target_date = today_local() - timedelta(days=1)
    else:
        target_date = today_local()

    user_id = update.effective_user.id
    await _refresh_checklist(query, context, user_id, target_date)


async def habit_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """No-op callback for label buttons."""
    query = update.callback_query
    await query.answer()


async def _refresh_checklist(query, context, user_id: int, target_date) -> None:
    """Re-render the checklist in-place after a change."""
    db = context.bot_data["db"]
    habits = await db.get_active_habits(user_id)
    checked = await db.get_checked_habits(user_id, target_date)
    is_today = target_date == today_local()

    habit_list = [{"id": h["id"], "habit_name": h["habit_name"]} for h in habits]

    checked_count = sum(1 for h in habits if h["id"] in checked)
    total = len(habits)

    await query.edit_message_text(
        f"✅ **Habits** — {checked_count}/{total} done\n"
        f"Tap to check off:",
        reply_markup=habit_checklist_keyboard(habit_list, checked, target_date, is_today),
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Setup flow — add/remove habits
# ---------------------------------------------------------------------------
async def _show_setup(message: Message, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    """Show current habits with remove buttons and prompt to add."""
    db = context.bot_data["db"]
    habits = await db.get_active_habits(user_id)

    if habits:
        habit_list = [{"id": h["id"], "habit_name": h["habit_name"]} for h in habits]
        await message.reply_text(
            "⚙️ **Habit Setup**\n\n"
            f"You have {len(habits)} active habit{'s' if len(habits) > 1 else ''}.\n"
            "Tap ❌ to remove, or type a new habit name to add:",
            reply_markup=habit_setup_keyboard(habit_list),
            parse_mode="Markdown",
        )
    else:
        await message.reply_text(
            "⚙️ **Habit Setup**\n\n"
            "You have no habits yet. Type a habit name to add one:\n"
            "(e.g., *Meditate*, *Read 30 min*, *No Sugar*)\n\n"
            "Use /cancel when done.",
            parse_mode="Markdown",
        )

    return ADDING_HABIT


async def add_habit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Add a habit from text input."""
    habit_name = update.message.text.strip()
    if not habit_name:
        await update.message.reply_text("❌ Habit name can't be empty.")
        return ADDING_HABIT

    if len(habit_name) > 50:
        await update.message.reply_text("❌ Habit name too long (max 50 chars).")
        return ADDING_HABIT

    db = context.bot_data["db"]
    user_id = update.effective_user.id

    habit_id, reactivated = await db.add_habit(user_id, habit_name)

    if reactivated:
        await update.message.reply_text(f"♻️ Reactivated: *{habit_name}*", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"✅ Added: *{habit_name}*", parse_mode="Markdown")

    # Show updated setup
    return await _show_setup(update.message, context, user_id)


async def remove_habit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove (deactivate) a habit."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    habit_id = int(parts[2])

    db = context.bot_data["db"]
    user_id = update.effective_user.id

    success = await db.deactivate_habit(user_id, habit_id)
    if success:
        habits = await db.get_active_habits(user_id)
        if habits:
            habit_list = [{"id": h["id"], "habit_name": h["habit_name"]} for h in habits]
            await query.edit_message_text(
                f"⚙️ **Habit Setup** — {len(habits)} active\n"
                "Tap ❌ to remove, or type a new habit name:",
                reply_markup=habit_setup_keyboard(habit_list),
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                "⚙️ **Habit Setup**\n\n"
                "No habits left. Type a name to add one, or /cancel.",
                parse_mode="Markdown",
            )


# ---------------------------------------------------------------------------
# ConversationHandler for setup
# ---------------------------------------------------------------------------
habits_setup_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("habits", habits_command, filters=AUTH_FILTER)],
    states={
        ADDING_HABIT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_habit_text),
        ],
        ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, timeout_handler)],
    },
    fallbacks=[cancel_handler],
    conversation_timeout=CONVERSATION_TIMEOUT,
)
