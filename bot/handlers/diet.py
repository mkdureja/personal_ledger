"""
/diet handler — ConversationHandler with shortcut parsing.

Shortcut: /diet lunch dal+rice 650
Guided:   /diet → MEAL_TYPE → FOOD_ITEMS → CALORIES → done
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

from .common import AUTH_FILTER, cancel_handler, timeout_handler, parse_int
from ..keyboards import meal_type_keyboard
from ..config import CONVERSATION_TIMEOUT

logger = logging.getLogger(__name__)

# Conversation states
MEAL_TYPE, FOOD_ITEMS, CALORIES = range(3)

# Valid meal types
VALID_MEALS = {"breakfast", "lunch", "dinner", "snack"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def diet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /diet with optional shortcut args."""
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)

    args = context.args or []

    # Shortcut: /diet <meal> <food> [calories]
    if len(args) >= 2:
        meal_type = args[0].lower()
        if meal_type not in VALID_MEALS:
            await update.message.reply_text(
                f"❌ Invalid meal type: *{args[0]}*\n"
                "Use: breakfast, lunch, dinner, or snack",
                parse_mode="Markdown",
            )
            return ConversationHandler.END

        # Food items: everything between meal and optional last-number
        calories = None
        if args[-1].isdigit() and len(args) >= 3:
            calories = int(args[-1])
            food_items = " ".join(args[1:-1])
        else:
            food_items = " ".join(args[1:])

        food_items = food_items.replace("+", ", ")
        await db.log_diet(user.id, meal_type, food_items, calories)

        cal_str = f"\n🔥 Calories: *{calories}*" if calories else ""
        await update.message.reply_text(
            f"✅ **Diet logged!**\n"
            f"🍽️ Meal: *{meal_type.title()}*\n"
            f"🥘 Food: *{food_items}*{cal_str}",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # Guided flow
    await update.message.reply_text(
        "🍽️ **Log Meal**\n\nWhich meal?",
        reply_markup=meal_type_keyboard(),
        parse_mode="Markdown",
    )
    return MEAL_TYPE


# ---------------------------------------------------------------------------
# Guided conversation states
# ---------------------------------------------------------------------------
async def receive_meal_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive meal type from InlineKeyboard."""
    query = update.callback_query
    await query.answer()

    meal_type = query.data.replace("meal_", "")
    if meal_type not in VALID_MEALS:
        await query.message.reply_text("❌ Invalid selection. Pick a meal type.")
        return MEAL_TYPE

    context.user_data["diet_meal_type"] = meal_type
    emoji_map = {"breakfast": "🌅", "lunch": "🌞", "dinner": "🌙", "snack": "🍿"}
    emoji = emoji_map.get(meal_type, "🍽️")

    await query.message.reply_text(
        f"{emoji} *{meal_type.title()}* — what did you eat?",
        parse_mode="Markdown",
    )
    return FOOD_ITEMS


async def receive_food_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive food items description."""
    food = update.message.text.strip()
    if not food:
        await update.message.reply_text("❌ Food items can't be empty. What did you eat?")
        return FOOD_ITEMS

    context.user_data["diet_food_items"] = food
    await update.message.reply_text("🔥 Estimated calories? (/skip if unsure)")
    return CALORIES


async def receive_calories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive calorie count."""
    text = update.message.text.strip()

    calories = None
    if text.lower() != "/skip":
        calories, err = parse_int(text, "Calories")
        if err:
            await update.message.reply_text(err + "\nOr /skip if unsure.")
            return CALORIES

    return await _save_diet(update, context, calories)


async def skip_calories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /skip for calories."""
    return await _save_diet(update, context, None)


async def _save_diet(
    update: Update, context: ContextTypes.DEFAULT_TYPE, calories: int | None
) -> int:
    """Save diet entry and confirm."""
    db = context.bot_data["db"]
    user_id = update.effective_user.id
    meal_type = context.user_data.pop("diet_meal_type")
    food_items = context.user_data.pop("diet_food_items")

    await db.log_diet(user_id, meal_type, food_items, calories)

    cal_str = f"\n🔥 Calories: *{calories}*" if calories else ""
    await update.message.reply_text(
        f"✅ **Diet logged!**\n"
        f"🍽️ Meal: *{meal_type.title()}*\n"
        f"🥘 Food: *{food_items}*{cal_str}",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler
# ---------------------------------------------------------------------------
diet_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("diet", diet_command, filters=AUTH_FILTER)],
    states={
        MEAL_TYPE: [CallbackQueryHandler(receive_meal_type, pattern=r"^meal_")],
        FOOD_ITEMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_food_items)],
        CALORIES: [
            CommandHandler("skip", skip_calories),
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_calories),
        ],
        ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, timeout_handler)],
    },
    fallbacks=[cancel_handler],
    conversation_timeout=CONVERSATION_TIMEOUT,
)
