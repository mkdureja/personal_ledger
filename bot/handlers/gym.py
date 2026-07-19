"""
/gym handler — ConversationHandler with per-exercise persistence.

Shortcut: /gym pushups 3 15
Guided:   /gym → EXERCISE → SETS → REPS → WEIGHT → MORE → (loop or done)

Each exercise is saved immediately after WEIGHT, before asking "Log another?"
Abandoning mid-loop loses only the current incomplete exercise.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from .common import AUTH_FILTER, cancel_handler, timeout_handler, parse_int, parse_float
from ..keyboards import yes_no_keyboard
from ..config import CONVERSATION_TIMEOUT

logger = logging.getLogger(__name__)

# Conversation states
EXERCISE, SETS, REPS, WEIGHT, MORE = range(5)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def gym_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /gym with optional shortcut args."""
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)

    args = context.args or []

    # Shortcut: /gym <exercise> <sets> <reps> [weight]
    if len(args) >= 3:
        exercise = args[0]
        sets, err = parse_int(args[1], "sets")
        if err:
            await update.message.reply_text(err)
            return ConversationHandler.END
        reps, err = parse_int(args[2], "reps")
        if err:
            await update.message.reply_text(err)
            return ConversationHandler.END

        weight = None
        if len(args) >= 4:
            weight, err = parse_float(args[3], "weight")
            if err:
                await update.message.reply_text(err)
                return ConversationHandler.END

        await db.log_gym(user.id, exercise, sets, reps, weight)
        w_str = f" @ {weight}kg" if weight else " (bodyweight)"
        await update.message.reply_text(
            f"✅ **Exercise logged!**\n"
            f"🏋️ {exercise} — {sets}×{reps}{w_str}",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # Guided flow
    context.user_data["gym_exercises"] = []
    await update.message.reply_text(
        "🏋️ **Log Workout**\n\nWhat exercise did you do?",
        parse_mode="Markdown",
    )
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

    context.user_data["gym_current_exercise"] = exercise
    await update.message.reply_text(f"🏋️ *{exercise}* — how many sets?",
                                     parse_mode="Markdown")
    return SETS


async def receive_sets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive number of sets."""
    sets, err = parse_int(update.message.text, "Sets")
    if err:
        await update.message.reply_text(err)
        return SETS

    context.user_data["gym_current_sets"] = sets
    await update.message.reply_text("How many reps per set?")
    return REPS


async def receive_reps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive number of reps."""
    reps, err = parse_int(update.message.text, "Reps")
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
        weight, err = parse_float(text, "Weight")
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

    exercise = context.user_data.pop("gym_current_exercise")
    sets = context.user_data.pop("gym_current_sets")
    reps = context.user_data.pop("gym_current_reps")

    await db.log_gym(user_id, exercise, sets, reps, weight)

    # Track for final summary
    w_str = f" @ {weight}kg" if weight else " (bodyweight)"
    context.user_data.setdefault("gym_exercises", []).append(
        f"🏋️ {exercise} — {sets}×{reps}{w_str}"
    )

    count = len(context.user_data["gym_exercises"])
    await update.message.reply_text(
        f"✅ Saved! ({count} exercise{'s' if count > 1 else ''} logged)\n\n"
        "Log another exercise?",
        reply_markup=yes_no_keyboard("gym"),
    )
    return MORE


async def more_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Yes/No for logging another exercise."""
    query = update.callback_query
    await query.answer()

    if query.data == "gym_yes":
        await query.message.reply_text("What exercise?")
        return EXERCISE
    else:
        # Done — show summary
        exercises = context.user_data.pop("gym_exercises", [])
        count = len(exercises)
        summary = "\n".join(exercises)
        await query.message.reply_text(
            f"✅ **Workout logged!** ({count} exercise{'s' if count > 1 else ''})\n\n{summary}",
            parse_mode="Markdown",
        )
        return ConversationHandler.END


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
        MORE: [CallbackQueryHandler(more_callback, pattern=r"^gym_(yes|no)$")],
        ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, timeout_handler)],
    },
    fallbacks=[cancel_handler],
    conversation_timeout=CONVERSATION_TIMEOUT,
)
