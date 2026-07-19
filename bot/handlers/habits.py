"""
/habits handler — Setup, daily check-off, and yesterday toggle.

/habits        → show today's checklist
/habits setup  → add/remove habits
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from telegram import Message, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    ContextTypes,
    filters,
)

from .common import (
    AUTH_FILTER,
    active_conversation_hint,
    activate_conversation,
    authorized_callback,
    cancel_handler,
    conversation_available,
    conversation_is_active,
    escape_html,
    finish_conversation,
    reply_html,
    timeout_handler,
)
from ..keyboards import (
    MAX_ACTIVE_HABITS,
    habit_checklist_keyboard,
    habit_setup_keyboard,
    paginate_habits,
)
from ..config import CONVERSATION_TIMEOUT, today_local

logger = logging.getLogger(__name__)

# Conversation state for setup
ADDING_HABIT = 0

_HABIT_ACTION_RE = re.compile(
    r"^habit_(check|uncheck|c|u)_(\d+)_(\d+)_(\d{4}-\d{2}-\d{2})(?:_p(\d+))?$"
)
_HABIT_TOGGLE_RE = re.compile(
    r"^habit_toggle_(\d+)_(today|yesterday)(?:_p(\d+))?$"
)
_HABIT_PAGE_RE = re.compile(r"^habit_page_(\d+)_(\d{4}-\d{2}-\d{2})_(\d+)$")
_HABIT_NOOP_RE = re.compile(r"^habit_noop_(\d+)_(date|\d+)$")
_HABIT_REMOVE_RE = re.compile(r"^habit_remove_(\d+)_(\d+)(?:_p(\d+))?$")
_HABIT_SETUP_PAGE_RE = re.compile(r"^habit_setup_page_(\d+)_(\d+)$")
_HABIT_SETUP_DONE_RE = re.compile(r"^habit_setup_done_(\d+)$")
_HABIT_SETUP_PROMPT_KEY = "habit_setup_prompt"


def _message_location(message) -> tuple[int, int] | None:
    """Return a stable ``(chat_id, message_id)`` pair when available."""
    chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    if isinstance(chat_id, int) and isinstance(message_id, int):
        return chat_id, message_id
    return None


async def _retire_previous_setup_keyboard(context) -> None:
    """Best-effort retirement of the previously active setup keyboard."""
    previous = context.user_data.pop(_HABIT_SETUP_PROMPT_KEY, None)
    bot = getattr(context, "bot", None)
    if previous is None or bot is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=previous[0],
            message_id=previous[1],
            reply_markup=None,
        )
    except TelegramError:
        logger.debug("Could not retire previous habit setup keyboard", exc_info=True)


def _is_current_setup_callback(update: Update, context) -> bool:
    """Validate that a setup callback belongs to the active prompt and chat."""
    return conversation_is_active(update, context, "habits") and context.user_data.get(
        _HABIT_SETUP_PROMPT_KEY
    ) == _message_location(update.callback_query.message)


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
        if not await conversation_available(update, context, "habits"):
            return ConversationHandler.END
        activate_conversation(update, context, "habits")
        try:
            return await _show_setup(update.message, context, user.id)
        except BaseException:
            finish_conversation(update, context, "habits")
            raise

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
        await reply_html(
            message,
            "📋 You have no habits set up yet.\n"
            "Use /habits setup to add some!",
        )
        return

    checked = await db.get_checked_habits(user_id, target_date)
    is_today = target_date == today_local()

    checked_count = sum(1 for h in habits if h["id"] in checked)
    total = len(habits)
    _page_habits, current_page, page_count = paginate_habits(habits)
    page_note = (
        f"\nShowing page {current_page + 1}/{page_count}."
        if page_count > 1
        else ""
    )

    await reply_html(
        message,
        f"✅ <b>Habits</b> — {checked_count}/{total} done\n"
        f"Tap to check off:{page_note}",
        reply_markup=habit_checklist_keyboard(
            habits, checked, target_date, user_id, is_today
        ),
    )


# ---------------------------------------------------------------------------
# Habit checklist callbacks (check/uncheck/toggle day)
# ---------------------------------------------------------------------------
@authorized_callback
async def habit_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark a habit as done."""
    query = update.callback_query
    user_id = update.effective_user.id
    parsed = await _parse_habit_action(query, user_id, "check")
    if parsed is None:
        return
    habit_id, log_date, page = parsed

    db = context.bot_data["db"]
    if not await _is_active_habit(db, user_id, habit_id):
        await _reject_callback(query, "This habit is no longer active.")
        return

    await query.answer()
    await db.check_habit(user_id, habit_id, log_date)
    await _refresh_checklist(query, context, user_id, log_date, page)


@authorized_callback
async def habit_uncheck_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Un-mark a habit."""
    query = update.callback_query
    user_id = update.effective_user.id
    parsed = await _parse_habit_action(query, user_id, "uncheck")
    if parsed is None:
        return
    habit_id, log_date, page = parsed

    db = context.bot_data["db"]
    if not await _is_active_habit(db, user_id, habit_id):
        await _reject_callback(query, "This habit is no longer active.")
        return

    await query.answer()
    await db.uncheck_habit(user_id, habit_id, log_date)
    await _refresh_checklist(query, context, user_id, log_date, page)


@authorized_callback
async def habit_toggle_day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle between today and yesterday."""
    query = update.callback_query
    user_id = update.effective_user.id
    match = _HABIT_TOGGLE_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This habit button is no longer valid.")
        return
    if int(match.group(1)) != user_id:
        await _reject_callback(
            query, "This habit checklist belongs to another user.", clear_keyboard=False
        )
        return

    if match.group(2) == "yesterday":
        target_date = today_local() - timedelta(days=1)
    else:
        target_date = today_local()
    page = int(match.group(3) or 0)

    await query.answer()
    await _refresh_checklist(query, context, user_id, target_date, page)


@authorized_callback
async def habit_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Move between pages in a legacy oversized habit checklist."""
    query = update.callback_query
    user_id = update.effective_user.id
    match = _HABIT_PAGE_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This habit page button is no longer valid.")
        return
    if int(match.group(1)) != user_id:
        await _reject_callback(
            query, "This habit checklist belongs to another user.", clear_keyboard=False
        )
        return

    try:
        target_date = date.fromisoformat(match.group(2))
    except ValueError:
        await _reject_callback(query, "This habit page has an invalid date.")
        return
    today = today_local()
    if target_date not in {today, today - timedelta(days=1)}:
        await _reject_callback(
            query, "This checklist has expired. Open /habits for a current one."
        )
        return

    await query.answer()
    await _refresh_checklist(
        query, context, user_id, target_date, int(match.group(3))
    )


@authorized_callback
async def habit_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """No-op callback for label buttons."""
    query = update.callback_query
    user_id = update.effective_user.id
    match = _HABIT_NOOP_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This habit button is no longer valid.")
        return
    if int(match.group(1)) != user_id:
        await _reject_callback(
            query, "This habit checklist belongs to another user.", clear_keyboard=False
        )
        return

    target = match.group(2)
    if target != "date":
        db = context.bot_data["db"]
        if not await _is_active_habit(db, user_id, int(target)):
            await _reject_callback(query, "This habit is no longer active.")
            return

    await query.answer()


async def _parse_habit_action(query, user_id: int, expected_action: str):
    """Validate an owned check/uncheck callback and its permitted date."""
    match = _HABIT_ACTION_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This habit button is no longer valid.")
        return None

    action = {"c": "check", "u": "uncheck"}.get(match.group(1), match.group(1))
    if action != expected_action:
        await _reject_callback(query, "This habit button is no longer valid.")
        return None

    owner_id = int(match.group(2))
    if owner_id != user_id:
        await _reject_callback(
            query, "This habit checklist belongs to another user.", clear_keyboard=False
        )
        return None

    try:
        log_date = date.fromisoformat(match.group(4))
    except ValueError:
        await _reject_callback(query, "This habit button has an invalid date.")
        return None

    today = today_local()
    if log_date not in {today, today - timedelta(days=1)}:
        await _reject_callback(
            query, "This checklist has expired. Open /habits for a current one."
        )
        return None

    return int(match.group(3)), log_date, int(match.group(5) or 0)


async def _is_active_habit(db, user_id: int, habit_id: int) -> bool:
    """Return whether a habit is active and owned by the requesting user."""
    habits = await db.get_active_habits(user_id)
    return any(habit["id"] == habit_id for habit in habits)


async def _reject_callback(query, text: str, *, clear_keyboard: bool = True) -> None:
    """Acknowledge an invalid callback and retire its stale keyboard."""
    try:
        await query.answer(text, show_alert=True)
    except TelegramError:
        logger.debug("Could not answer stale habit callback", exc_info=True)

    if clear_keyboard:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            logger.debug("Could not remove stale habit keyboard", exc_info=True)


async def _refresh_checklist(
    query, context, user_id: int, target_date, page: int = 0
) -> None:
    """Re-render the checklist in-place after a change."""
    db = context.bot_data["db"]
    habits = await db.get_active_habits(user_id)
    checked = await db.get_checked_habits(user_id, target_date)
    is_today = target_date == today_local()

    checked_count = sum(1 for h in habits if h["id"] in checked)
    total = len(habits)
    _page_habits, current_page, page_count = paginate_habits(habits, page)
    page_note = (
        f"\nShowing page {current_page + 1}/{page_count}."
        if page_count > 1
        else ""
    )

    try:
        await query.edit_message_text(
            f"✅ <b>Habits</b> — {checked_count}/{total} done\n"
            f"Tap to check off:{page_note}",
            reply_markup=habit_checklist_keyboard(
                habits, checked, target_date, user_id, is_today, current_page
            ),
            parse_mode="HTML",
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
        logger.debug("Habit checklist was already up to date")


# ---------------------------------------------------------------------------
# Setup flow — add/remove habits
# ---------------------------------------------------------------------------
def _setup_view(habits: list[dict], user_id: int, page: int = 0):
    """Build bounded setup text and keyboard, including legacy pagination."""
    _page_habits, current_page, page_count = paginate_habits(habits, page)
    if habits:
        at_limit = len(habits) >= MAX_ACTIVE_HABITS
        limit_note = (
            f"\nMaximum reached ({MAX_ACTIVE_HABITS}); remove habits before adding."
            if at_limit
            else ""
        )
        page_note = (
            f"\nShowing page {current_page + 1}/{page_count}."
            if page_count > 1
            else ""
        )
        text = (
            "⚙️ <b>Habit Setup</b>\n\n"
            f"You have {len(habits)} active habit{'s' if len(habits) != 1 else ''}.\n"
            f"Tap ❌ to remove, or type a new habit name to add:{limit_note}{page_note}"
        )
    else:
        text = (
            "⚙️ <b>Habit Setup</b>\n\n"
            "You have no habits yet. Type a habit name to add one:\n"
            "(e.g., <i>Meditate</i>, <i>Read 30 min</i>, <i>No Sugar</i>)\n\n"
            "Use /cancel when done."
        )
    return text, habit_setup_keyboard(habits, user_id, current_page)


async def _show_setup(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    page: int = 0,
) -> int:
    """Show current habits with remove buttons and prompt to add."""
    db = context.bot_data["db"]
    habits = await db.get_active_habits(user_id)
    await _retire_previous_setup_keyboard(context)

    text, keyboard = _setup_view(habits, user_id, page)
    prompt = await reply_html(message, text, reply_markup=keyboard)

    location = _message_location(prompt)
    if location is not None:
        context.user_data[_HABIT_SETUP_PROMPT_KEY] = location

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

    active_habits = await db.get_active_habits(user_id)
    if any(habit["habit_name"] == habit_name for habit in active_habits):
        await reply_html(
            update.message,
            f"ℹ️ Already active: <b>{escape_html(habit_name)}</b>",
        )
        return ADDING_HABIT

    if len(active_habits) >= MAX_ACTIVE_HABITS:
        await update.message.reply_text(
            f"❌ You can have at most {MAX_ACTIVE_HABITS} active habits. "
            "Remove one before adding another."
        )
        return ADDING_HABIT

    _habit_id, status = await db.add_habit(user_id, habit_name)

    safe_name = escape_html(habit_name)
    if status == "reactivated":
        await reply_html(update.message, f"♻️ Reactivated: <b>{safe_name}</b>")
    elif status == "already_active":
        await reply_html(update.message, f"ℹ️ Already active: <b>{safe_name}</b>")
    else:
        await reply_html(update.message, f"✅ Added: <b>{safe_name}</b>")

    # Show updated setup
    return await _show_setup(update.message, context, user_id)


@authorized_callback
async def remove_habit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove (deactivate) a habit."""
    query = update.callback_query
    user_id = update.effective_user.id
    match = _HABIT_REMOVE_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This remove button is no longer valid.")
        return
    if int(match.group(1)) != user_id:
        await _reject_callback(
            query, "This habit setup belongs to another user.", clear_keyboard=False
        )
        return
    if not _is_current_setup_callback(update, context):
        await _reject_callback(query, "This habit setup has expired.")
        return
    habit_id = int(match.group(2))
    page = int(match.group(3) or 0)

    db = context.bot_data["db"]
    if not await _is_active_habit(db, user_id, habit_id):
        await _reject_callback(query, "This habit is no longer active.")
        return

    success = await db.deactivate_habit(user_id, habit_id)
    if not success:
        await _reject_callback(query, "This habit is no longer active.")
        return

    await query.answer()
    habits = await db.get_active_habits(user_id)
    text, keyboard = _setup_view(habits, user_id, page)
    await query.edit_message_text(
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@authorized_callback
async def habit_setup_page_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Move between pages of legacy habits in the active setup prompt."""
    query = update.callback_query
    user_id = update.effective_user.id
    match = _HABIT_SETUP_PAGE_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This setup page button is no longer valid.")
        if conversation_is_active(update, context, "habits"):
            return ADDING_HABIT
        return ConversationHandler.END
    if int(match.group(1)) != user_id:
        await _reject_callback(
            query, "This habit setup belongs to another user.", clear_keyboard=False
        )
        return ADDING_HABIT
    if not _is_current_setup_callback(update, context):
        await _reject_callback(query, "This habit setup has expired.")
        if conversation_is_active(update, context, "habits"):
            return ADDING_HABIT
        return ConversationHandler.END

    habits = await context.bot_data["db"].get_active_habits(user_id)
    text, keyboard = _setup_view(habits, user_id, int(match.group(2)))
    await query.answer()
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    return ADDING_HABIT


@authorized_callback
async def habit_setup_done_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Close habit setup and show a fresh checklist."""
    query = update.callback_query
    user_id = update.effective_user.id
    match = _HABIT_SETUP_DONE_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This setup button is no longer valid.")
        if conversation_is_active(update, context, "habits"):
            return ADDING_HABIT
        return ConversationHandler.END
    if int(match.group(1)) != user_id:
        await _reject_callback(
            query, "This habit setup belongs to another user.", clear_keyboard=False
        )
        return ADDING_HABIT
    if not _is_current_setup_callback(update, context):
        await _reject_callback(query, "This habit setup has expired.")
        if conversation_is_active(update, context, "habits"):
            return ADDING_HABIT
        return ConversationHandler.END

    await query.answer()
    context.user_data.pop(_HABIT_SETUP_PROMPT_KEY, None)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        logger.debug("Could not retire habit setup keyboard", exc_info=True)
    try:
        await show_habits_checklist(query.message, context, user_id)
    except TelegramError:
        logger.warning("Could not deliver habit checklist", exc_info=True)
    finish_conversation(update, context, "habits")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler for setup
# ---------------------------------------------------------------------------
habits_setup_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("habits", habits_command, filters=AUTH_FILTER)],
    states={
        ADDING_HABIT: [
            CallbackQueryHandler(
                habit_setup_done_callback, pattern=r"^habit_setup_done_"
            ),
            CallbackQueryHandler(
                remove_habit_callback, pattern=r"^habit_remove_"
            ),
            CallbackQueryHandler(
                habit_setup_page_callback, pattern=r"^habit_setup_page_"
            ),
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_habit_text),
        ],
        ConversationHandler.TIMEOUT: [TypeHandler(Update, timeout_handler)],
    },
    fallbacks=[
        cancel_handler,
        CommandHandler("habits", active_conversation_hint, filters=AUTH_FILTER),
    ],
    conversation_timeout=CONVERSATION_TIMEOUT,
)
