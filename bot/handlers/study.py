"""
/study handler — ConversationHandler with shortcut parsing.

Shortcut: /study maths 45 reviewed eigenvalues
Guided:   /study → SUBJECT → DURATION → NOTES → done
"""

from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.error import TelegramError
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
    ACTIVE_CONTROL_FILTER,
    AUTH_FILTER,
    activate_conversation,
    active_conversation_hint,
    active_flow_control_interceptor,
    authorized_callback,
    cancel_handler,
    conversation_available,
    deliver_or_end,
    escape_html,
    finish_conversation,
    mutation_source,
    parse_int,
    reply_html,
    timeout_handler,
    voice_not_enabled_interceptor,
)
from ..config import CONVERSATION_TIMEOUT

logger = logging.getLogger(__name__)

# Conversation states
SUBJECT, DURATION, NOTES = range(3)
MAX_STUDY_MINUTES = 24 * 60
MAX_SUBJECT_LENGTH = 100
MAX_NOTES_LENGTH = 500

# Explicit duration marker, e.g. "45m" / "45min" / "45minutes".
_DURATION_MARKER = re.compile(r"^(\d+)(?:m|min|mins|minute|minutes)$", re.IGNORECASE)
# Sentinel returned when a shortcut has two or more plausible durations.
_AMBIGUOUS = object()


def _parse_study_shortcut(args: list[str]):
    """Resolve the duration in a /study shortcut unambiguously.

    Only tokens at index >= 1 may be the duration, so the subject is never
    empty. An explicit ``<n>m`` marker always wins; otherwise a single bare
    integer is accepted. Returns ``(duration_index, minutes)``, the
    ``_AMBIGUOUS`` sentinel when more than one candidate exists, or ``None``
    when there is no duration (fall back to the guided flow).
    """
    marker_hits = [
        (i, int(m.group(1)))
        for i in range(1, len(args))
        if (m := _DURATION_MARKER.match(args[i]))
    ]
    if len(marker_hits) > 1:
        return _AMBIGUOUS
    if len(marker_hits) == 1:
        return marker_hits[0]

    bare_ints = [
        (i, int(args[i]))
        for i in range(1, len(args))
        if args[i].isdigit() and int(args[i]) > 0
    ]
    if len(bare_ints) > 1:
        return _AMBIGUOUS
    if len(bare_ints) == 1:
        return bare_ints[0]
    return None


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

    # Shortcut: /study <subject words...> <minutes> [notes...]
    # The duration must be unambiguous — a single bare integer, or any token
    # explicitly marked with "m" (e.g. 45m). Two plausible numbers are rejected
    # with a hint rather than silently guessed, which used to corrupt notes
    # ending in a number (e.g. "…chapter 2").
    if len(args) >= 2:
        parsed = _parse_study_shortcut(args)
        if parsed is _AMBIGUOUS:
            await reply_html(
                update.message,
                "🤔 I can't tell which number is the duration. Mark it with "
                "<b>m</b> — e.g. <code>/study physics 60m reviewed chapter 2</code>.",
            )
            return ConversationHandler.END
        if parsed is not None:
            duration_index, duration = parsed
            if not 0 < duration <= MAX_STUDY_MINUTES:
                await update.message.reply_text(
                    f"❌ Duration must be between 1 and {MAX_STUDY_MINUTES} minutes."
                )
                return ConversationHandler.END

            subject = " ".join(args[:duration_index])
            if len(subject) > MAX_SUBJECT_LENGTH:
                await update.message.reply_text(
                    f"❌ Subject too long (max {MAX_SUBJECT_LENGTH} characters)."
                )
                return ConversationHandler.END

            notes = " ".join(args[duration_index + 1:]) if len(args) > duration_index + 1 else None
            if notes and len(notes) > MAX_NOTES_LENGTH:
                await update.message.reply_text(
                    f"❌ Notes too long (max {MAX_NOTES_LENGTH} characters)."
                )
                return ConversationHandler.END
            await db.log_study(
                user.id, subject, duration, notes, source=mutation_source(update)
            )

            # The row is committed; a failed confirmation must not surface as an
            # error (the user can reconcile via /recent). A genuine resend is a
            # new update and logs again by design; a Telegram-level replay of this
            # update is deduplicated by the receipt above.
            try:
                await reply_html(update.message, _confirmation(subject, duration, notes))
            except TelegramError:
                logger.warning("Could not deliver study confirmation", exc_info=True)
            return ConversationHandler.END

    # Guided flow
    return await _begin_study_flow(update, context)


async def _begin_study_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Start the guided study flow from either /study or the Study menu tap.

    Uses ``effective_message`` so it works for a callback entry (where
    ``update.message`` is ``None``).
    """
    activate_conversation(update, context, "study")
    try:
        await reply_html(
            update.effective_message,
            "📖 <b>Log Study Session</b>\n\nWhat subject did you study?",
        )
    except BaseException:
        finish_conversation(update, context, "study")
        raise
    return SUBJECT


@authorized_callback
async def study_menu_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Enter the guided study flow from a main-menu 'Study' tap."""
    query = update.callback_query
    await query.answer()
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)
    if not await conversation_available(update, context, "study"):
        return ConversationHandler.END
    return await _begin_study_flow(update, context)


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
    if not await deliver_or_end(
        update,
        context,
        "study",
        reply_html(
            update.message,
            f"📖 <b>{escape_html(subject)}</b> — how long did you study? (minutes)",
        ),
    ):
        return ConversationHandler.END
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
    if not await deliver_or_end(
        update,
        context,
        "study",
        update.message.reply_text("📝 Any notes? (or /skip)"),
    ):
        return ConversationHandler.END
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

    await db.log_study(
        user_id, subject, duration, notes, source=mutation_source(update)
    )
    context.user_data.pop("study_subject", None)
    context.user_data.pop("study_duration", None)

    return await _confirm_and_finish(update, context, subject, duration, notes)


async def skip_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /skip for notes."""
    db = context.bot_data["db"]
    user_id = update.effective_user.id
    subject = context.user_data["study_subject"]
    duration = context.user_data["study_duration"]

    await db.log_study(
        user_id, subject, duration, None, source=mutation_source(update)
    )
    context.user_data.pop("study_subject", None)
    context.user_data.pop("study_duration", None)

    return await _confirm_and_finish(update, context, subject, duration)


# ---------------------------------------------------------------------------
# ConversationHandler
# ---------------------------------------------------------------------------
# Reject voice mid-flow (no download) and nudge on any Home control word before
# it can be captured as a subject/note (plan §8.5/§8.6).
_voice_guard = MessageHandler(filters.VOICE, voice_not_enabled_interceptor)
_control_guard = MessageHandler(
    ACTIVE_CONTROL_FILTER, active_flow_control_interceptor
)

study_conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("study", study_command, filters=AUTH_FILTER),
        CallbackQueryHandler(study_menu_entry, pattern=r"^menu_study$"),
    ],
    states={
        SUBJECT: [
            _voice_guard,
            _control_guard,
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_subject),
        ],
        DURATION: [
            _voice_guard,
            _control_guard,
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_duration),
        ],
        NOTES: [
            CommandHandler("skip", skip_notes),
            _voice_guard,
            _control_guard,
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
