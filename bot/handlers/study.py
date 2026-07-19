"""
/study handler — ConversationHandler with shortcut parsing.

Shortcut: /study maths 45 reviewed eigenvalues
Guided:   /study → SUBJECT → DURATION → NOTES → done
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    ContextTypes,
    filters,
)

from .common import (
    AUTH_FILTER,
    activate_conversation,
    active_conversation_hint,
    cancel_handler,
    conversation_available,
    escape_html,
    finish_conversation,
    parse_int,
    reply_html,
    timeout_handler,
)
from ..config import CONVERSATION_TIMEOUT

logger = logging.getLogger(__name__)

# Conversation states
SUBJECT, DURATION, NOTES = range(3)
MAX_STUDY_MINUTES = 24 * 60
MAX_SUBJECT_LENGTH = 100
MAX_NOTES_LENGTH = 500


def _confirmation(subject: str, duration: int, notes: str | None = None) -> str:
    """Build a safe HTML confirmation for a study log."""
    notes_line = f"\n📝 Notes: <i>{escape_html(notes)}</i>" if notes else ""
    return (
        "✅ <b>Study logged!</b>\n"
        f"📖 Subject: <b>{escape_html(subject)}</b>\n"
        f"⏱️ Duration: <b>{duration} min</b>{notes_line}"
    )


async def _confirm_and_finish(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    subject: str,
    duration: int,
    notes: str | None = None,
) -> int:
    """End a persisted study flow even if Telegram cannot deliver its receipt."""
    try:
        await reply_html(update.message, _confirmation(subject, duration, notes))
    except TelegramError:
        logger.warning("Could not deliver study confirmation", exc_info=True)
    finish_conversation(update, context, "study")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Entry point — shortcut or guided
# ---------------------------------------------------------------------------
async def study_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /study with optional shortcut args."""
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)

    if not await conversation_available(update, context, "study"):
        return ConversationHandler.END

    args = context.args or []

    # Shortcut: /study <subject> <minutes> [notes...]
    if len(args) >= 2:
        subject = args[0]
        if len(subject) > MAX_SUBJECT_LENGTH:
            await update.message.reply_text(
                f"❌ Subject too long (max {MAX_SUBJECT_LENGTH} characters)."
            )
            return ConversationHandler.END
        duration, err = parse_int(
            args[1], "Duration", max_value=MAX_STUDY_MINUTES
        )
        if err:
            await update.message.reply_text(err)
            return ConversationHandler.END

        notes = " ".join(args[2:]) if len(args) > 2 else None
        if notes and len(notes) > MAX_NOTES_LENGTH:
            await update.message.reply_text(
                f"❌ Notes too long (max {MAX_NOTES_LENGTH} characters)."
            )
            return ConversationHandler.END
        await db.log_study(user.id, subject, duration, notes)

        await reply_html(update.message, _confirmation(subject, duration, notes))
        return ConversationHandler.END

    # Guided flow
    activate_conversation(update, context, "study")
    try:
        await reply_html(
            update.message,
            "📖 <b>Log Study Session</b>\n\nWhat subject did you study?",
        )
    except BaseException:
        finish_conversation(update, context, "study")
        raise
    return SUBJECT


# ---------------------------------------------------------------------------
# Guided conversation states
# ---------------------------------------------------------------------------
async def receive_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive study subject."""
    subject = update.message.text.strip()
    if not subject:
        await update.message.reply_text("❌ Subject can't be empty. What subject?")
        return SUBJECT
    if len(subject) > MAX_SUBJECT_LENGTH:
        await update.message.reply_text(
            f"❌ Subject too long (max {MAX_SUBJECT_LENGTH} characters)."
        )
        return SUBJECT

    context.user_data["study_subject"] = subject
    await reply_html(
        update.message,
        f"📖 <b>{escape_html(subject)}</b> — how long did you study? (minutes)",
    )
    return DURATION


async def receive_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive study duration in minutes."""
    duration, err = parse_int(
        update.message.text,
        "Duration",
        max_value=MAX_STUDY_MINUTES,
    )
    if err:
        await update.message.reply_text(err)
        return DURATION

    context.user_data["study_duration"] = duration
    await update.message.reply_text("📝 Any notes? (or /skip)")
    return NOTES


async def receive_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive optional notes."""
    text = update.message.text.strip()
    notes = None if not text or text.lower() == "/skip" else text
    if notes and len(notes) > MAX_NOTES_LENGTH:
        await update.message.reply_text(
            f"❌ Notes too long (max {MAX_NOTES_LENGTH} characters)."
        )
        return NOTES

    db = context.bot_data["db"]
    user_id = update.effective_user.id
    subject = context.user_data["study_subject"]
    duration = context.user_data["study_duration"]

    await db.log_study(user_id, subject, duration, notes)
    context.user_data.pop("study_subject", None)
    context.user_data.pop("study_duration", None)

    return await _confirm_and_finish(update, context, subject, duration, notes)


async def skip_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /skip for notes."""
    db = context.bot_data["db"]
    user_id = update.effective_user.id
    subject = context.user_data["study_subject"]
    duration = context.user_data["study_duration"]

    await db.log_study(user_id, subject, duration, None)
    context.user_data.pop("study_subject", None)
    context.user_data.pop("study_duration", None)

    return await _confirm_and_finish(update, context, subject, duration)


# ---------------------------------------------------------------------------
# ConversationHandler
# ---------------------------------------------------------------------------
study_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("study", study_command, filters=AUTH_FILTER)],
    states={
        SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_subject)],
        DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_duration)],
        NOTES: [
            CommandHandler("skip", skip_notes),
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_notes),
        ],
        ConversationHandler.TIMEOUT: [TypeHandler(Update, timeout_handler)],
    },
    fallbacks=[
        cancel_handler,
        CommandHandler("study", active_conversation_hint, filters=AUTH_FILTER),
    ],
    conversation_timeout=CONVERSATION_TIMEOUT,
)
