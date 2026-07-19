"""
/diet handler — ConversationHandler with shortcut parsing.

Shortcut: /diet lunch dal+rice 650
Guided:   /diet → MEAL_TYPE → FOOD_ITEMS → CALORIES → done
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
    parse_int,
    reply_html,
    timeout_handler,
)
from ..keyboards import meal_type_keyboard
from ..config import CONVERSATION_TIMEOUT

logger = logging.getLogger(__name__)

# Conversation states
MEAL_TYPE, FOOD_ITEMS, CALORIES = range(3)

# Valid meal types
VALID_MEALS = {"breakfast", "lunch", "dinner", "snack"}
MAX_MEAL_CALORIES = 100_000
MAX_FOOD_ITEMS_LENGTH = 500
_NUMBER_LIKE = re.compile(r"^[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)$")
_MEAL_CALLBACK_RE = re.compile(
    r"^meal_(\d+)_(breakfast|lunch|dinner|snack)$"
)


def _looks_like_number(text: str) -> bool:
    """Return whether a shortcut token appears intended as a number."""
    stripped = text.strip()
    unsigned = stripped.lstrip("+-")
    return bool(_NUMBER_LIKE.fullmatch(stripped) or unsigned.isnumeric())


def _confirmation(meal_type: str, food_items: str, calories: int | None) -> str:
    """Build a safe HTML confirmation for a diet log."""
    calories_line = (
        f"\n🔥 Calories: <b>{calories}</b>" if calories is not None else ""
    )
    return (
        "✅ <b>Diet logged!</b>\n"
        f"🍽️ Meal: <b>{escape_html(meal_type.title())}</b>\n"
        f"🥘 Food: <b>{escape_html(food_items)}</b>{calories_line}"
    )


async def _remove_callback_markup(query: object) -> None:
    """Best-effort removal of an inline keyboard after it is consumed."""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        logger.debug("Could not remove stale meal keyboard", exc_info=True)


@authorized_callback
async def stale_meal_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Acknowledge a meal button that has no active diet conversation."""
    query = update.callback_query
    match = _MEAL_CALLBACK_RE.fullmatch(query.data or "")
    if match is None:
        await query.answer("This meal menu is no longer valid.", show_alert=True)
        return
    if int(match.group(1)) != update.effective_user.id:
        await query.answer("This meal menu belongs to another user.", show_alert=True)
        return
    await query.answer("This meal menu has expired.", show_alert=True)
    await _remove_callback_markup(query)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def diet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /diet with optional shortcut args."""
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)

    if not await conversation_available(update, context, "diet"):
        return ConversationHandler.END

    args = context.args or []

    # Shortcut: /diet <meal> <food> [calories]
    if len(args) >= 2:
        meal_type = args[0].lower()
        if meal_type not in VALID_MEALS:
            await reply_html(
                update.message,
                f"❌ Invalid meal type: <b>{escape_html(args[0])}</b>\n"
                "Use: breakfast, lunch, dinner, or snack",
            )
            return ConversationHandler.END

        # Food items: everything between meal and optional last-number
        calories = None
        if len(args) >= 3 and _looks_like_number(args[-1]):
            calories, err = parse_int(
                args[-1],
                "Calories",
                max_value=MAX_MEAL_CALORIES,
            )
            if err:
                await update.message.reply_text(err)
                return ConversationHandler.END
            food_items = " ".join(args[1:-1])
        else:
            food_items = " ".join(args[1:])

        food_items = food_items.replace("+", ", ")
        if len(food_items) > MAX_FOOD_ITEMS_LENGTH:
            await update.message.reply_text(
                f"❌ Food description too long (max {MAX_FOOD_ITEMS_LENGTH} characters)."
            )
            return ConversationHandler.END
        await db.log_diet(user.id, meal_type, food_items, calories)

        await reply_html(
            update.message,
            _confirmation(meal_type, food_items, calories),
        )
        return ConversationHandler.END

    # Guided flow
    activate_conversation(update, context, "diet")
    try:
        prompt = await reply_html(
            update.message,
            "🍽️ <b>Log Meal</b>\n\nWhich meal?",
            reply_markup=meal_type_keyboard(user.id),
        )
    except BaseException:
        finish_conversation(update, context, "diet")
        raise
    context.user_data["diet_meal_message_id"] = prompt.message_id
    return MEAL_TYPE


# ---------------------------------------------------------------------------
# Guided conversation states
# ---------------------------------------------------------------------------
@authorized_callback
async def receive_meal_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive meal type from InlineKeyboard."""
    query = update.callback_query
    match = _MEAL_CALLBACK_RE.fullmatch(query.data or "")
    if match is None:
        await query.answer("Invalid selection. Pick a meal type.", show_alert=True)
        return MEAL_TYPE
    if int(match.group(1)) != update.effective_user.id:
        await query.answer("This meal menu belongs to another user.", show_alert=True)
        return MEAL_TYPE

    expected_message_id = context.user_data.get("diet_meal_message_id")
    actual_message_id = getattr(query.message, "message_id", None)
    if expected_message_id is None or actual_message_id != expected_message_id:
        await query.answer("This meal menu has expired.", show_alert=True)
        await _remove_callback_markup(query)
        return MEAL_TYPE

    meal_type = match.group(2)

    await query.answer()
    context.user_data.pop("diet_meal_message_id", None)
    await _remove_callback_markup(query)
    context.user_data["diet_meal_type"] = meal_type
    emoji_map = {"breakfast": "🌅", "lunch": "🌞", "dinner": "🌙", "snack": "🍿"}
    emoji = emoji_map.get(meal_type, "🍽️")

    try:
        await reply_html(
            query.message,
            f"{emoji} <b>{escape_html(meal_type.title())}</b> — what did you eat?",
        )
    except TelegramError:
        logger.warning("Could not deliver diet food prompt", exc_info=True)
        finish_conversation(update, context, "diet")
        return ConversationHandler.END
    return FOOD_ITEMS


async def receive_food_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive food items description."""
    food = update.message.text.strip()
    if not food:
        await update.message.reply_text("❌ Food items can't be empty. What did you eat?")
        return FOOD_ITEMS
    if len(food) > MAX_FOOD_ITEMS_LENGTH:
        await update.message.reply_text(
            f"❌ Food description too long (max {MAX_FOOD_ITEMS_LENGTH} characters)."
        )
        return FOOD_ITEMS

    context.user_data["diet_food_items"] = food
    await update.message.reply_text("🔥 Estimated calories? (/skip if unsure)")
    return CALORIES


async def receive_calories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive calorie count."""
    text = update.message.text.strip()

    calories = None
    if text.lower() != "/skip":
        calories, err = parse_int(
            text,
            "Calories",
            max_value=MAX_MEAL_CALORIES,
        )
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
    meal_type = context.user_data["diet_meal_type"]
    food_items = context.user_data["diet_food_items"]

    await db.log_diet(user_id, meal_type, food_items, calories)
    context.user_data.pop("diet_meal_type", None)
    context.user_data.pop("diet_food_items", None)

    try:
        await reply_html(
            update.message,
            _confirmation(meal_type, food_items, calories),
        )
    except TelegramError:
        logger.warning("Could not deliver diet confirmation", exc_info=True)
    finish_conversation(update, context, "diet")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler
# ---------------------------------------------------------------------------
diet_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("diet", diet_command, filters=AUTH_FILTER)],
    states={
        MEAL_TYPE: [
            CallbackQueryHandler(
                receive_meal_type,
                pattern=r"^meal_\d+_(breakfast|lunch|dinner|snack)$",
            )
        ],
        FOOD_ITEMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_food_items)],
        CALORIES: [
            CommandHandler("skip", skip_calories),
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_calories),
        ],
        ConversationHandler.TIMEOUT: [TypeHandler(Update, timeout_handler)],
    },
    fallbacks=[
        cancel_handler,
        CommandHandler("diet", active_conversation_hint, filters=AUTH_FILTER),
    ],
    conversation_timeout=CONVERSATION_TIMEOUT,
)
