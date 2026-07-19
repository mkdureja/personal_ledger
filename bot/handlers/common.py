"""
Common handler utilities: auth filter, error handler, /cancel, /undo, validators.
"""

from __future__ import annotations

import html
import logging
import traceback
from datetime import datetime

from telegram import Update
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    ConversationHandler,
    filters,
)

from ..config import ALLOWED_USER_IDS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth filter — compose into every handler
# ---------------------------------------------------------------------------
AUTH_FILTER = filters.User(user_id=ALLOWED_USER_IDS)

# ---------------------------------------------------------------------------
# Input validators
# ---------------------------------------------------------------------------


def parse_int(text: str, field_name: str) -> tuple[int | None, str | None]:
    """Parse a positive integer from text.

    Returns (value, None) on success or (None, error_message) on failure.
    """
    text = text.strip()
    try:
        val = int(text)
    except ValueError:
        return None, f"❌ Please enter a valid number for {field_name}."
    if val <= 0:
        return None, f"❌ {field_name} must be a positive number."
    return val, None


def parse_float(text: str, field_name: str) -> tuple[float | None, str | None]:
    """Parse a positive float from text.

    Returns (value, None) on success or (None, error_message) on failure.
    """
    text = text.strip()
    try:
        val = float(text)
    except ValueError:
        return None, f"❌ Please enter a valid number for {field_name}."
    if val <= 0:
        return None, f"❌ {field_name} must be a positive number."
    return val, None


# ---------------------------------------------------------------------------
# /cancel — fallback for all ConversationHandlers
# ---------------------------------------------------------------------------
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the current conversation."""
    # Clear any stored conversation data
    context.user_data.clear()
    await update.message.reply_text("✖️ Cancelled.")
    return ConversationHandler.END


cancel_handler = CommandHandler("cancel", cancel_command)


# ---------------------------------------------------------------------------
# Conversation timeout handler
# ---------------------------------------------------------------------------
async def timeout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Called when a conversation times out."""
    context.user_data.clear()
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "⏰ Timed out. Send the command again to start over."
        )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /undo — delete most recent log entry
# ---------------------------------------------------------------------------
async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the most recent log entry (within 24h)."""
    db = context.bot_data["db"]
    user_id = update.effective_user.id

    entry = await db.undo_last(user_id)
    if entry is None:
        await update.message.reply_text(
            "🤷 Nothing to undo — no entries in the last 24 hours."
        )
        return

    category = entry.get("category", "Unknown")
    lines = [f"↩️ **Undone:** {category}"]

    # Build detail based on category
    if "subject" in entry:
        lines.append(f"📖 {entry['subject']} — {entry['duration_min']} min")
    elif "exercise" in entry:
        w = f" @ {entry['weight_kg']}kg" if entry.get("weight_kg") else " (bodyweight)"
        lines.append(f"🏋️ {entry['exercise']} — {entry['sets']}×{entry['reps']}{w}")
    elif "food_items" in entry:
        cal = f" — {entry['calories']} cal" if entry.get("calories") else ""
        lines.append(f"🍽️ {entry['meal_type']}: {entry['food_items']}{cal}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Global error handler
# ---------------------------------------------------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors and notify the user."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    # Format traceback for logging
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)
    logger.error("Traceback:\n%s", tb_string)

    # Notify user (if we have an update with a message)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ Something went wrong. The error has been logged. "
            "Try again or use /cancel if you're stuck in a conversation."
        )
