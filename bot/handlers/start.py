"""
/start, /help, /menu handlers.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from ..keyboards import main_menu_keyboard, analytics_keyboard


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message with overview."""
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)

    text = (
        f"👋 Hey {user.first_name or 'there'}! Welcome to **Ledger**.\n\n"
        "I'm your personal logging bot for tracking:\n"
        "📖 **Study** — subjects, duration, notes\n"
        "🏋️ **Gym** — exercises, sets, reps, weight\n"
        "🍽️ **Diet** — meals, food items, calories\n"
        "✅ **Habits** — daily check-offs with streaks\n\n"
        "Use /menu for the main menu, or type /help for all commands."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Full command reference."""
    text = (
        "📋 **All Commands**\n\n"
        "**Logging**\n"
        "`/study` — Log a study session\n"
        "`/study <subject> <minutes> [notes]` — Quick log\n"
        "`/gym` — Log gym exercises\n"
        "`/gym <exercise> <sets> <reps> [weight]` — Quick log\n"
        "`/diet` — Log a meal\n"
        "`/diet <meal> <food> [calories]` — Quick log\n\n"
        "**Habits**\n"
        "`/habits` — Check off today's habits\n"
        "`/habits setup` — Add/remove habits\n\n"
        "**Analytics**\n"
        "`/summary` — Today's summary\n"
        "`/summary week` — Weekly summary\n"
        "`/chart study` — Study chart (7 days)\n"
        "`/chart gym` — Gym volume chart\n"
        "`/chart diet` — Diet calories chart\n"
        "`/chart habits` — Habit heatmap (14 days)\n"
        "`/streak` — Current habit streaks\n\n"
        "**Other**\n"
        "`/undo` — Delete last log entry (within 24h)\n"
        "`/cancel` — Cancel current conversation\n"
        "`/menu` — Main menu\n"
        "`/help` — This message"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /menu
# ---------------------------------------------------------------------------
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the main menu keyboard."""
    await update.message.reply_text(
        "📋 **Main Menu** — Pick a category:",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Menu callback handler
# ---------------------------------------------------------------------------
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle taps on the main menu InlineKeyboard."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_study":
        await query.message.reply_text(
            "📖 **Study** — Send /study to start logging, or use:\n"
            "`/study <subject> <minutes> [notes]`",
            parse_mode="Markdown",
        )
    elif data == "menu_gym":
        await query.message.reply_text(
            "🏋️ **Gym** — Send /gym to start logging, or use:\n"
            "`/gym <exercise> <sets> <reps> [weight]`",
            parse_mode="Markdown",
        )
    elif data == "menu_diet":
        await query.message.reply_text(
            "🍽️ **Diet** — Send /diet to start logging, or use:\n"
            "`/diet <meal> <food> [calories]`",
            parse_mode="Markdown",
        )
    elif data == "menu_habits":
        # Import here to avoid circular imports
        from .habits import show_habits_checklist
        await show_habits_checklist(query.message, context, update.effective_user.id)
    elif data == "menu_analytics":
        await query.message.reply_text(
            "📊 **Analytics** — Choose a report:",
            reply_markup=analytics_keyboard(),
            parse_mode="Markdown",
        )
