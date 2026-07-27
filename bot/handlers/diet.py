"""
/diet handler — ConversationHandler with shortcut parsing.

Shortcut: /diet lunch dal+rice 650 p=25 c=80 f=15
Guided:   /diet → MEAL_TYPE → FOOD_ITEMS → CALORIES → MACROS → done
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone

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
    mutation_source,
    parse_int,
    reply_html,
    timeout_handler,
)
from .catalog import (
    resolve_catalog_diet_entry,
    resolve_food_diet_entry,
    resolve_recipe_diet_entry,
)
from ..keyboards import (
    diet_save_keyboard,
    food_choice_keyboard,
    food_portion_keyboard,
    log_another_keyboard,
    meal_type_keyboard,
    recipe_quantity_keyboard,
)
from ..config import CONVERSATION_TIMEOUT
from ..nutrition import MAX_LOG_CALORIES, MAX_LOG_MACRO_GRAMS, NutritionError
from .. import suggestions

logger = logging.getLogger(__name__)

# Conversation states. MEAL_TYPE..MACROS are the original free-text flow;
# FOOD_CHOICE..LOG_ANOTHER are the tap-first flow layered on top of it.
(
    MEAL_TYPE,
    FOOD_ITEMS,
    CALORIES,
    MACROS,
    FOOD_CHOICE,
    PORTION_CHOICE,
    CUSTOM_AMOUNT,
    CONFIRM_ITEM,
    LOG_ANOTHER,
) = range(9)

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
_MEAL_EMOJI = {"breakfast": "🌅", "lunch": "🌞", "dinner": "🌙", "snack": "🍿"}

# Tap-flow callback shapes (owner id is always re-validated before any lookup).
_DFOOD_RE = re.compile(r"^dfood_(\d+)_(\d+)$")
_DRECIPE_RE = re.compile(r"^drecipe_(\d+)_(\d+)$")
_DPORT_RE = re.compile(r"^dport_(\d+)_(\d+)$")
_DRECENT_RE = re.compile(r"^drecent_(\d+)_(\d+)$")
_DMORE_RE = re.compile(r"^dmore_(\d+)_(yes|no)$")
# Any diet tap: action word + owner id (used for owner checks and stale taps).
_DIET_TAP_RE = re.compile(
    r"^d(food|recipe|type|port|custom|back|rq|save|cancel|more|add|recent|pin|hide)"
    r"_(\d+)"
)


def _utc_now() -> datetime:
    # Naive UTC, matching the naive UTC timestamps stored in logged_at.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def _ranked_choices(
    context: ContextTypes.DEFAULT_TYPE, uid: int, meal_type: str
) -> list[dict]:
    """Saved foods/recipes as ranked, hidden-filtered choice dicts for the list.

    With personalization off, returns the plain alphabetical union (foods then
    recipes) unchanged. With it on, ranks by the user's completed history, pins,
    and recency, and drops hidden sources.
    """
    db = context.bot_data["db"]
    foods = await db.list_foods(uid)
    recipes = await db.list_recipes(uid)
    if not foods and not recipes:
        return []
    if not await db.get_suggestions_enabled(uid):
        return [
            {"source_type": "food", "id": f["id"], "name": f["name"]} for f in foods
        ] + [
            {"source_type": "recipe", "id": r["id"], "name": r["name"]}
            for r in recipes
        ]

    stats = await db.get_diet_item_stats(uid, meal_type)
    prefs = await db.get_food_preferences(uid)
    now = _utc_now()
    names: dict[tuple[str, int], str] = {}
    candidates: list[suggestions.Candidate] = []
    for source_type, rows in (("food", foods), ("recipe", recipes)):
        for row in rows:
            key = (source_type, row["id"])
            names[key] = row["name"]
            stat = stats.get(key, {})
            pref = prefs.get(key, {})
            candidates.append(
                suggestions.Candidate(
                    source_type=source_type,
                    source_id=row["id"],
                    name_key=str(row.get("name_key") or row["name"]).casefold(),
                    meal_uses=int(stat.get("meal_uses") or 0),
                    total_uses=int(stat.get("total_uses") or 0),
                    last_used=_parse_ts(stat.get("last_used")),
                    is_pinned=bool(pref.get("is_pinned")),
                    hidden=bool(pref.get("hidden")),
                )
            )
    return [
        {
            "source_type": c.source_type,
            "id": c.source_id,
            "name": names[(c.source_type, c.source_id)],
        }
        for c in suggestions.rank(candidates, now)
    ]


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


def _meal_body(
    meal_type: str,
    food_items: str,
    calories: int | None,
    protein_g: float | None = None,
    carbs_g: float | None = None,
    fat_g: float | None = None,
) -> str:
    """Build the shared meal/food/calorie/macro lines used by both a confirmation
    and a pre-save preview."""
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
        f"🍽️ Meal: <b>{escape_html(meal_type.title())}</b>\n"
        f"🥘 Food: <b>{escape_html(food_items)}</b>{calories_line}{macros_line}"
    )


def _confirmation(
    meal_type: str,
    food_items: str,
    calories: int | None,
    protein_g: float | None = None,
    carbs_g: float | None = None,
    fat_g: float | None = None,
) -> str:
    """Build a safe HTML confirmation for a saved diet log."""
    body = _meal_body(meal_type, food_items, calories, protein_g, carbs_g, fat_g)
    return f"✅ <b>Diet logged!</b>\n{body}"




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
                source=mutation_source(update),
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
            source=mutation_source(update),
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
    return await _begin_diet_flow(update, context)


async def _begin_diet_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Show the meal-type keyboard from either /diet or the Diet menu tap.

    Uses ``effective_message`` so it works for a callback entry (where
    ``update.message`` is ``None``). Also reused by the keep-logging loop.
    """
    activate_conversation(update, context, "diet")
    try:
        prompt = await reply_html(
            update.effective_message,
            "🍽️ <b>Log Meal</b>\n\nWhich meal?",
            reply_markup=meal_type_keyboard(update.effective_user.id),
        )
    except BaseException:
        finish_conversation(update, context, "diet")
        raise
    context.user_data["diet_meal_message_id"] = prompt.message_id
    return MEAL_TYPE


@authorized_callback
async def diet_menu_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Enter the guided diet flow from a main-menu 'Diet' tap."""
    query = update.callback_query
    await query.answer()
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)
    if not await conversation_available(update, context, "diet"):
        return ConversationHandler.END
    return await _begin_diet_flow(update, context)


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
    return await _prompt_food_choice(update, context, query.message, meal_type)


async def _prompt_food_choice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: object,
    meal_type: str,
) -> int:
    """Offer saved foods/recipes as taps, or fall back to the free-text prompt.

    When the user has no saved nutrition, the original type-what-you-ate flow is
    preserved unchanged.
    """
    uid = update.effective_user.id
    choices = await _ranked_choices(context, uid, meal_type)
    emoji = _MEAL_EMOJI.get(meal_type, "🍽️")
    title = escape_html(meal_type.title())

    if choices:
        prompt = await _send_tap_keyboard(
            update,
            context,
            message,
            f"{emoji} <b>{title}</b> — pick a saved item, or ✍️ type it:",
            food_choice_keyboard(uid, choices),
        )
        return FOOD_CHOICE if prompt is not None else ConversationHandler.END

    try:
        await reply_html(
            message,
            f"{emoji} <b>{title}</b> — what did you eat?",
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
        source=mutation_source(update),
    )

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
    return await _offer_log_another(update, context, update.message)


# ---------------------------------------------------------------------------
# Tap-first flow: select a saved food/recipe, choose a quantity, preview, save
# ---------------------------------------------------------------------------
def _clear_diet_entry_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop one meal's working data while keeping the conversation active."""
    for key in (
        "diet_meal_type",
        "diet_food_items",
        "diet_calories",
        "diet_sel_kind",
        "diet_sel_id",
        "diet_recent_qtys",
        "diet_items",
        "diet_meal_message_id",
        "diet_ui_message_id",
    ):
        context.user_data.pop(key, None)


async def _send_tap_keyboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: object,
    text: str,
    keyboard: object,
) -> object | None:
    """Send a tracked keyboard message, or end the flow if delivery fails.

    On success the message id is stored as ``diet_ui_message_id`` so the next
    tap can be validated against exactly this message (stale-keyboard defense).
    """
    try:
        prompt = await reply_html(message, text, reply_markup=keyboard)
    except TelegramError:
        logger.warning("Could not deliver diet tap keyboard", exc_info=True)
        finish_conversation(update, context, "diet")
        return None
    context.user_data["diet_ui_message_id"] = prompt.message_id
    return prompt


async def _consume_diet_tap(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    check_ui: bool = True,
) -> object | None:
    """Validate a diet tap's owner and (optionally) the live UI message.

    Returns the answered callback query on success, or ``None`` after telling
    the user the menu belongs to someone else / has expired. Never trusts an id
    from a button — the owner id embedded in the callback must match the acting
    user, and the tap must land on the message we last sent.
    """
    query = update.callback_query
    match = _DIET_TAP_RE.match(query.data or "")
    if match is None or int(match.group(2)) != update.effective_user.id:
        await query.answer("This menu belongs to another user.", show_alert=True)
        return None
    if check_ui:
        expected = context.user_data.get("diet_ui_message_id")
        actual = getattr(query.message, "message_id", None)
        if expected is None or actual != expected:
            await query.answer("This menu has expired.", show_alert=True)
            await _remove_callback_markup(query)
            return None
    await query.answer()
    return query


@authorized_callback
async def choose_food(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A saved food was tapped: show its portions or ask for a custom amount."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return FOOD_CHOICE
    food_id = int(_DFOOD_RE.fullmatch(query.data).group(2))
    db = context.bot_data["db"]
    uid = update.effective_user.id
    food = await db.get_food_by_id(uid, food_id)
    if food is None:
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)

    await _remove_callback_markup(query)
    context.user_data["diet_sel_kind"] = "food"
    context.user_data["diet_sel_id"] = food_id
    portions = await db.get_food_portions(uid, food_id)
    recent = await db.get_recent_item_quantities(uid, "food", food_id)
    context.user_data["diet_recent_qtys"] = recent
    if portions or recent:
        pref = await db.get_food_preference(uid, "food", food_id) or {}
        prompt = await _send_tap_keyboard(
            update,
            context,
            query.message,
            f"🥗 <b>{escape_html(food['name'])}</b> — how much?",
            food_portion_keyboard(
                uid,
                portions,
                recent,
                is_pinned=bool(pref.get("is_pinned")),
                hidden=bool(pref.get("hidden")),
            ),
        )
        return PORTION_CHOICE if prompt is not None else ConversationHandler.END
    return await _prompt_custom_amount_text(
        update, context, query.message, food["name"], food["base_unit"]
    )


@authorized_callback
async def choose_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A saved recipe was tapped: offer its yield quantity or a custom amount."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return FOOD_CHOICE
    recipe_id = int(_DRECIPE_RE.fullmatch(query.data).group(2))
    db = context.bot_data["db"]
    uid = update.effective_user.id
    recipe = await db.get_recipe_by_id(uid, recipe_id)
    if recipe is None:
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)

    await _remove_callback_markup(query)
    context.user_data["diet_sel_kind"] = "recipe"
    context.user_data["diet_sel_id"] = recipe_id
    recent = await db.get_recent_item_quantities(uid, "recipe", recipe_id)
    context.user_data["diet_recent_qtys"] = recent
    pref = await db.get_food_preference(uid, "recipe", recipe_id) or {}
    prompt = await _send_tap_keyboard(
        update,
        context,
        query.message,
        f"🍲 <b>{escape_html(recipe['name'])}</b> — how much?",
        recipe_quantity_keyboard(
            uid,
            recipe["yield_unit"],
            recent,
            is_pinned=bool(pref.get("is_pinned")),
            hidden=bool(pref.get("hidden")),
        ),
    )
    return PORTION_CHOICE if prompt is not None else ConversationHandler.END


@authorized_callback
async def type_food_instead(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Escape the tap flow: fall back to the original free-text food prompt."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return FOOD_CHOICE
    await _remove_callback_markup(query)
    context.user_data.pop("diet_ui_message_id", None)
    meal_type = context.user_data.get("diet_meal_type", "")
    emoji = _MEAL_EMOJI.get(meal_type, "🍽️")
    title = escape_html(meal_type.title()) if meal_type else "Meal"
    try:
        await reply_html(
            query.message, f"{emoji} <b>{title}</b> — what did you eat?"
        )
    except TelegramError:
        logger.warning("Could not deliver diet food prompt", exc_info=True)
        finish_conversation(update, context, "diet")
        return ConversationHandler.END
    return FOOD_ITEMS


@authorized_callback
async def back_to_food_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Return from the quantity screen to the saved-item list."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return PORTION_CHOICE
    await _remove_callback_markup(query)
    return await _reprompt_food_choice(update, context, query.message)


async def _reprompt_food_choice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: object,
) -> int:
    """Re-show the saved-item list (after Back, or a vanished selection)."""
    uid = update.effective_user.id
    context.user_data.pop("diet_sel_kind", None)
    context.user_data.pop("diet_sel_id", None)
    context.user_data.pop("diet_recent_qtys", None)
    meal_type = context.user_data.get("diet_meal_type", "")
    choices = await _ranked_choices(context, uid, meal_type)
    if not choices:
        context.user_data.pop("diet_ui_message_id", None)
        try:
            await reply_html(message, "🍽️ What did you eat?")
        except TelegramError:
            finish_conversation(update, context, "diet")
            return ConversationHandler.END
        return FOOD_ITEMS
    prompt = await _send_tap_keyboard(
        update,
        context,
        message,
        "Pick a saved item, or ✍️ type it:",
        food_choice_keyboard(uid, choices),
    )
    return FOOD_CHOICE if prompt is not None else ConversationHandler.END


@authorized_callback
async def choose_portion(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """A named food portion was tapped: resolve nutrition and preview it."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return PORTION_CHOICE
    portion_id = int(_DPORT_RE.fullmatch(query.data).group(2))
    db = context.bot_data["db"]
    uid = update.effective_user.id
    food_id = context.user_data.get("diet_sel_id")
    food = await db.get_food_by_id(uid, food_id) if food_id is not None else None
    if food is None:
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)
    portions = await db.get_food_portions(uid, food_id)
    portion = next((p for p in portions if p["id"] == portion_id), None)
    if portion is None:
        await query.answer("That portion is no longer available.", show_alert=True)
        return PORTION_CHOICE
    try:
        entry = resolve_food_diet_entry(food, portions, ["1", portion["name"]])
    except NutritionError as exc:
        await query.answer(str(exc)[:190], show_alert=True)
        return PORTION_CHOICE
    await _remove_callback_markup(query)
    return await _show_item_preview(update, context, query.message, entry)


@authorized_callback
async def recipe_quick_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """The '1 <yield_unit>' recipe quick button: resolve nutrition and preview."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return PORTION_CHOICE
    db = context.bot_data["db"]
    uid = update.effective_user.id
    recipe_id = context.user_data.get("diet_sel_id")
    recipe = (
        await db.get_recipe_by_id(uid, recipe_id) if recipe_id is not None else None
    )
    if recipe is None:
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)
    ingredients = await db.get_recipe_ingredients(uid, recipe_id)
    try:
        entry = resolve_recipe_diet_entry(
            recipe, ingredients, ["1", recipe["yield_unit"]]
        )
    except NutritionError as exc:
        await query.answer(str(exc)[:190], show_alert=True)
        return PORTION_CHOICE
    await _remove_callback_markup(query)
    return await _show_item_preview(update, context, query.message, entry)


@authorized_callback
async def prompt_custom_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """The 'Custom amount' button: ask the user to type a quantity."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return PORTION_CHOICE
    await _remove_callback_markup(query)
    kind = context.user_data.get("diet_sel_kind")
    db = context.bot_data["db"]
    uid = update.effective_user.id
    sel_id = context.user_data.get("diet_sel_id")
    if kind == "food" and sel_id is not None:
        food = await db.get_food_by_id(uid, sel_id)
        if food is None:
            return await _reprompt_food_choice(update, context, query.message)
        return await _prompt_custom_amount_text(
            update, context, query.message, food["name"], food["base_unit"]
        )
    if kind == "recipe" and sel_id is not None:
        recipe = await db.get_recipe_by_id(uid, sel_id)
        if recipe is None:
            return await _reprompt_food_choice(update, context, query.message)
        return await _prompt_custom_amount_text(
            update, context, query.message, recipe["name"], recipe["yield_unit"]
        )
    return await _reprompt_food_choice(update, context, query.message)


async def _prompt_custom_amount_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: object,
    name: str,
    unit_hint: str,
) -> int:
    """Ask for a typed quantity and move to the CUSTOM_AMOUNT text state."""
    context.user_data.pop("diet_ui_message_id", None)
    try:
        await reply_html(
            message,
            f"✍️ How much <b>{escape_html(name)}</b>? "
            f"e.g. <code>200 {escape_html(unit_hint)}</code> or "
            "<code>1 medium</code>.",
        )
    except TelegramError:
        logger.warning("Could not deliver custom-amount prompt", exc_info=True)
        finish_conversation(update, context, "diet")
        return ConversationHandler.END
    return CUSTOM_AMOUNT


async def _resolve_selected(
    db: object,
    uid: int,
    kind: object,
    sel_id: object,
    tokens: list[str],
):
    """Resolve tokens against the selected food/recipe (shared by custom + recent)."""
    if kind == "food" and sel_id is not None:
        food = await db.get_food_by_id(uid, sel_id)
        if food is None:
            raise NutritionError("That saved food is no longer available.")
        portions = await db.get_food_portions(uid, sel_id)
        return resolve_food_diet_entry(food, portions, tokens)
    if kind == "recipe" and sel_id is not None:
        recipe = await db.get_recipe_by_id(uid, sel_id)
        if recipe is None:
            raise NutritionError("That saved recipe is no longer available.")
        ingredients = await db.get_recipe_ingredients(uid, sel_id)
        return resolve_recipe_diet_entry(recipe, ingredients, tokens)
    raise NutritionError("Lost track of the item.")


async def receive_custom_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Resolve a typed quantity for the selected food/recipe, then preview."""
    tokens = update.message.text.split()
    kind = context.user_data.get("diet_sel_kind")
    sel_id = context.user_data.get("diet_sel_id")
    db = context.bot_data["db"]
    uid = update.effective_user.id
    if kind not in ("food", "recipe") or sel_id is None:
        finish_conversation(update, context, "diet")
        await update.message.reply_text(
            "⚠️ Lost track of the item. Start again with /diet."
        )
        return ConversationHandler.END
    try:
        entry = await _resolve_selected(db, uid, kind, sel_id, tokens)
    except NutritionError as exc:
        await update.message.reply_text(
            f"❌ {exc}\nTry again, e.g. 200 g or 1 medium."
        )
        return CUSTOM_AMOUNT
    return await _show_item_preview(
        update, context, update.effective_message, entry
    )


@authorized_callback
async def use_recent_quantity(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """A recent-quantity button was tapped: resolve that amount and preview."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return PORTION_CHOICE
    index = int(_DRECENT_RE.fullmatch(query.data).group(2))
    recent = context.user_data.get("diet_recent_qtys") or []
    if index < 0 or index >= len(recent):
        await query.answer("That quantity is no longer available.", show_alert=True)
        return PORTION_CHOICE
    quantity = recent[index]
    tokens = [f"{float(quantity['entered_amount']):g}", str(quantity["entered_unit"])]
    db = context.bot_data["db"]
    uid = update.effective_user.id
    try:
        entry = await _resolve_selected(
            db,
            uid,
            context.user_data.get("diet_sel_kind"),
            context.user_data.get("diet_sel_id"),
            tokens,
        )
    except NutritionError as exc:
        await query.answer(str(exc)[:190], show_alert=True)
        return PORTION_CHOICE
    await _remove_callback_markup(query)
    return await _show_item_preview(update, context, query.message, entry)


async def _rerender_quantity_screen(
    update: Update, context: ContextTypes.DEFAULT_TYPE, query: object
) -> int:
    """Redraw the current quantity keyboard in place after a pin/hide toggle."""
    db = context.bot_data["db"]
    uid = update.effective_user.id
    kind = context.user_data.get("diet_sel_kind")
    sel_id = context.user_data.get("diet_sel_id")
    if kind not in ("food", "recipe") or sel_id is None:
        return PORTION_CHOICE
    pref = await db.get_food_preference(uid, kind, sel_id) or {}
    recent = context.user_data.get("diet_recent_qtys") or []
    if kind == "food":
        portions = await db.get_food_portions(uid, sel_id)
        keyboard = food_portion_keyboard(
            uid,
            portions,
            recent,
            is_pinned=bool(pref.get("is_pinned")),
            hidden=bool(pref.get("hidden")),
        )
    else:
        recipe = await db.get_recipe_by_id(uid, sel_id)
        if recipe is None:
            return PORTION_CHOICE
        keyboard = recipe_quantity_keyboard(
            uid,
            recipe["yield_unit"],
            recent,
            is_pinned=bool(pref.get("is_pinned")),
            hidden=bool(pref.get("hidden")),
        )
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except TelegramError:
        logger.debug("Could not redraw quantity keyboard", exc_info=True)
    return PORTION_CHOICE


@authorized_callback
async def toggle_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pin/unpin the selected food/recipe so it ranks first next time."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return PORTION_CHOICE
    kind = context.user_data.get("diet_sel_kind")
    sel_id = context.user_data.get("diet_sel_id")
    if kind not in ("food", "recipe") or sel_id is None:
        return await _reprompt_food_choice(update, context, query.message)
    db = context.bot_data["db"]
    uid = update.effective_user.id
    pref = await db.get_food_preference(uid, kind, sel_id) or {}
    new_pinned = not bool(pref.get("is_pinned"))
    await db.set_food_preference(uid, kind, sel_id, is_pinned=new_pinned)
    await query.answer("📌 Pinned" if new_pinned else "Unpinned")
    return await _rerender_quantity_screen(update, context, query)


@authorized_callback
async def toggle_hide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Hide/unhide the selected food/recipe from future suggestions."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return PORTION_CHOICE
    kind = context.user_data.get("diet_sel_kind")
    sel_id = context.user_data.get("diet_sel_id")
    if kind not in ("food", "recipe") or sel_id is None:
        return await _reprompt_food_choice(update, context, query.message)
    db = context.bot_data["db"]
    uid = update.effective_user.id
    pref = await db.get_food_preference(uid, kind, sel_id) or {}
    new_hidden = not bool(pref.get("hidden"))
    await db.set_food_preference(uid, kind, sel_id, hidden=new_hidden)
    await query.answer(
        "🙈 Hidden from suggestions" if new_hidden else "👁 Shown again"
    )
    return await _rerender_quantity_screen(update, context, query)


def _meal_total(items: list[dict], field: str, *, integer: bool = False):
    """Sum one nutrient across items, or None if any item's value is unknown."""
    values = [item.get(field) for item in items]
    if any(value is None for value in values):
        return None
    summed = sum(float(value) for value in values)
    return int(round(summed)) if integer else round(summed, 2)


def _meal_totals(items: list[dict]) -> dict:
    return {
        "calories": _meal_total(items, "calories", integer=True),
        "protein_g": _meal_total(items, "protein_g"),
        "carbs_g": _meal_total(items, "carbs_g"),
        "fat_g": _meal_total(items, "fat_g"),
    }


def _meal_summary(items: list[dict]) -> str:
    return ", ".join(str(item["display_name"]) for item in items)


def _meal_preview(meal_type: str, items: list[dict]) -> str:
    """Preview the whole meal draft: each item, running total, next actions."""
    lines = [
        f"👀 <b>Preview — {escape_html(meal_type.title())}</b>",
    ]
    for index, item in enumerate(items, 1):
        cal = item.get("calories")
        cal_text = f"{cal} kcal" if cal is not None else "kcal ?"
        macros = " · ".join(
            f"{label} {_format_grams(item[key]) if item.get(key) is not None else '?'}"
            for label, key in (("P", "protein_g"), ("C", "carbs_g"), ("F", "fat_g"))
        )
        lines.append(
            f"{index}. <b>{escape_html(item['display_name'])}</b> — "
            f"{cal_text} · {macros}"
        )
    totals = _meal_totals(items)
    total_cal = totals["calories"]
    total_cal_text = f"<b>{total_cal}</b>" if total_cal is not None else "<b>?</b>"
    total_macros = " · ".join(
        f"{label} {_format_grams(totals[key]) if totals[key] is not None else '?'} g"
        for label, key in (("P", "protein_g"), ("C", "carbs_g"), ("F", "fat_g"))
    )
    lines.append(f"🔥 Total: {total_cal_text} kcal · 🥩 {total_macros}")
    lines.append("➕ Add another item, or ✅ Save meal.")
    return "\n".join(lines)


async def _show_item_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: object,
    entry: object,
) -> int:
    """Append the resolved item to the meal draft and preview the whole meal."""
    meal_type = context.user_data.get("diet_meal_type", "")
    items: list[dict] = context.user_data.setdefault("diet_items", [])
    items.append(entry.as_item())
    prompt = await _send_tap_keyboard(
        update,
        context,
        message,
        _meal_preview(meal_type, items),
        diet_save_keyboard(update.effective_user.id),
    )
    return CONFIRM_ITEM if prompt is not None else ConversationHandler.END


@authorized_callback
async def add_another_item(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Keep the meal draft and return to the saved-item list for another item."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return CONFIRM_ITEM
    await _remove_callback_markup(query)
    return await _reprompt_food_choice(update, context, query.message)


@authorized_callback
async def save_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Persist the whole meal (header + items), then offer to log another meal."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return CONFIRM_ITEM
    items = context.user_data.get("diet_items")
    meal_type = context.user_data.get("diet_meal_type")
    if not isinstance(items, list) or not items or not isinstance(meal_type, str):
        await _remove_callback_markup(query)
        finish_conversation(update, context, "diet")
        try:
            await query.message.reply_text(
                "⚠️ Lost the pending meal. Start again with /diet."
            )
        except TelegramError:
            logger.warning("Could not report lost diet meal", exc_info=True)
        return ConversationHandler.END

    await _remove_callback_markup(query)
    db = context.bot_data["db"]
    await db.log_diet_with_items(
        update.effective_user.id,
        meal_type,
        items,
        source=mutation_source(update),
    )
    totals = _meal_totals(items)
    try:
        await reply_html(
            query.message,
            _confirmation(
                meal_type,
                _meal_summary(items),
                totals["calories"],
                totals["protein_g"],
                totals["carbs_g"],
                totals["fat_g"],
            ),
        )
    except TelegramError:
        logger.warning("Could not deliver diet confirmation", exc_info=True)
    return await _offer_log_another(update, context, query.message)


async def _offer_log_another(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: object,
) -> int:
    """After a save, keep the flow open with a one-tap 'Log another' / 'Done'.

    Clears the finished meal's data but leaves the conversation active; the
    normal 300s idle timeout closes it when the user stops logging.
    """
    _clear_diet_entry_data(context)
    prompt = await _send_tap_keyboard(
        update,
        context,
        message,
        "➕ Log another meal?",
        log_another_keyboard(update.effective_user.id),
    )
    return LOG_ANOTHER if prompt is not None else ConversationHandler.END


@authorized_callback
async def log_another(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Keep-logging loop: reopen the meal picker, or finish the diet flow."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return LOG_ANOTHER
    choice = _DMORE_RE.fullmatch(query.data).group(2)
    await _remove_callback_markup(query)
    if choice == "yes":
        _clear_diet_entry_data(context)
        return await _begin_diet_flow(update, context)
    finish_conversation(update, context, "diet")
    try:
        await reply_html(query.message, "✅ <b>Done logging.</b>")
    except TelegramError:
        logger.warning("Could not deliver diet done notice", exc_info=True)
    return ConversationHandler.END


@authorized_callback
async def cancel_diet_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancel button available on the tap keyboards (owner-checked, forgiving)."""
    query = await _consume_diet_tap(update, context, check_ui=False)
    if query is None:
        return ConversationHandler.END
    await _remove_callback_markup(query)
    finish_conversation(update, context, "diet")
    try:
        await query.message.reply_text("✖️ Cancelled.")
    except TelegramError:
        logger.warning("Could not deliver diet cancellation", exc_info=True)
    return ConversationHandler.END


@authorized_callback
async def stale_diet_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Answer a diet tap whose conversation already ended (timeout/cancel)."""
    query = update.callback_query
    match = _DIET_TAP_RE.match(query.data or "")
    if match is None:
        await query.answer("This menu is no longer valid.", show_alert=True)
        return
    if int(match.group(2)) != update.effective_user.id:
        await query.answer("This menu belongs to another user.", show_alert=True)
        return
    await query.answer("This menu has expired.", show_alert=True)
    await _remove_callback_markup(query)


# ---------------------------------------------------------------------------
# ConversationHandler
# ---------------------------------------------------------------------------
diet_conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("diet", diet_command, filters=AUTH_FILTER),
        CallbackQueryHandler(diet_menu_entry, pattern=r"^menu_diet$"),
    ],
    states={
        MEAL_TYPE: [
            CallbackQueryHandler(
                receive_meal_type,
                pattern=r"^meal_\d+_(breakfast|lunch|dinner|snack)$",
            )
        ],
        FOOD_CHOICE: [
            CallbackQueryHandler(choose_food, pattern=r"^dfood_\d+_\d+$"),
            CallbackQueryHandler(choose_recipe, pattern=r"^drecipe_\d+_\d+$"),
            CallbackQueryHandler(type_food_instead, pattern=r"^dtype_\d+$"),
        ],
        PORTION_CHOICE: [
            CallbackQueryHandler(choose_portion, pattern=r"^dport_\d+_\d+$"),
            CallbackQueryHandler(use_recent_quantity, pattern=r"^drecent_\d+_\d+$"),
            CallbackQueryHandler(recipe_quick_amount, pattern=r"^drq_\d+$"),
            CallbackQueryHandler(prompt_custom_amount, pattern=r"^dcustom_\d+$"),
            CallbackQueryHandler(back_to_food_choice, pattern=r"^dback_\d+$"),
            CallbackQueryHandler(toggle_pin, pattern=r"^dpin_\d+$"),
            CallbackQueryHandler(toggle_hide, pattern=r"^dhide_\d+$"),
        ],
        CUSTOM_AMOUNT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_amount)
        ],
        CONFIRM_ITEM: [
            CallbackQueryHandler(add_another_item, pattern=r"^dadd_\d+$"),
            CallbackQueryHandler(save_item, pattern=r"^dsave_\d+$"),
        ],
        LOG_ANOTHER: [
            CallbackQueryHandler(log_another, pattern=r"^dmore_\d+_(yes|no)$"),
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
        CallbackQueryHandler(cancel_diet_callback, pattern=r"^dcancel_\d+$"),
        CommandHandler("diet", active_conversation_hint, filters=AUTH_FILTER),
    ],
    conversation_timeout=CONVERSATION_TIMEOUT,
    # per_message=False is correct: the code manually validates callback
    # ownership via ``diet_meal_message_id`` in user_data rather than
    # relying on PTB's per-message tracking.
    per_message=False,
)
