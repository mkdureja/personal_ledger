"""
Common handler utilities: auth filter, error handler, /cancel, /undo, validators.
"""

from __future__ import annotations

import html
import logging
import math
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, TypeVar

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    ConversationHandler,
    filters,
)

from ..config import ALLOWED_USER_IDS

logger = logging.getLogger(__name__)

CallbackResult = TypeVar("CallbackResult")

# ---------------------------------------------------------------------------
# Auth filter — compose into every handler
# ---------------------------------------------------------------------------
AUTH_FILTER = filters.User(user_id=ALLOWED_USER_IDS)

_ACTIVE_CONVERSATION_KEY = "_ledger_active_conversation"
_CONVERSATION_LABELS = {
    "study": "a study session",
    "gym": "a workout",
    "diet": "a meal",
    "habits": "habit setup",
}
_CONVERSATION_DATA_KEYS = {
    "study": ("study_subject", "study_duration"),
    "gym": (
        "gym_exercises",
        "gym_current_exercise",
        "gym_current_sets",
        "gym_current_reps",
        "gym_more_message_id",
    ),
    "diet": (
        "diet_meal_message_id",
        "diet_meal_type",
        "diet_food_items",
        "diet_calories",
    ),
    "habits": ("habit_setup_prompt",),
}
_MAX_UNDO_TEXT_LENGTH = 400
_MAX_UNDO_VALUE_LENGTH = 32


def _conversation_chat_id(update: Update) -> int | None:
    """Extract the chat owning a conversation marker."""
    chat = getattr(update, "effective_chat", None)
    return getattr(chat, "id", None)


def conversation_is_active(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    flow: str,
) -> bool:
    """Return whether ``flow`` owns the current user's marker in this chat."""
    return current_conversation(update, context) == flow


def active_conversation_flow(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Return any guided flow marked active for the current user."""
    active = context.user_data.get(_ACTIVE_CONVERSATION_KEY)
    if not isinstance(active, tuple) or len(active) != 2:
        return None
    return str(active[0])


def current_conversation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> str | None:
    """Return the guided flow active in this chat, if any."""
    active = context.user_data.get(_ACTIVE_CONVERSATION_KEY)
    if not isinstance(active, tuple) or len(active) != 2:
        return None
    active_flow, active_chat_id = active
    if active_chat_id != _conversation_chat_id(update):
        return None
    return str(active_flow)


def escape_html(value: object) -> str:
    """Escape a dynamic value for inclusion in a Telegram HTML message."""
    return html.escape(str(value))


def _bounded_html(value: object, max_chars: int) -> str:
    """Escape a legacy value after bounding its contribution to a message."""
    text = str(value)
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return escape_html(text)


async def conversation_available(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    flow: str,
) -> bool:
    """Return whether ``flow`` may start without overlapping another flow."""
    active = context.user_data.get(_ACTIVE_CONVERSATION_KEY)
    if active is None or conversation_is_active(update, context, flow):
        return True

    active_flow = active[0] if isinstance(active, tuple) else str(active)
    label = _CONVERSATION_LABELS.get(active_flow, "another guided log")
    await update.effective_message.reply_text(
        f"⏳ You're already in {label}. Finish it or use /cancel in the chat "
        "where it started."
    )
    return False


def activate_conversation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    flow: str,
) -> None:
    """Mark one guided flow as active for this user."""
    context.user_data[_ACTIVE_CONVERSATION_KEY] = (
        flow,
        _conversation_chat_id(update),
    )


def finish_conversation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    flow: str | None = None,
) -> bool:
    """Release the active-flow marker without disturbing another flow's data."""
    active = context.user_data.get(_ACTIVE_CONVERSATION_KEY)
    if not isinstance(active, tuple):
        return False
    active_flow, active_chat_id = active
    if active_chat_id != _conversation_chat_id(update):
        return False
    if flow is None or active_flow == flow:
        context.user_data.pop(_ACTIVE_CONVERSATION_KEY, None)
        for key in _CONVERSATION_DATA_KEYS.get(active_flow, ()):
            context.user_data.pop(key, None)
        return True
    return False


async def reply_html(message: Any, text: str, **kwargs: Any) -> Any:
    """Reply with trusted HTML markup and an explicit Telegram parse mode.

    Callers must pass dynamic values through :func:`escape_html` before
    interpolating them into ``text``.
    """
    kwargs["parse_mode"] = ParseMode.HTML
    return await message.reply_text(text, **kwargs)


def authorized_callback(
    handler: Callable[..., Awaitable[CallbackResult]],
) -> Callable[..., Awaitable[CallbackResult | None]]:
    """Reject inline-button presses from users outside the configured allowlist."""

    @wraps(handler)
    async def wrapped(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args: Any, **kwargs: Any
    ) -> CallbackResult | None:
        user = update.effective_user
        if user is None or user.id not in ALLOWED_USER_IDS:
            query = update.callback_query
            if query is not None:
                # Acknowledge the press to stop Telegram's loading spinner, but
                # preserve the repository's silent-denial access-control policy.
                await query.answer()
            return None
        return await handler(update, context, *args, **kwargs)

    return wrapped

# ---------------------------------------------------------------------------
# Input validators
# ---------------------------------------------------------------------------


def parse_int(
    text: str,
    field_name: str,
    *,
    max_value: int | None = None,
) -> tuple[int | None, str | None]:
    """Parse a positive integer from text.

    Returns (value, None) on success or (None, error_message) on failure.
    """
    try:
        val = int(text.strip())
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None, f"❌ Please enter a valid number for {field_name}."
    if val <= 0:
        return None, f"❌ {field_name} must be a positive number."
    if max_value is not None and val > max_value:
        return None, f"❌ {field_name} must be {max_value} or less."
    return val, None


def parse_float(
    text: str,
    field_name: str,
    *,
    max_value: float | None = None,
) -> tuple[float | None, str | None]:
    """Parse a positive float from text.

    Returns (value, None) on success or (None, error_message) on failure.
    """
    try:
        val = float(text.strip())
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None, f"❌ Please enter a valid number for {field_name}."
    if not math.isfinite(val):
        return None, f"❌ Please enter a finite number for {field_name}."
    if val <= 0:
        return None, f"❌ {field_name} must be a positive number."
    if max_value is not None and val > max_value:
        return None, f"❌ {field_name} must be {max_value:g} or less."
    return val, None


# ---------------------------------------------------------------------------
# /cancel — fallback for all ConversationHandlers
# ---------------------------------------------------------------------------
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the current conversation."""
    cleared = finish_conversation(update, context)
    text = "✖️ Cancelled." if cleared else "ℹ️ No active guided log in this chat."
    try:
        await update.message.reply_text(text)
    except TelegramError:
        logger.warning("Could not deliver conversation cancellation", exc_info=True)
    return ConversationHandler.END


cancel_handler = CommandHandler("cancel", cancel_command)


async def active_conversation_hint(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Explain why a guided command cannot re-enter its current conversation."""
    flow = active_conversation_flow(context)
    label = _CONVERSATION_LABELS.get(flow or "", "a guided log")
    await update.effective_message.reply_text(
        f"⏳ You're already in {label}. Finish it or use /cancel in this chat."
    )


# ---------------------------------------------------------------------------
# Conversation timeout handler
# ---------------------------------------------------------------------------
async def timeout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Called when a conversation times out."""
    finish_conversation(update, context)
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⏰ Timed out. Send the command again to start over."
            )
        except TelegramError:
            logger.warning("Could not deliver conversation timeout", exc_info=True)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /undo — delete most recent log entry
# ---------------------------------------------------------------------------
async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the most recent log entry (within 24h)."""
    if active_conversation_flow(context) is not None:
        await update.message.reply_text(
            "⏳ Finish the current guided log or use /cancel in the chat where "
            "it started before /undo."
        )
        return

    db = context.bot_data["db"]
    user_id = update.effective_user.id

    entry = await db.undo_last(user_id)
    if entry is None:
        await update.message.reply_text(
            "🤷 Nothing to undo — no entries in the last 24 hours."
        )
        return

    category = _bounded_html(entry.get("category", "Unknown"), 64)
    lines = [f"↩️ <b>Undone:</b> {category}"]

    # Build detail based on category
    if "subject" in entry:
        subject = _bounded_html(entry["subject"], _MAX_UNDO_TEXT_LENGTH)
        duration = _bounded_html(entry["duration_min"], _MAX_UNDO_VALUE_LENGTH)
        lines.append(f"📖 {subject} — {duration} min")
    elif "exercise" in entry:
        exercise = _bounded_html(entry["exercise"], _MAX_UNDO_TEXT_LENGTH)
        sets = _bounded_html(entry["sets"], _MAX_UNDO_VALUE_LENGTH)
        reps = _bounded_html(entry["reps"], _MAX_UNDO_VALUE_LENGTH)
        w = (
            f" @ {_bounded_html(entry['weight_kg'], _MAX_UNDO_VALUE_LENGTH)}kg"
            if entry.get("weight_kg") is not None
            else " (bodyweight)"
        )
        lines.append(f"🏋️ {exercise} — {sets}×{reps}{w}")
    elif "food_items" in entry:
        meal_type = _bounded_html(entry["meal_type"], _MAX_UNDO_VALUE_LENGTH)
        food_items = _bounded_html(entry["food_items"], _MAX_UNDO_TEXT_LENGTH)
        cal = (
            f" — {_bounded_html(entry['calories'], _MAX_UNDO_VALUE_LENGTH)} cal"
            if entry.get("calories") is not None
            else ""
        )
        lines.append(f"🍽️ {meal_type}: {food_items}{cal}")
        macro_parts: list[str] = []
        for key, label in (
            ("protein_g", "P"),
            ("carbs_g", "C"),
            ("fat_g", "F"),
        ):
            value = entry.get(key)
            if value is None:
                continue
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                rendered = f"{float(value):g}"
            else:
                rendered = _bounded_html(value, _MAX_UNDO_VALUE_LENGTH)
            macro_parts.append(f"{label} {rendered}g")
        if macro_parts:
            lines.append(f"⚖️ {' · '.join(macro_parts)}")

    await reply_html(update.message, "\n".join(lines))


# ---------------------------------------------------------------------------
# Global error handler
# ---------------------------------------------------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors and notify the user."""
    error = context.error
    logger.error(
        "Exception while handling an update",
        exc_info=(type(error), error, error.__traceback__),
    )

    # Notify user (if we have an update with a message)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ Something went wrong. The error has been logged. "
            "Try again or use /cancel if you're stuck in a conversation."
        )
