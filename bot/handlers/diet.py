"""
/diet handler — ConversationHandler with shortcut parsing.

Shortcut: /diet lunch dal+rice 650 p=25 c=80 f=15
Guided:   /diet → MEAL_TYPE → FOOD_ITEMS → CALORIES → MACROS → done
"""

from __future__ import annotations

import logging
import math
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
from .catalog import resolve_catalog_diet_entry
from ..keyboards import meal_type_keyboard
from ..config import CONVERSATION_TIMEOUT
from ..nutrition import MAX_LOG_CALORIES, MAX_LOG_MACRO_GRAMS, NutritionError

logger = logging.getLogger(__name__)

# Conversation states
MEAL_TYPE, FOOD_ITEMS, CALORIES, MACROS = range(4)

# Valid meal types
VALID_MEALS = {"breakfast", "lunch", "dinner", "snack"}
MAX_MEAL_CALORIES = int(MAX_LOG_CALORIES)
MAX_MACRO_GRAMS = float(MAX_LOG_MACRO_GRAMS)
MAX_FOOD_ITEMS_LENGTH = 500
_NUMBER_LIKE = re.compile(r"^[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)$")
_MACRO_TOKEN_RE = re.compile(r"^(p|c|f)=(.*)$", re.IGNORECASE)
_MEAL_CALLBACK_RE = re.compile(
    r"^meal_(\d+)_(breakfast|lunch|dinner|snack)$"
)


def _is_catalog_reference(token: str) -> bool:
    lowered = token.casefold()
    return lowered.startswith("food:") or lowered.startswith("recipe:")


def _looks_like_number(text: str) -> bool:
    """Return whether a shortcut token looks like an unsigned integer calorie count."""
    stripped = text.strip()
    return stripped.isdigit()


def _parse_macro_grams(
    text: str,
    field_name: str,
) -> tuple[float | None, str | None]:
    """Parse a finite, nonnegative macro value in grams."""
    try:
        value = float(text.strip())
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None, f"❌ Please enter a valid number for {field_name}."
    if not math.isfinite(value):
        return None, f"❌ Please enter a finite number for {field_name}."
    if value < 0:
        return None, f"❌ {field_name} can't be negative."
    if value > MAX_MACRO_GRAMS:
        return None, f"❌ {field_name} must be {MAX_MACRO_GRAMS:g} g or less."
    # Avoid displaying or persisting the surprising spelling "-0".
    return (0.0 if value == 0 else value), None


def _extract_shortcut_macros(
    tokens: list[str],
) -> tuple[list[str], dict[str, float | None], str | None]:
    """Remove a contiguous suffix of labeled macro tokens from shortcut args."""
    remaining = list(tokens)
    macros: dict[str, float | None] = {
        "protein_g": None,
        "carbs_g": None,
        "fat_g": None,
    }
    label_fields = {
        "p": ("protein_g", "Protein"),
        "c": ("carbs_g", "Carbs"),
        "f": ("fat_g", "Fat"),
    }
    seen: set[str] = set()

    while remaining:
        match = _MACRO_TOKEN_RE.fullmatch(remaining[-1])
        if match is None:
            break
        remaining.pop()
        label = match.group(1).lower()
        key, field_name = label_fields[label]
        if label in seen:
            return remaining, macros, f"❌ {field_name} was provided more than once."
        seen.add(label)
        value, error = _parse_macro_grams(match.group(2), field_name)
        if error:
            return remaining, macros, error
        macros[key] = value

    return remaining, macros, None


def _format_grams(value: float) -> str:
    """Format parsed grams without an unnecessary decimal suffix."""
    return f"{value:g}"


def _confirmation(
    meal_type: str,
    food_items: str,
    calories: int | None,
    protein_g: float | None = None,
    carbs_g: float | None = None,
    fat_g: float | None = None,
) -> str:
    """Build a safe HTML confirmation for a diet log."""
    calories_line = (
        f"\n🔥 Calories: <b>{calories}</b>" if calories is not None else ""
    )
    macro_parts = [
        f"{label} {_format_grams(value)} g"
        for label, value in (("P", protein_g), ("C", carbs_g), ("F", fat_g))
        if value is not None
    ]
    macros_line = (
        f"\n🥩 Macros: <b>{escape_html(' · '.join(macro_parts))}</b>"
        if macro_parts
        else ""
    )
    return (
        "✅ <b>Diet logged!</b>\n"
        f"🍽️ Meal: <b>{escape_html(meal_type.title())}</b>\n"
        f"🥘 Food: <b>{escape_html(food_items)}</b>{calories_line}{macros_line}"
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

    # Shortcut: /diet <meal> <food> [calories] [p=grams c=grams f=grams]
    if len(args) >= 2:
        meal_type = args[0].lower()
        if meal_type not in VALID_MEALS:
            await reply_html(
                update.message,
                f"❌ Invalid meal type: <b>{escape_html(args[0])}</b>\n"
                "Use: breakfast, lunch, dinner, or snack",
            )
            return ConversationHandler.END

        # Explicit catalog syntax is isolated from the legacy free-text parser.
        # This preserves commands such as ``/diet lunch apple 220`` where the
        # final number has always meant calories.
        if _is_catalog_reference(args[1]):
            try:
                entry = await resolve_catalog_diet_entry(
                    db,
                    user.id,
                    args[1],
                    args[2:],
                )
            except NutritionError as exc:
                await update.message.reply_text(
                    f"❌ {exc}\n"
                    "Examples: /diet snack food:apple 1 medium or "
                    "/diet dinner recipe:curry 1 serving"
                )
                return ConversationHandler.END

            await db.log_diet(
                user.id,
                meal_type,
                entry.display_text,
                entry.calories,
                protein_g=entry.protein_g,
                carbs_g=entry.carbs_g,
                fat_g=entry.fat_g,
            )
            try:
                await reply_html(
                    update.message,
                    _confirmation(
                        meal_type,
                        entry.display_text,
                        entry.calories,
                        entry.protein_g,
                        entry.carbs_g,
                        entry.fat_g,
                    ),
                )
            except TelegramError:
                logger.warning("Could not deliver diet confirmation", exc_info=True)
            return ConversationHandler.END

        food_tokens, macros, macro_error = _extract_shortcut_macros(args[1:])
        if macro_error:
            await update.message.reply_text(
                macro_error
                + "\nUse labeled macros after the food/calories, e.g. p=25 c=80 f=15."
            )
            return ConversationHandler.END
        if not food_tokens:
            await update.message.reply_text("❌ Food items can't be empty.")
            return ConversationHandler.END

        # Food items: everything before the optional calories and macro suffix.
        calories = None
        if len(food_tokens) >= 2 and _looks_like_number(food_tokens[-1]):
            calories, err = parse_int(
                food_tokens[-1],
                "Calories",
                max_value=MAX_MEAL_CALORIES,
            )
            if err:
                await update.message.reply_text(err)
                return ConversationHandler.END
            food_items = " ".join(food_tokens[:-1])
        else:
            food_items = " ".join(food_tokens)

        food_items = food_items.replace("+", ", ")
        if len(food_items) > MAX_FOOD_ITEMS_LENGTH:
            await update.message.reply_text(
                f"❌ Food description too long (max {MAX_FOOD_ITEMS_LENGTH} characters)."
            )
            return ConversationHandler.END
        await db.log_diet(
            user.id,
            meal_type,
            food_items,
            calories,
            protein_g=macros["protein_g"],
            carbs_g=macros["carbs_g"],
            fat_g=macros["fat_g"],
        )

        try:
            await reply_html(
                update.message,
                _confirmation(meal_type, food_items, calories, **macros),
            )
        except TelegramError:
            logger.warning("Could not deliver diet confirmation", exc_info=True)
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

    food_tokens = food.split()
    if food_tokens and _is_catalog_reference(food_tokens[0]):
        db = context.bot_data["db"]
        try:
            entry = await resolve_catalog_diet_entry(
                db,
                update.effective_user.id,
                food_tokens[0],
                food_tokens[1:],
            )
        except NutritionError as exc:
            await update.message.reply_text(
                f"❌ {exc}\n"
                "Try again, or send ordinary food text for manual nutrition entry."
            )
            return FOOD_ITEMS
        return await _save_diet(
            update,
            context,
            entry.calories,
            protein_g=entry.protein_g,
            carbs_g=entry.carbs_g,
            fat_g=entry.fat_g,
            food_items=entry.display_text,
        )

    context.user_data["diet_food_items"] = food
    try:
        await update.message.reply_text("🔥 Estimated calories? (/skip if unsure)")
    except TelegramError:
        logger.warning("Could not deliver diet calorie prompt", exc_info=True)
        finish_conversation(update, context, "diet")
        return ConversationHandler.END
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

    return await _prompt_for_macros(update, context, calories)


async def skip_calories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /skip for calories."""
    return await _prompt_for_macros(update, context, None)


async def _prompt_for_macros(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    calories: int | None,
) -> int:
    """Store pending calories and ask for optional protein/carbs/fat grams."""
    context.user_data["diet_calories"] = calories
    try:
        await update.message.reply_text(
            "🥩 Macros in grams? Send protein carbs fat (e.g. 25 80 15), "
            "or /skip if unsure."
        )
    except TelegramError:
        logger.warning("Could not deliver diet macro prompt", exc_info=True)
        finish_conversation(update, context, "diet")
        return ConversationHandler.END
    return MACROS


async def receive_macros(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive protein, carbohydrate, and fat grams in that order."""
    parts = update.message.text.split()
    if len(parts) != 3:
        return await _macro_validation_error(
            update,
            context,
            "❌ Send exactly three values: protein carbs fat (e.g. 25 80 15).\n"
            "Or /skip if unsure.",
        )

    values: list[float] = []
    for raw, field_name in zip(parts, ("Protein", "Carbs", "Fat"), strict=True):
        value, error = _parse_macro_grams(raw, field_name)
        if error:
            return await _macro_validation_error(
                update,
                context,
                error + "\nOr /skip if unsure.",
            )
        assert value is not None
        values.append(value)

    food_items, calories = _pending_diet(context)
    return await _save_diet(
        update,
        context,
        calories,
        protein_g=values[0],
        carbs_g=values[1],
        fat_g=values[2],
        food_items=food_items,
    )


async def _macro_validation_error(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> int:
    """Keep the macro state when retry guidance is delivered successfully."""
    try:
        await update.message.reply_text(text)
    except TelegramError:
        logger.warning("Could not deliver diet macro validation", exc_info=True)
        finish_conversation(update, context, "diet")
        return ConversationHandler.END
    return MACROS


async def skip_macros(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save a guided diet entry without macro estimates."""
    food_items, calories = _pending_diet(context)
    return await _save_diet(
        update,
        context,
        calories,
        food_items=food_items,
    )


def _pending_diet(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, int | None]:
    """Return the guided food and calories stored before the macro state."""
    food_items = context.user_data["diet_food_items"]
    if "diet_calories" not in context.user_data:
        raise RuntimeError("Diet macro state is missing its pending meal data")
    calories = context.user_data["diet_calories"]
    if not isinstance(food_items, str) or (
        calories is not None
        and (not isinstance(calories, int) or isinstance(calories, bool))
    ):
        raise RuntimeError("Diet macro state contains invalid pending meal data")
    return food_items, calories


async def _save_diet(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    calories: int | None,
    *,
    protein_g: float | None = None,
    carbs_g: float | None = None,
    fat_g: float | None = None,
    food_items: str | None = None,
) -> int:
    """Save diet entry and confirm."""
    db = context.bot_data["db"]
    user_id = update.effective_user.id
    meal_type = context.user_data["diet_meal_type"]
    if food_items is None:
        pending_food = context.user_data["diet_food_items"]
        if not isinstance(pending_food, str):
            raise RuntimeError("Diet state contains invalid food items")
        food_items = pending_food

    await db.log_diet(
        user_id,
        meal_type,
        food_items,
        calories,
        protein_g=protein_g,
        carbs_g=carbs_g,
        fat_g=fat_g,
    )
    context.user_data.pop("diet_meal_type", None)
    context.user_data.pop("diet_food_items", None)
    context.user_data.pop("diet_calories", None)

    try:
        await reply_html(
            update.message,
            _confirmation(
                meal_type,
                food_items,
                calories,
                protein_g,
                carbs_g,
                fat_g,
            ),
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
        MACROS: [
            CommandHandler("skip", skip_macros),
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_macros),
        ],
        ConversationHandler.TIMEOUT: [TypeHandler(Update, timeout_handler)],
    },
    fallbacks=[
        cancel_handler,
        CommandHandler("diet", active_conversation_hint, filters=AUTH_FILTER),
    ],
    conversation_timeout=CONVERSATION_TIMEOUT,
    # per_message=False is correct: the code manually validates callback
    # ownership via ``diet_meal_message_id`` in user_data rather than
    # relying on PTB's per-message tracking.
    per_message=False,
)
