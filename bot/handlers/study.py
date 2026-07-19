"""
/study handler — ConversationHandler with shortcut parsing.

Shortcut: /study maths 45 reviewed eigenvalues
Guided:   /study → SUBJECT → DURATION → NOTES → done
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from .common import AUTH_FILTER, cancel_handler, timeout_handler, parse_int
from ..config import CONVERSATION_TIMEOUT

logger = logging.getLogger(__name__)

# Conversation states
SUBJECT, DURATION, NOTES = range(3)


# ---------------------------------------------------------------------------
# Entry point — shortcut or guided
# ---------------------------------------------------------------------------
async def study_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /study with optional shortcut args."""
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)

    args = context.args or []

    # Shortcut: /study <subject> <minutes> [notes...]
    if len(args) >= 2:
        subject = args[0]
        duration, err = parse_int(args[1], "duration")
        if err:
            await update.message.reply_text(err)
            return ConversationHandler.END

        notes = " ".join(args[2:]) if len(args) > 2 else None
        row_id = await db.log_study(user.id, subject, duration, notes)

        notes_line = f"\n📝 Notes: _{notes}_" if notes else ""
        await update.message.reply_text(
            f"✅ **Study logged!**\n"
            f"📖 Subject: *{subject}*\n"
            f"⏱️ Duration: *{duration} min*{notes_line}",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # Guided flow
    await update.message.reply_text("📖 **Log Study Session**\n\nWhat subject did you study?",
                                     parse_mode="Markdown")
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

    context.user_data["study_subject"] = subject
    await update.message.reply_text(f"📖 *{subject}* — how long did you study? (minutes)")
    return DURATION


async def receive_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive study duration in minutes."""
    duration, err = parse_int(update.message.text, "Duration")
    if err:
        await update.message.reply_text(err)
        return DURATION

    context.user_data["study_duration"] = duration
    await update.message.reply_text("📝 Any notes? (or /skip)")
    return NOTES


async def receive_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive optional notes."""
    text = update.message.text.strip()
    notes = None if text.lower() == "/skip" else text

    db = context.bot_data["db"]
    user_id = update.effective_user.id
    subject = context.user_data.pop("study_subject")
    duration = context.user_data.pop("study_duration")

    await db.log_study(user_id, subject, duration, notes)

    notes_line = f"\n📝 Notes: _{notes}_" if notes else ""
    await update.message.reply_text(
        f"✅ **Study logged!**\n"
        f"📖 Subject: *{subject}*\n"
        f"⏱️ Duration: *{duration} min*{notes_line}",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def skip_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /skip for notes."""
    db = context.bot_data["db"]
    user_id = update.effective_user.id
    subject = context.user_data.pop("study_subject")
    duration = context.user_data.pop("study_duration")

    await db.log_study(user_id, subject, duration, None)

    await update.message.reply_text(
        f"✅ **Study logged!**\n"
        f"📖 Subject: *{subject}*\n"
        f"⏱️ Duration: *{duration} min*",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


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
        ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, timeout_handler)],
    },
    fallbacks=[cancel_handler],
    conversation_timeout=CONVERSATION_TIMEOUT,
)
