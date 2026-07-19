"""
/start, /help, /menu handlers.
"""

from __future__ import annotations

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .common import authorized_callback, escape_html, reply_html
from ..keyboards import main_menu_keyboard, analytics_keyboard


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message with overview."""
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)

    first_name = escape_html(user.first_name or "there")
    text = (
        f"👋 Hey {first_name}! Welcome to <b>Ledger</b>.\n\n"
        "I'm your personal logging bot for tracking:\n"
        "📖 <b>Study</b> — subjects, duration, notes\n"
        "🏋️ <b>Gym</b> — exercises, sets, reps, weight\n"
        "🍽️ <b>Diet</b> — meals, calories, and macros\n"
        "✅ <b>Habits</b> — daily check-offs with streaks\n\n"
        "Use /menu for the main menu, or type /help for all commands."
    )
    await reply_html(update.message, text)


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Full command reference."""
    text = (
        "📋 <b>All Commands</b>\n\n"
        "<b>Logging</b>\n"
        "<code>/study</code> — Log a study session\n"
        "<code>/study &lt;subject&gt; &lt;minutes&gt; [notes]</code> — Quick log\n"
        "<code>/gym</code> — Log gym exercises\n"
        "<code>/gym &lt;exercise&gt; &lt;sets&gt; &lt;reps&gt; [weight]</code> — Quick log\n"
        "<code>/diet</code> — Log a meal\n"
        "<code>/diet &lt;meal&gt; &lt;food&gt; [calories] "
        "[p=&lt;g&gt; c=&lt;g&gt; f=&lt;g&gt;]</code> — Quick log\n"
        "<code>/food</code> — Manage saved foods and portions\n"
        "<code>/recipe</code> — Manage saved recipes\n\n"
        "<b>Habits</b>\n"
        "<code>/habits</code> — Check off today's habits\n"
        "<code>/habits setup</code> — Add/remove habits\n\n"
        "<b>Analytics</b>\n"
        "<code>/summary</code> — Today's summary\n"
        "<code>/summary week</code> — Weekly summary\n"
        "<code>/chart study</code> — Study chart (7 days)\n"
        "<code>/chart gym</code> — Gym volume chart\n"
        "<code>/chart diet</code> — Diet calories chart\n"
        "<code>/chart habits</code> — Habit heatmap (14 days)\n"
        "<code>/streak</code> — Current habit streaks\n\n"
        "<b>Other</b>\n"
        "<code>/undo</code> — Delete last log entry (within 24h)\n"
        "<code>/cancel</code> — Cancel current conversation\n"
        "<code>/menu</code> — Main menu\n"
        "<code>/help</code> — This message"
    )
    await reply_html(update.message, text)


# ---------------------------------------------------------------------------
# /menu
# ---------------------------------------------------------------------------
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the main menu keyboard."""
    await reply_html(
        update.message,
        "📋 <b>Main Menu</b> — Pick a category:",
        reply_markup=main_menu_keyboard(),
    )


# ---------------------------------------------------------------------------
# Menu callback handler
# ---------------------------------------------------------------------------
@authorized_callback
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle taps on the main menu InlineKeyboard."""
    query = update.callback_query
    data = query.data or ""
    valid_actions = {
        "menu_study",
        "menu_gym",
        "menu_diet",
        "menu_habits",
        "menu_analytics",
    }
    if data not in valid_actions:
        await query.answer("This menu is no longer valid.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        return

    await query.answer()

    if data == "menu_study":
        await reply_html(
            query.message,
            "📖 <b>Study</b> — Send /study to start logging, or use:\n"
            "<code>/study &lt;subject&gt; &lt;minutes&gt; [notes]</code>",
        )
    elif data == "menu_gym":
        await reply_html(
            query.message,
            "🏋️ <b>Gym</b> — Send /gym to start logging, or use:\n"
            "<code>/gym &lt;exercise&gt; &lt;sets&gt; &lt;reps&gt; [weight]</code>",
        )
    elif data == "menu_diet":
        await reply_html(
            query.message,
            "🍽️ <b>Diet</b> — Send /diet to start logging, or use:\n"
            "<code>/diet &lt;meal&gt; &lt;food&gt; [calories] "
            "[p=&lt;g&gt; c=&lt;g&gt; f=&lt;g&gt;]</code>\n"
            "<code>/diet snack food:apple 1 medium</code>\n"
            "<code>/diet dinner recipe:curry 1 serving</code>\n\n"
            "Manage saved nutrition with /food and /recipe.",
        )
    elif data == "menu_habits":
        # Import here to avoid circular imports
        from .habits import show_habits_checklist
        await show_habits_checklist(query.message, context, update.effective_user.id)
    elif data == "menu_analytics":
        await reply_html(
            query.message,
            "📊 <b>Analytics</b> — Choose a report:",
            reply_markup=analytics_keyboard(),
        )
