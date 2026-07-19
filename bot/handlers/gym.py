"""
/gym handler — ConversationHandler with per-exercise persistence.

Shortcut: /gym pushups 3 15
Guided:   /gym → EXERCISE → SETS → REPS → WEIGHT → MORE → (loop or done)

Each exercise is saved immediately after WEIGHT, before asking "Log another?"
Abandoning mid-loop loses only the current incomplete exercise.
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
    AUTH_FILTER,
    active_conversation_hint,
    activate_conversation,
    authorized_callback,
    cancel_handler,
    conversation_available,
    escape_html,
    finish_conversation,
    parse_float,
    parse_int,
    reply_html,
    timeout_handler,
)
from ..keyboards import yes_no_keyboard
from ..config import CONVERSATION_TIMEOUT

logger = logging.getLogger(__name__)

# Conversation states
EXERCISE, SETS, REPS, WEIGHT, MORE = range(5)
MAX_GYM_SETS = 100
MAX_GYM_REPS = 1_000
MAX_WEIGHT_KG = 1_000.0
MAX_EXERCISE_NAME_LENGTH = 50
MAX_GYM_EXERCISES = 10
_GYM_MORE_RE = re.compile(r"^gym_(\d+)_(yes|no)$")


def _exercise_summary(
    exercise: str,
    sets: int,
    reps: int,
    weight: float | None,
) -> str:
    """Build one safe HTML exercise-summary line."""
    weight_text = f" @ {weight:g}kg" if weight is not None else " (bodyweight)"
    return f"🏋️ {escape_html(exercise)} — {sets}×{reps}{weight_text}"


def _workout_confirmation(exercises: list[str]) -> str:
    """Build a bounded, safe HTML workout confirmation."""
    count = len(exercises)
    summary = "\n".join(exercises)
    return (
        f"✅ <b>Workout logged!</b> "
        f"({count} exercise{'s' if count != 1 else ''})\n\n{summary}"
    )


async def _finish_workout(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message,
    exercises: list[str],
) -> int:
    """End a persisted workout even if Telegram cannot deliver its summary."""
    try:
        await reply_html(message, _workout_confirmation(exercises))
    except TelegramError:
        logger.warning("Could not deliver workout confirmation", exc_info=True)
    finish_conversation(update, context, "gym")
    return ConversationHandler.END


async def _remove_callback_markup(query: object) -> None:
    """Best-effort removal of an inline keyboard after it is consumed."""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        logger.debug("Could not remove stale gym keyboard", exc_info=True)


@authorized_callback
async def stale_gym_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Acknowledge a workout button that has no active gym conversation."""
    query = update.callback_query
    match = _GYM_MORE_RE.fullmatch(query.data or "")
    if match is None:
        await query.answer("This workout prompt is no longer valid.", show_alert=True)
        return
    if int(match.group(1)) != update.effective_user.id:
        await query.answer("This workout prompt belongs to another user.", show_alert=True)
        return
    await query.answer("This workout prompt has expired.", show_alert=True)
    await _remove_callback_markup(query)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def gym_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /gym with optional shortcut args."""
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)

    if not await conversation_available(update, context, "gym"):
        return ConversationHandler.END

    args = context.args or []

    # Shortcut: /gym <exercise> <sets> <reps> [weight]
    if len(args) >= 3:
        exercise = args[0]
        if len(exercise) > MAX_EXERCISE_NAME_LENGTH:
            await update.message.reply_text(
                f"❌ Exercise name too long (max {MAX_EXERCISE_NAME_LENGTH} characters)."
            )
            return ConversationHandler.END
        sets, err = parse_int(args[1], "Sets", max_value=MAX_GYM_SETS)
        if err:
            await update.message.reply_text(err)
            return ConversationHandler.END
        reps, err = parse_int(args[2], "Reps", max_value=MAX_GYM_REPS)
        if err:
            await update.message.reply_text(err)
            return ConversationHandler.END

        weight = None
        if len(args) >= 4:
            weight, err = parse_float(
                args[3], "Weight", max_value=MAX_WEIGHT_KG
            )
            if err:
                await update.message.reply_text(err)
                return ConversationHandler.END

        await db.log_gym(user.id, exercise, sets, reps, weight)
        await reply_html(
            update.message,
            "✅ <b>Exercise logged!</b>\n"
            f"{_exercise_summary(exercise, sets, reps, weight)}",
        )
        return ConversationHandler.END

    # Guided flow
    context.user_data["gym_exercises"] = []
    activate_conversation(update, context, "gym")
    try:
        await reply_html(
            update.message,
            "🏋️ <b>Log Workout</b>\n\nWhat exercise did you do?",
        )
    except BaseException:
        finish_conversation(update, context, "gym")
        raise
    return EXERCISE


# ---------------------------------------------------------------------------
# Guided conversation states
# ---------------------------------------------------------------------------
async def receive_exercise(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive exercise name."""
    exercise = update.message.text.strip()
    if not exercise:
        await update.message.reply_text("❌ Exercise name can't be empty. What exercise?")
        return EXERCISE
    if len(exercise) > MAX_EXERCISE_NAME_LENGTH:
        await update.message.reply_text(
            f"❌ Exercise name too long (max {MAX_EXERCISE_NAME_LENGTH} characters)."
        )
        return EXERCISE

    context.user_data["gym_current_exercise"] = exercise
    await reply_html(
        update.message,
        f"🏋️ <b>{escape_html(exercise)}</b> — how many sets?",
    )
    return SETS


async def receive_sets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive number of sets."""
    sets, err = parse_int(
        update.message.text,
        "Sets",
        max_value=MAX_GYM_SETS,
    )
    if err:
        await update.message.reply_text(err)
        return SETS

    context.user_data["gym_current_sets"] = sets
    await update.message.reply_text("How many reps per set?")
    return REPS


async def receive_reps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive number of reps."""
    reps, err = parse_int(
        update.message.text,
        "Reps",
        max_value=MAX_GYM_REPS,
    )
    if err:
        await update.message.reply_text(err)
        return REPS

    context.user_data["gym_current_reps"] = reps
    await update.message.reply_text("Weight in kg? (/skip for bodyweight)")
    return WEIGHT


async def receive_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive weight (or skip for bodyweight). Saves exercise immediately."""
    text = update.message.text.strip()

    weight = None
    if text.lower() != "/skip":
        weight, err = parse_float(
            text,
            "Weight",
            max_value=MAX_WEIGHT_KG,
        )
        if err:
            await update.message.reply_text(err + "\nOr /skip for bodyweight.")
            return WEIGHT

    return await _save_current_exercise(update, context, weight)


async def skip_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /skip for weight."""
    return await _save_current_exercise(update, context, None)


async def _save_current_exercise(
    update: Update, context: ContextTypes.DEFAULT_TYPE, weight: float | None
) -> int:
    """Save the current exercise to DB immediately, then ask for more."""
    db = context.bot_data["db"]
    user_id = update.effective_user.id

    exercise = context.user_data["gym_current_exercise"]
    sets = context.user_data["gym_current_sets"]
    reps = context.user_data["gym_current_reps"]

    await db.log_gym(user_id, exercise, sets, reps, weight)

    # Track for final summary
    context.user_data.setdefault("gym_exercises", []).append(
        _exercise_summary(exercise, sets, reps, weight)
    )
    context.user_data.pop("gym_current_exercise", None)
    context.user_data.pop("gym_current_sets", None)
    context.user_data.pop("gym_current_reps", None)

    count = len(context.user_data["gym_exercises"])
    if count >= MAX_GYM_EXERCISES:
        return await _finish_workout(
            update,
            context,
            update.message,
            context.user_data.pop("gym_exercises"),
        )

    try:
        prompt = await update.message.reply_text(
            f"✅ Saved! ({count} exercise{'s' if count > 1 else ''} logged)\n\n"
            "Log another exercise?",
            reply_markup=yes_no_keyboard("gym", user_id),
        )
    except TelegramError:
        logger.warning("Could not deliver workout continuation prompt", exc_info=True)
        finish_conversation(update, context, "gym")
        return ConversationHandler.END
    context.user_data["gym_more_message_id"] = prompt.message_id
    return MORE


@authorized_callback
async def more_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Yes/No for logging another exercise."""
    query = update.callback_query
    match = _GYM_MORE_RE.fullmatch(query.data or "")
    if match is None:
        await query.answer("This workout prompt is no longer valid.", show_alert=True)
        return MORE
    if int(match.group(1)) != update.effective_user.id:
        await query.answer("This workout prompt belongs to another user.", show_alert=True)
        return MORE

    expected_message_id = context.user_data.get("gym_more_message_id")
    actual_message_id = getattr(query.message, "message_id", None)
    if expected_message_id is None or actual_message_id != expected_message_id:
        await query.answer("This workout prompt has expired.", show_alert=True)
        await _remove_callback_markup(query)
        return MORE

    await query.answer()
    context.user_data.pop("gym_more_message_id", None)
    await _remove_callback_markup(query)

    if match.group(2) == "yes":
        try:
            await query.message.reply_text("What exercise?")
        except TelegramError:
            logger.warning("Could not deliver next exercise prompt", exc_info=True)
            finish_conversation(update, context, "gym")
            return ConversationHandler.END
        return EXERCISE
    else:
        # Done — show summary
        exercises = context.user_data.pop("gym_exercises", [])
        return await _finish_workout(
            update, context, query.message, exercises
        )


# ---------------------------------------------------------------------------
# ConversationHandler
# ---------------------------------------------------------------------------
gym_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("gym", gym_command, filters=AUTH_FILTER)],
    states={
        EXERCISE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_exercise)],
        SETS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_sets)],
        REPS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_reps)],
        WEIGHT: [
            CommandHandler("skip", skip_weight),
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_weight),
        ],
        MORE: [CallbackQueryHandler(more_callback, pattern=r"^gym_\d+_(yes|no)$")],
        ConversationHandler.TIMEOUT: [TypeHandler(Update, timeout_handler)],
    },
    fallbacks=[
        cancel_handler,
        CommandHandler("gym", active_conversation_hint, filters=AUTH_FILTER),
    ],
    conversation_timeout=CONVERSATION_TIMEOUT,
    # per_message=False is correct: the code manually validates callback
    # ownership via ``gym_more_message_id`` in user_data.
    per_message=False,
)
