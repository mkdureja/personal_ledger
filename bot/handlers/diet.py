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
from typing import Mapping, Sequence

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

from ..callback_data import parse_base36
from .home import home_fallback_handlers
from .common import (
    AUTH_FILTER,
    DIET_NONMEAL_CONTROL_FILTER,
    MEAL_LABEL_FILTER,
    active_conversation_flow,
    active_conversation_hint,
    active_flow_control_interceptor,
    activate_conversation,
    authorized_callback,
    buttons_or_cancel_catchall,
    cancel_handler,
    conversation_available,
    escape_html,
    finish_conversation,
    mutation_source,
    parse_int,
    reply_html,
    timeout_handler,
    voice_mid_flow_interceptor,
)
from .receipts import (
    RECEIPT_CURRENT_PATTERN,
    RECEIPT_MORE_PATTERN,
    send_meal_receipt,
)
from .keep_food import keep_food_offers
from .catalog import (
    resolve_catalog_diet_entry,
    resolve_catalog_food_entry,
    resolve_food_diet_entry,
    resolve_recipe_diet_entry,
)
from ..keyboards import (
    current_values_keyboard,
    default_confirm_keyboard,
    default_menu_keyboard,
    diet_save_keyboard,
    food_choice_keyboard,
    search_empty_keyboard,
    food_portion_keyboard,
    log_another_keyboard,
    meal_type_keyboard,
    quick_confirm_keyboard,
    recipe_quantity_keyboard,
    reply_keyboard_remove,
)
from ..config import CONVERSATION_TIMEOUT, now_local, phase1_enabled_for
from ..meal_models import (
    CurrentCommitStatus,
    CurrentValueDecision,
    CurrentValueIssueCode,
    DefaultQuantity,
    DietEntryMode,
    QuickMealStatus,
    PREFERENCE_SOURCE_TYPES,
)
from ..services.meal_logging import infer_meal_type
from ..nutrition import (
    MAX_LOG_CALORIES,
    MAX_LOG_MACRO_GRAMS,
    NutritionError,
    format_decimal,
)
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
    SEARCH,
    # Phase 1b (appended, never renumbered — a stored state int must keep its
    # meaning across a restart).
    QUICK_CONFIRM,
    DEFAULT_MENU,
    DEFAULT_AMOUNT,
    DEFAULT_CONFIRM,
    CURRENT_VALUES_REVIEW,
) = range(15)

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

# Calories and all three macros are required on every log. Nutrition is the
# point of the ledger: a meal saved without it silently under-reports the day's
# totals, and no later analysis can tell an unknown from a zero. When the numbers
# are not to hand, save the food once with /food add (or use one from the shared
# catalog) and the app supplies them from then on — it never estimates them.
_REQUIRED_HINT = (
    "Nutrition is required. If you don't have the numbers, /cancel and save the "
    "food first with /food add — then logging it fills these in for you."
)
_CALORIE_PROMPT = f"🔥 How many calories?\n{_REQUIRED_HINT}"
_MACRO_HINT = (
    "If you don't have them, /skip — the meal saves with its calories and the "
    "macros stay blank rather than being guessed. Saving the food once with "
    "/food add fills them in for you every time after that."
)
_MACRO_PROMPT = (
    f"🥩 Macros in grams? Send protein carbs fat (e.g. 25 80 15).\n{_MACRO_HINT}"
)

# Tap-flow callback shapes (owner id is always re-validated before any lookup).
_DFOOD_RE = re.compile(r"^dfood_(\d+)_(\d+)$")
_DRECIPE_RE = re.compile(r"^drecipe_(\d+)_(\d+)$")
_DPORT_RE = re.compile(r"^dport_(\d+)_(\d+)$")
_DRECENT_RE = re.compile(r"^drecent_(\d+)_(\d+)$")
_DCATALOG_RE = re.compile(r"^dcatalog_(\d+)_(\d+)$")
_DMORE_RE = re.compile(r"^dmore_(\d+)_(yes|no)$")
_MAX_SEARCH_QUERY = 50
# Any diet tap: action word + owner id (used for owner checks and stale taps).
_DIET_TAP_RE = re.compile(
    r"^d(food|recipe|type|port|custom|back|rq|save|cancel|more|add|recent|pin"
    r"|hide|search|catalog)_(\d+)"
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
    """Saved foods/recipes/known catalog items as ranked, decorated choice dicts.

    With personalization off, returns the plain alphabetical union of the user's
    *own* foods and recipes — no learned catalog history, no frequency, no
    recency, because that is what "off" means. With it on, ranks everything the
    user has actually used by meal-type frequency, recency, and overall use,
    applies pins, and drops hidden sources.

    Either way the result is decorated with each source's stored "usual" from
    **one** batched ``get_food_preferences`` read — the same map the ranking
    already needs — so the picker never queries per displayed row and the keyboard
    layer performs no I/O.
    """
    db = context.bot_data["db"]
    foods = await db.list_foods(uid)
    recipes = await db.list_recipes(uid)
    prefs = await db.get_food_preferences(uid)
    if not await db.get_suggestions_enabled(uid):
        plain = [
            {"source_type": "food", "id": f["id"], "name": f["name"]} for f in foods
        ] + [
            {"source_type": "recipe", "id": r["id"], "name": r["name"]}
            for r in recipes
        ]
        return suggestions.annotate_defaults(plain, prefs)

    # A catalog food only becomes a suggestion once it appears in this user's
    # own history — the shared catalog itself stays behind Search — *or* once
    # they mark it a shortcut for this meal, which is the same statement made
    # deliberately instead of inferred from repetition.
    catalog = await db.get_user_catalog_history(uid)
    shortcuts = await db.get_meal_shortcuts(uid, meal_type)
    shortcut_rows = await db.get_shortcut_targets(uid, meal_type) if shortcuts else []
    if not foods and not recipes and not catalog and not shortcut_rows:
        return []

    stats = await db.get_diet_item_stats(uid, meal_type)
    now = _utc_now()
    names: dict[tuple[str, int], str] = {}
    candidates: list[suggestions.Candidate] = []
    seen: set[tuple[str, int]] = set()
    grouped: list[tuple[str, list]] = [
        ("food", list(foods)),
        ("recipe", list(recipes)),
        ("catalog", list(catalog)),
    ]
    # A shortcut may point at something absent from all three lists — a catalog
    # food never logged before is exactly the case this feature exists for — so
    # its resolved row joins the candidates carrying its own source type.
    for row in shortcut_rows:
        grouped.append((str(row["source_type"]), [row]))

    for source_type, rows in grouped:
        for row in rows:
            key = (source_type, row["id"])
            if key in seen:
                continue
            seen.add(key)
            names[key] = row["name"]
            stat = stats.get(key, {})
            # Catalog rows have no private preference row (see the v12 migration).
            pref = prefs.get(key, {}) if source_type != "catalog" else {}
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
                    is_meal_shortcut=key in shortcuts,
                )
            )
    return suggestions.annotate_defaults(
        [
            {
                "source_type": c.source_type,
                "id": c.source_id,
                "name": names[(c.source_type, c.source_id)],
                "is_meal_shortcut": c.is_meal_shortcut,
            }
            for c in suggestions.rank(candidates, now)
        ],
        prefs,
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

            await db.log_diet_with_items(
                user.id,
                meal_type,
                [entry.as_item()],
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

        # The one-shot form is the fastest way to log, which is exactly why it
        # has to carry complete nutrition: a shortcut that quietly writes a
        # partial row is worse than one that refuses. Caught here so the user
        # gets the syntax back, rather than the write path's generic refusal.
        missing = [
            label
            for label, value in (
                ("calories", calories),
                ("p=", macros["protein_g"]),
                ("c=", macros["carbs_g"]),
                ("f=", macros["fat_g"]),
            )
            if value is None
        ]
        if missing:
            await reply_html(
                update.message,
                f"❌ Missing {escape_html(', '.join(missing))}.\n"
                "Every meal needs calories and all three macros:\n"
                f"<code>/diet {escape_html(meal_type)} "
                f"{escape_html(food_items[:40])} 450 p=25 c=80 f=15</code>\n\n"
                "Or save the food once and let the app fill them in:\n"
                "<code>/food add oats per=100g kcal=389 p=16.9 c=66 f=6.9</code>",
            )
            return ConversationHandler.END

        freetext_child = {
            "source_type": "freetext",
            "source_id": None,
            "source_provider": None,
            "source_revision": None,
            "display_name": food_items,
            "entered_amount": None,
            "entered_unit": None,
            "resolved_base_amount": None,
            "resolved_base_unit": None,
            "calories": calories,
            "protein_g": macros["protein_g"],
            "carbs_g": macros["carbs_g"],
            "fat_g": macros["fat_g"],
        }
        await db.log_diet_with_items(
            user.id,
            meal_type,
            [freetext_child],
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
    context.user_data["diet_ui_revision"] = 0
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

    # Always show the choice keyboard so 🔎 Search and ✍️ Type are reachable even
    # with no personal foods yet — otherwise the shared catalog is unreachable by
    # tapping and the flow looks like the old free-text one.
    if choices:
        prompt_text = f"{emoji} <b>{title}</b> — pick a saved item, 🔎 search, or ✍️ type it:"
    else:
        prompt_text = (
            f"{emoji} <b>{title}</b> — 🔎 search the catalog or ✍️ type what you ate:"
        )
    prompt = await _send_tap_keyboard(
        update,
        context,
        message,
        prompt_text,
        food_choice_keyboard(
            uid,
            choices,
            manage=phase1_enabled_for(uid),
            revision=context.user_data.get("diet_ui_revision", 0),
            page=int(context.user_data.get("diet_choice_page", 0) or 0),
            paginate=phase1_enabled_for(uid),
            change_meal=phase1_enabled_for(uid),
            quick=_quick_mode(context, uid),
        ),
    )
    return FOOD_CHOICE if prompt is not None else ConversationHandler.END


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
        # A resolved catalog/food/recipe reference keeps its structured identity
        # (source type, id, provenance) as a child, joining any in-progress draft
        # so the same reference produces the same history whether tapped or typed.
        draft = context.user_data.get("diet_items")
        items = [*draft] if isinstance(draft, list) else []
        items.append(entry.as_item())
        return await _finish_structured_meal(update, context, items)

    context.user_data["diet_food_items"] = food
    try:
        await update.message.reply_text(_CALORIE_PROMPT)
    except TelegramError:
        logger.warning("Could not deliver diet calorie prompt", exc_info=True)
        finish_conversation(update, context, "diet")
        return ConversationHandler.END
    return CALORIES


async def receive_calories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive calorie count. Required — there is no way past this step."""
    text = update.message.text.strip()

    calories, err = parse_int(
        text,
        "Calories",
        max_value=MAX_MEAL_CALORIES,
    )
    if err:
        await update.message.reply_text(f"{err}\n{_REQUIRED_HINT}")
        return CALORIES

    return await _prompt_for_macros(update, context, calories)


async def _prompt_for_macros(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    calories: int,
) -> int:
    """Store pending calories and ask for the required protein/carbs/fat grams."""
    context.user_data["diet_calories"] = calories
    try:
        await update.message.reply_text(_MACRO_PROMPT)
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
            f"{_MACRO_HINT}",
        )

    values: list[float] = []
    for raw, field_name in zip(parts, ("Protein", "Carbs", "Fat"), strict=True):
        value, error = _parse_macro_grams(raw, field_name)
        if error:
            return await _macro_validation_error(
                update,
                context,
                f"{error}\n{_MACRO_HINT}",
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


async def skip_macros(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save the meal with its calories and no macros.

    Restored deliberately, and only here. Every other door — a saved food, a
    catalog row, a resolved item — still demands all four, because those are
    *definitions* and a hole in one spreads to every meal that uses it. This is
    the one place a person is typing a single meal by hand, and the realistic
    alternative to a blank macro is not a correct one: it is a number they
    estimated, which the ledger would then be unable to tell from a measured one.
    """
    food_items, calories = _pending_diet(context)
    return await _save_diet(
        update,
        context,
        calories,
        protein_g=None,
        carbs_g=None,
        fat_g=None,
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


def _skip_retired(state: int):
    """Answer a habitual ``/skip`` instead of letting it fall through silently.

    ``/skip`` used to be the documented way past both nutrition steps, so both
    users have it in their fingers. Left unhandled it would be swallowed by the
    command filter and look like the bot had frozen; this says plainly that the
    step is now required and keeps the conversation where it is.
    """

    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        try:
            await update.message.reply_text(
                "ℹ️ /skip is gone — nutrition is required now, so the ledger can "
                f"total your macros.\n{_REQUIRED_HINT}"
            )
        except TelegramError:
            logger.warning("Could not deliver the retired-skip notice", exc_info=True)
            finish_conversation(update, context, "diet")
            return ConversationHandler.END
        return state

    return _handler


def _pending_diet(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, int]:
    """Return the guided food and calories stored before the macro state."""
    food_items = context.user_data["diet_food_items"]
    if "diet_calories" not in context.user_data:
        raise RuntimeError("Diet macro state is missing its pending meal data")
    calories = context.user_data["diet_calories"]
    if (
        not isinstance(food_items, str)
        or not isinstance(calories, int)
        or isinstance(calories, bool)
    ):
        raise RuntimeError("Diet macro state contains invalid pending meal data")
    return food_items, calories


async def _finish_structured_meal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    items: list[dict],
) -> int:
    """Persist a structured meal (header + children), confirm, and offer the loop.

    Shared by the tap flow's typed and catalog-reference escapes so every
    completed meal keeps item-level provenance regardless of how it was entered.
    """
    db = context.bot_data["db"]
    meal_type = context.user_data["diet_meal_type"]
    meal_id = await db.log_diet_with_items(
        update.effective_user.id, meal_type, items, source=mutation_source(update)
    )
    totals = _meal_totals(items)
    try:
        await reply_html(
            update.effective_message,
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
    return await _offer_log_another(
        update, context, update.effective_message, meal_id=meal_id, items=items
    )


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
    """Save a guided free-text diet entry and confirm.

    If a multi-item tap draft is already in progress (the user tapped items and
    then chose "Type it"), the typed entry joins that draft as a structured
    ``freetext`` child rather than replacing it — otherwise the previously
    confirmed items would be silently dropped.
    """
    if food_items is None:
        pending_food = context.user_data["diet_food_items"]
        if not isinstance(pending_food, str):
            raise RuntimeError("Diet state contains invalid food items")
        food_items = pending_food

    draft = context.user_data.get("diet_items")
    child = {
        "source_type": "freetext",
        "source_id": None,
        "source_provider": None,
        "source_revision": None,
        "display_name": food_items,
        "entered_amount": None,
        "entered_unit": None,
        "resolved_base_amount": None,
        "resolved_base_unit": None,
        "calories": calories,
        "protein_g": protein_g,
        "carbs_g": carbs_g,
        "fat_g": fat_g,
    }
    items = [*draft, child] if isinstance(draft, list) and draft else [child]
    return await _finish_structured_meal(update, context, items)


# ---------------------------------------------------------------------------
# Tap-first flow: select a saved food/recipe, choose a quantity, preview, save
# ---------------------------------------------------------------------------
_DIET_ENTRY_KEYS = (
    "diet_meal_type",
    "diet_food_items",
    "diet_calories",
    "diet_sel_kind",
    "diet_sel_id",
    "diet_recent_qtys",
    "diet_items",
    "diet_meal_message_id",
    "diet_ui_message_id",
    "diet_ui_revision",
    "diet_entry_mode",
    "diet_choice_page",
    # Phase 1b working data. Drafts and pending values are intentionally
    # in-memory only: after a restart their callbacks are simply stale.
    "diet_quick_pending_item",
    "diet_default_source_type",
    "diet_default_source_id",
    "diet_pending_default",
    "diet_current_source_meal_id",
    "diet_current_child_ids",
    "diet_current_decisions",
    "diet_current_digest",
    "diet_edit_index",
)


def _clear_diet_entry_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop one meal's working data while keeping the conversation active."""
    for key in _DIET_ENTRY_KEYS:
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

    context.user_data["diet_sel_kind"] = "food"
    context.user_data["diet_sel_id"] = food_id
    # In Quick mode a saved "usual" makes this tap the whole interaction: the
    # keyboard stays up until the write succeeds so a failure can be retried.
    if _quick_mode(context, uid):
        handled = await _try_quick_default(update, context, query, "food", food_id)
        if handled is not None:
            return handled

    await _remove_callback_markup(query)
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
                revision=context.user_data.get("diet_ui_revision", 0),
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

    context.user_data["diet_sel_kind"] = "recipe"
    context.user_data["diet_sel_id"] = recipe_id
    if _quick_mode(context, uid):
        handled = await _try_quick_default(
            update, context, query, "recipe", recipe_id
        )
        if handled is not None:
            return handled

    await _remove_callback_markup(query)
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
            revision=context.user_data.get("diet_ui_revision", 0),
        ),
    )
    return PORTION_CHOICE if prompt is not None else ConversationHandler.END


@authorized_callback
async def start_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The 'Search catalog' button: prompt for a query and enter the SEARCH state."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return FOOD_CHOICE
    await _remove_callback_markup(query)
    context.user_data.pop("diet_ui_message_id", None)
    try:
        await reply_html(
            query.message,
            "🔎 Type a food to search the catalog (e.g. <code>banana</code>):",
        )
    except TelegramError:
        logger.warning("Could not deliver search prompt", exc_info=True)
        finish_conversation(update, context, "diet")
        return ConversationHandler.END
    return SEARCH


async def receive_search_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Search the shared catalog and the user's own foods, then show results.

    A user's own foods/recipes rank before catalog hits (they are first-class,
    higher-priority sources).
    """
    text = update.message.text.strip()
    if not text or len(text) > _MAX_SEARCH_QUERY:
        await update.message.reply_text(
            "Enter a short food name to search, or /cancel."
        )
        return SEARCH
    db = context.bot_data["db"]
    uid = update.effective_user.id
    key = text.casefold()
    results: list[dict] = []
    for food in await db.list_foods(uid):
        if key in str(food["name_key"]):
            results.append(
                {"source_type": "food", "id": food["id"], "name": food["name"]}
            )
    for recipe in await db.list_recipes(uid):
        if key in str(recipe["name_key"]):
            results.append(
                {"source_type": "recipe", "id": recipe["id"], "name": recipe["name"]}
            )
    try:
        for hit in await db.search_catalog(text):
            results.append(
                {"source_type": "catalog", "id": hit["id"], "name": hit["name"]}
            )
    except NutritionError:
        pass

    if not results:
        # A dead end with no buttons used to leave the only ways out invisible:
        # remember an exact command or abandon the draft. Offer the three real
        # exits instead, and stay in SEARCH so typing another word still works.
        prompt = await _send_tap_keyboard(
            update,
            context,
            update.effective_message,
            f"🔎 No matches for “{escape_html(text[:_MAX_SEARCH_QUERY])}”. "
            "Type another word, or pick one of these:",
            search_empty_keyboard(uid),
        )
        return SEARCH if prompt is not None else ConversationHandler.END

    # One batched preference read decorates the results, so a search row shows the
    # same honest ⚡/amount as the picker; the tap behaves identically either way.
    decorated = suggestions.annotate_defaults(
        results, await db.get_food_preferences(uid)
    )
    prompt = await _send_tap_keyboard(
        update,
        context,
        update.effective_message,
        f"🔎 Results for “{escape_html(text)}” — pick one:",
        food_choice_keyboard(uid, decorated, quick=_quick_mode(context, uid)),
    )
    return FOOD_CHOICE if prompt is not None else ConversationHandler.END


@authorized_callback
async def choose_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A catalog food was tapped: offer its portions or a custom amount."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return FOOD_CHOICE
    catalog_id = int(_DCATALOG_RE.fullmatch(query.data).group(2))
    db = context.bot_data["db"]
    uid = update.effective_user.id
    catalog_food = await db.get_catalog_food(catalog_id)
    if catalog_food is None:
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)

    await _remove_callback_markup(query)
    context.user_data["diet_sel_kind"] = "catalog"
    context.user_data["diet_sel_id"] = catalog_id
    portions = await db.get_catalog_portions(catalog_id)
    recent = await db.get_recent_item_quantities(uid, "catalog", catalog_id)
    context.user_data["diet_recent_qtys"] = recent
    # Preferences are shown for a catalog food as of v16. The row is shared, the
    # preference is not: rice stays one row that both users resolve from, and
    # each stores their own usual amount against it. Before v16 this was
    # ``show_prefs=False`` — not by design, but because the preference table's
    # CHECK could not hold a catalog row at all.
    pref = await db.get_food_preference(uid, "catalog", catalog_id) or {}
    if portions or recent:
        prompt = await _send_tap_keyboard(
            update,
            context,
            query.message,
            # 🥫 (a food from the shared catalog), not 🔎 — the magnifier reads
            # as "you are searching", and by this point the pick is made. Matches
            # the re-render path.
            f"🥫 <b>{escape_html(catalog_food['name'])}</b> — how much?",
            food_portion_keyboard(
                uid,
                portions,
                recent,
                is_pinned=bool(pref.get("is_pinned")),
                hidden=bool(pref.get("hidden")),
                revision=context.user_data.get("diet_ui_revision", 0),
            ),
        )
        return PORTION_CHOICE if prompt is not None else ConversationHandler.END
    return await _prompt_custom_amount_text(
        update,
        context,
        query.message,
        catalog_food["name"],
        catalog_food["base_unit"],
    )


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
async def search_back_to_choices(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Leave a zero-result search and return to the saved-item picker.

    Separate from :func:`back_to_food_choice` only so a stale tap resolves to
    ``SEARCH`` — the state the user is actually in — instead of the quantity
    screen's state.
    """
    query = await _consume_diet_tap(update, context)
    if query is None:
        return SEARCH
    await _remove_callback_markup(query)
    return await _reprompt_food_choice(update, context, query.message)


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
    prompt = await _send_tap_keyboard(
        update,
        context,
        message,
        "Pick a saved item, 🔎 search, or ✍️ type it:",
        food_choice_keyboard(
            uid,
            choices,
            manage=phase1_enabled_for(uid),
            revision=context.user_data.get("diet_ui_revision", 0),
            page=int(context.user_data.get("diet_choice_page", 0) or 0),
            paginate=phase1_enabled_for(uid),
            change_meal=phase1_enabled_for(uid),
            quick=_quick_mode(context, uid),
        ),
    )
    return FOOD_CHOICE if prompt is not None else ConversationHandler.END


@authorized_callback
async def choose_portion(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """A named portion was tapped: resolve nutrition and preview it."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return PORTION_CHOICE
    portion_id = int(_DPORT_RE.fullmatch(query.data).group(2))
    db = context.bot_data["db"]
    uid = update.effective_user.id
    kind = context.user_data.get("diet_sel_kind")
    sel_id = context.user_data.get("diet_sel_id")
    if sel_id is None:
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)
    if kind == "catalog":
        portions = await db.get_catalog_portions(sel_id)
    else:
        portions = await db.get_food_portions(uid, sel_id)
    portion = next((p for p in portions if p["id"] == portion_id), None)
    if portion is None:
        await query.answer("That portion is no longer available.", show_alert=True)
        return PORTION_CHOICE
    try:
        entry = await _resolve_selected(
            db, uid, kind, sel_id, ["1", portion["name"]]
        )
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
    if kind == "catalog" and sel_id is not None:
        catalog_food = await db.get_catalog_food(sel_id)
        if catalog_food is None:
            return await _reprompt_food_choice(update, context, query.message)
        return await _prompt_custom_amount_text(
            update,
            context,
            query.message,
            catalog_food["name"],
            catalog_food["base_unit"],
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
    if kind == "catalog" and sel_id is not None:
        catalog_food = await db.get_catalog_food(sel_id)
        if catalog_food is None:
            raise NutritionError("That catalog food is no longer available.")
        portions = await db.get_catalog_portions(sel_id)
        return resolve_catalog_food_entry(catalog_food, portions, tokens)
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
    if kind not in PREFERENCE_SOURCE_TYPES or sel_id is None:
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
    if kind not in PREFERENCE_SOURCE_TYPES or sel_id is None:
        return PORTION_CHOICE
    pref = await db.get_food_preference(uid, kind, sel_id) or {}
    recent = context.user_data.get("diet_recent_qtys") or []
    revision = context.user_data.get("diet_ui_revision", 0)
    if kind in ("food", "catalog"):
        # Same keyboard either way — a catalog food's portions resolve through
        # the identical code path, which is why widening the preference table
        # was the only thing standing between a shared staple and a ⚡ row.
        portions = (
            await db.get_catalog_portions(sel_id)
            if kind == "catalog"
            else await db.get_food_portions(uid, sel_id)
        )
        keyboard = food_portion_keyboard(
            uid,
            portions,
            recent,
            is_pinned=bool(pref.get("is_pinned")),
            hidden=bool(pref.get("hidden")),
            revision=revision,
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
            revision=revision,
        )
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except TelegramError:
        logger.debug("Could not redraw quantity keyboard", exc_info=True)
    return PORTION_CHOICE


async def _apply_pref_desired_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    field: str,
    on_label: str,
    off_label: str,
) -> int:
    """Shared body for the pin/hide desired-state writes (plan §7.5).

    Validates owner, current UI message, and revision, applies one atomic
    setter, then increments the server revision before re-rendering. Fails
    closed on malformed/stale payloads and never toggles: the desired state is
    embedded in the callback so repeated delivery converges.
    """
    query = update.callback_query
    parts = (query.data or "").split("_")
    try:
        owner_id = parse_base36(parts[1])
        ui_revision = parse_base36(parts[2])
        desired = parts[3] == "1"
    except (IndexError, ValueError):
        await query.answer("This menu has expired.", show_alert=True)
        await _remove_callback_markup(query)
        return PORTION_CHOICE

    if owner_id != update.effective_user.id:
        await query.answer("This menu belongs to another user.", show_alert=True)
        return PORTION_CHOICE

    expected_rev = context.user_data.get("diet_ui_revision")
    expected_msg = context.user_data.get("diet_ui_message_id")
    actual_msg = getattr(query.message, "message_id", None)
    if (
        expected_rev is None
        or ui_revision != expected_rev
        or expected_msg is None
        or actual_msg != expected_msg
    ):
        await query.answer("This menu has expired.", show_alert=True)
        await _remove_callback_markup(query)
        return PORTION_CHOICE

    kind = context.user_data.get("diet_sel_kind")
    sel_id = context.user_data.get("diet_sel_id")
    if kind not in PREFERENCE_SOURCE_TYPES or sel_id is None:
        await query.answer()
        return await _reprompt_food_choice(update, context, query.message)

    db = context.bot_data["db"]
    uid = update.effective_user.id
    try:
        await db.set_food_preference(uid, kind, sel_id, **{field: desired})
    except ValueError:
        # A true pin/hide needs an active, owner-scoped source; if it was
        # archived/removed there is nothing to pin. Leave the revision as-is.
        await query.answer(
            "That food or recipe is no longer available.", show_alert=True
        )
        return await _rerender_quantity_screen(update, context, query)

    context.user_data["diet_ui_revision"] = ui_revision + 1
    await query.answer(on_label if desired else off_label)
    return await _rerender_quantity_screen(update, context, query)


@authorized_callback
async def set_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Explicit desired-state write for pinning a food/recipe."""
    return await _apply_pref_desired_state(
        update,
        context,
        field="is_pinned",
        on_label="📌 Pinned",
        off_label="Unpinned",
    )


@authorized_callback
async def set_hide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Explicit desired-state write for hiding a food/recipe."""
    return await _apply_pref_desired_state(
        update,
        context,
        field="hidden",
        on_label="🙈 Hidden from suggestions",
        off_label="👁 Shown again",
    )


# ---------------------------------------------------------------------------
# Phase 1b — Quick mode and private default quantities (plan §9.3/§9.4)
# ---------------------------------------------------------------------------
def _quick_mode(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Whether this draft is a Phase 1 Quick log rather than the Builder.

    Quick mode is only ever set by a Phase 1 entry point, and the flag is
    re-read here so a mid-flow rollback degrades to the Builder rather than
    continuing to offer fast mutations.
    """
    return (
        context.user_data.get("diet_entry_mode") == DietEntryMode.QUICK
        and phase1_enabled_for(user_id)
    )


def _bump_revision(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Advance the UI revision, invalidating every callback issued before now."""
    revision = int(context.user_data.get("diet_ui_revision", 0)) + 1
    context.user_data["diet_ui_revision"] = revision
    return revision


async def _consume_phase1_tap(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    owner_index: int,
    revision_index: int | None,
) -> object | None:
    """Answer and validate a base-36 tap, or ``None`` to ignore it.

    Checks owner and the message we last sent — the two things that make a tap
    "the live keyboard, pressed by its owner" — plus the revision for families
    that carry one. ``revision_index=None`` is for compact payloads (paging)
    whose slot is taken by another token; the tracked message id alone retires
    their older keyboards. Anything else is answered as expired and its markup
    retired, so a queued tap from a superseded screen cannot act on newer state.

    Base-36 only: a Phase 1 payload must never reach the legacy decimal parser,
    which would read the same characters as a different number (plan §9.2).
    """
    query = update.callback_query
    parts = (query.data or "").split("_")
    try:
        owner_id = parse_base36(parts[owner_index])
        revision = (
            None if revision_index is None else parse_base36(parts[revision_index])
        )
    except (IndexError, ValueError):
        await query.answer("This menu has expired.", show_alert=True)
        await _remove_callback_markup(query)
        return None
    if owner_id != update.effective_user.id:
        await query.answer("This menu belongs to another user.", show_alert=True)
        return None
    expected_rev = context.user_data.get("diet_ui_revision")
    expected_msg = context.user_data.get("diet_ui_message_id")
    actual_msg = getattr(query.message, "message_id", None)
    stale_revision = revision is not None and (
        expected_rev is None or revision != expected_rev
    )
    if stale_revision or expected_msg is None or actual_msg != expected_msg:
        await query.answer("This menu has expired.", show_alert=True)
        await _remove_callback_markup(query)
        return None
    await query.answer()
    return query


def _quantity_text(amount: object, unit: object) -> str:
    """Render a stored amount/unit pair for a message."""
    return f"{_format_grams(float(amount))} {escape_html(unit)}"


async def _finish_with_receipt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: object,
    receipt: object,
    headline: str,
) -> int:
    """End the Diet flow and leave the durable receipt behind."""
    finish_conversation(update, context, "diet")
    try:
        await send_meal_receipt(message, receipt, headline)
    except TelegramError:
        logger.warning("Could not deliver quick-log receipt", exc_info=True)
    return ConversationHandler.END


async def _commit_quick_meal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query: object,
    kind: str,
    source_id: int,
    *,
    quantity,
    set_as_default: bool,
    retry_state: int,
) -> int:
    """Log one single-item meal and route every outcome to the right screen.

    Deliberately does not retire the source keyboard first: if the write fails,
    the same tap must still be available to retry. A commit is the point of no
    return, and only then is the keyboard replaced by a receipt.
    """
    db = context.bot_data["db"]
    uid = update.effective_user.id
    meal_type = context.user_data.get("diet_meal_type")
    if not isinstance(meal_type, str):
        meal_type = infer_meal_type(now_local().time())
    if not phase1_enabled_for(uid):
        # Flag-off mid-flow: never mutate through a Phase 1 path.
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)

    try:
        result = await db.create_quick_meal(
            uid,
            meal_type,
            kind,
            source_id,
            quantity=quantity,
            set_as_default=set_as_default,
            source=mutation_source(update),
        )
    except NutritionError as exc:
        await query.answer(str(exc)[:190], show_alert=True)
        return retry_state
    except Exception:
        logger.warning("Quick meal write failed; keeping controls", exc_info=True)
        await query.answer("Couldn't log that — try again.", show_alert=True)
        return retry_state

    status = result.status
    if status in (QuickMealStatus.CREATED, QuickMealStatus.REPLAYED):
        await _remove_callback_markup(query)
        headline = (
            "✅ <b>Logged</b>"
            if status is QuickMealStatus.CREATED
            else "✅ <b>Already logged</b>"
        )
        if set_as_default and status is QuickMealStatus.CREATED:
            headline = "✅ <b>Logged</b> · ⭐ saved as your usual"
        return await _finish_with_receipt(
            update, context, query.message, result.receipt, headline
        )
    if status is QuickMealStatus.REPLAYED_REMOVED:
        await _remove_callback_markup(query)
        finish_conversation(update, context, "diet")
        try:
            await query.message.reply_text(
                "↩️ That entry was already undone; nothing changed."
            )
        except TelegramError:
            logger.warning("Could not report undone quick log", exc_info=True)
        return ConversationHandler.END
    if status is QuickMealStatus.SOURCE_UNAVAILABLE:
        await query.answer("That item is no longer available.", show_alert=True)
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)
    if status is QuickMealStatus.DEFAULT_INVALID:
        await query.answer("Your saved amount no longer works.", show_alert=True)
        await _remove_callback_markup(query)
        return await _open_default_menu(
            update, context, query.message, kind, source_id
        )
    if status is QuickMealStatus.QUANTITY_INVALID:
        await query.answer("That amount no longer works.", show_alert=True)
        await _remove_callback_markup(query)
        return await _rerender_portion_choice(update, context, query.message)
    # QUANTITY_REQUIRED: the default vanished between the read and the write.
    await _remove_callback_markup(query)
    return await _rerender_portion_choice(update, context, query.message)


async def _try_quick_default(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query: object,
    kind: str,
    source_id: int,
) -> int | None:
    """One-tap log when this source has a usable "usual" amount.

    Returns the next state when the tap was fully handled, or ``None`` to fall
    through to the ordinary quantity screen. A half-stored default is treated as
    a repair prompt rather than an absent one, so a broken preference surfaces
    instead of silently doing nothing.
    """
    db = context.bot_data["db"]
    uid = update.effective_user.id
    if await db.has_partial_default(uid, kind, source_id):
        await _remove_callback_markup(query)
        return await _open_default_menu(
            update, context, query.message, kind, source_id
        )
    if await db.get_default_quantity(uid, kind, source_id) is None:
        return None
    # Pass no quantity: the repository re-reads the stored pair inside its own
    # transaction, which is both fresher and what makes a stale default report
    # as DEFAULT_INVALID (repair it) rather than as a bad amount the user typed.
    return await _commit_quick_meal(
        update,
        context,
        query,
        kind,
        source_id,
        quantity=None,
        set_as_default=False,
        retry_state=FOOD_CHOICE,
    )


async def _show_quick_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: object,
    entry: object,
) -> int:
    """Confirm one resolved Quick item, with the option to make it the usual."""
    kind = context.user_data.get("diet_sel_kind")
    source_id = context.user_data.get("diet_sel_id")
    item = entry.as_item()
    context.user_data["diet_quick_pending_item"] = {
        "source_type": kind,
        "source_id": source_id,
        "entered_amount": item["entered_amount"],
        "entered_unit": item["entered_unit"],
        "display_name": item["display_name"],
    }
    revision = _bump_revision(context)
    meal_type = context.user_data.get("diet_meal_type", "")
    cal = item.get("calories")
    cal_text = f"{cal} kcal" if cal is not None else "kcal ?"
    text = (
        f"👀 <b>{escape_html(str(meal_type).title())}</b>\n"
        f"{escape_html(item['display_name'])} — {cal_text}"
    )
    prompt = await _send_tap_keyboard(
        update,
        context,
        message,
        text,
        quick_confirm_keyboard(
            update.effective_user.id,
            revision,
            can_set_default=(
                kind in PREFERENCE_SOURCE_TYPES
                and item["entered_amount"] is not None
            ),
        ),
    )
    return QUICK_CONFIRM if prompt is not None else ConversationHandler.END


def _pending_quantity(context: ContextTypes.DEFAULT_TYPE):
    """The confirmed Quick item as a (kind, id, DefaultQuantity) triple."""
    pending = context.user_data.get("diet_quick_pending_item")
    if not isinstance(pending, dict):
        return None
    kind = pending.get("source_type")
    source_id = pending.get("source_id")
    amount = pending.get("entered_amount")
    unit = pending.get("entered_unit")
    if kind not in PREFERENCE_SOURCE_TYPES or source_id is None:
        return None
    if amount is None or unit is None:
        return None
    return kind, int(source_id), DefaultQuantity(amount=float(amount), unit=str(unit))


async def _quick_commit_pending(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, set_as_default: bool
) -> int:
    """Shared body of the Quick ``Log`` and ``Log + set as usual`` buttons."""
    query = await _consume_phase1_tap(
        update, context, owner_index=2, revision_index=3
    )
    if query is None:
        return QUICK_CONFIRM
    pending = _pending_quantity(context)
    if pending is None:
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)
    kind, source_id, quantity = pending
    if set_as_default and kind == "catalog":
        # Catalog rows carry no private preference; log without one.
        set_as_default = False
    return await _commit_quick_meal(
        update,
        context,
        query,
        kind,
        source_id,
        quantity=quantity,
        set_as_default=set_as_default,
        retry_state=QUICK_CONFIRM,
    )


@authorized_callback
async def quick_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """``Log it`` on the Quick confirmation."""
    return await _quick_commit_pending(update, context, set_as_default=False)


@authorized_callback
async def quick_log_and_default(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """``Log + set as my usual``: one atomic meal-plus-preference write."""
    return await _quick_commit_pending(update, context, set_as_default=True)


@authorized_callback
async def quick_change_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Discard the pending Quick item and reopen its quantity screen."""
    query = await _consume_phase1_tap(
        update, context, owner_index=2, revision_index=3
    )
    if query is None:
        return QUICK_CONFIRM
    context.user_data.pop("diet_quick_pending_item", None)
    await _remove_callback_markup(query)
    return await _rerender_portion_choice(update, context, query.message)


@authorized_callback
async def quick_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Abandon the Quick item without logging anything."""
    query = await _consume_phase1_tap(
        update, context, owner_index=2, revision_index=3
    )
    if query is None:
        return QUICK_CONFIRM
    await _remove_callback_markup(query)
    finish_conversation(update, context, "diet")
    try:
        await query.message.reply_text("✖️ Cancelled — nothing was logged.")
    except TelegramError:
        logger.warning("Could not deliver quick cancellation", exc_info=True)
    return ConversationHandler.END


# --- Default management ----------------------------------------------------
async def _open_default_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: object,
    kind: str,
    source_id: int,
) -> int:
    """Show one source's "usual" amount and what can be done about it."""
    db = context.bot_data["db"]
    uid = update.effective_user.id
    name = await _source_name(db, uid, kind, source_id)
    if name is None:
        return await _reprompt_food_choice(update, context, message)

    context.user_data["diet_default_source_type"] = kind
    context.user_data["diet_default_source_id"] = source_id
    context.user_data.pop("diet_pending_default", None)
    default = await db.get_default_quantity(uid, kind, source_id)
    needs_repair = await db.has_partial_default(uid, kind, source_id)
    if default is not None:
        # A stored pair can still stop resolving (a portion was renamed, a
        # recipe's yield unit changed); say so rather than logging it later.
        try:
            await db.resolve_quantity(
                uid,
                kind,
                source_id,
                [format_decimal(default.amount), default.unit],
            )
        except (NutritionError, LookupError):
            needs_repair = True

    if needs_repair:
        body = "⚠️ Your saved amount no longer works — repair or remove it."
    elif default is not None:
        body = f"⭐ Your usual: <b>{_quantity_text(default.amount, default.unit)}</b>"
    else:
        body = "No usual amount saved yet."
    revision = _bump_revision(context)
    prompt = await _send_tap_keyboard(
        update,
        context,
        message,
        f"⚙️ <b>{escape_html(name)}</b>\n{body}",
        default_menu_keyboard(
            uid,
            revision,
            has_default=default is not None,
            needs_repair=needs_repair,
        ),
    )
    return DEFAULT_MENU if prompt is not None else ConversationHandler.END


async def _source_name(db, uid: int, kind: str, source_id: int) -> str | None:
    """Display name of one active, owner-scoped source, or ``None``."""
    if kind == "food":
        row = await db.get_food_by_id(uid, source_id)
    elif kind == "recipe":
        row = await db.get_recipe_by_id(uid, source_id)
    else:
        return None
    return str(row["name"]) if row is not None else None


def _default_target(context: ContextTypes.DEFAULT_TYPE):
    """The (kind, id) the default screens are acting on, if still coherent."""
    kind = context.user_data.get("diet_default_source_type")
    source_id = context.user_data.get("diet_default_source_id")
    if kind not in PREFERENCE_SOURCE_TYPES or source_id is None:
        return None
    return kind, int(source_id)


@authorized_callback
async def manage_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The ⚙️ button beside a saved item: open its default menu, log nothing."""
    query = await _consume_phase1_tap(
        update, context, owner_index=1, revision_index=2
    )
    if query is None:
        return FOOD_CHOICE
    parts = (query.data or "").split("_")
    try:
        kind = {"f": "food", "r": "recipe", "c": "catalog"}[parts[3]]
        source_id = parse_base36(parts[4])
    except (IndexError, KeyError, ValueError):
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)
    await _remove_callback_markup(query)
    return await _open_default_menu(update, context, query.message, kind, source_id)


@authorized_callback
async def default_use_other(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Leave management mode and pick an amount, without touching the default."""
    query = await _consume_phase1_tap(
        update, context, owner_index=2, revision_index=3
    )
    if query is None:
        return DEFAULT_MENU
    target = _default_target(context)
    if target is None:
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)
    kind, source_id = target
    await _remove_callback_markup(query)
    context.user_data["diet_sel_kind"] = kind
    context.user_data["diet_sel_id"] = source_id
    db = context.bot_data["db"]
    context.user_data["diet_recent_qtys"] = await db.get_recent_item_quantities(
        update.effective_user.id, kind, source_id
    )
    return await _rerender_portion_choice(update, context, query.message)


@authorized_callback
async def default_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask for the amount that should become (or repair) the usual."""
    query = await _consume_phase1_tap(
        update, context, owner_index=2, revision_index=3
    )
    if query is None:
        return DEFAULT_MENU
    target = _default_target(context)
    if target is None:
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)
    kind, source_id = target
    db = context.bot_data["db"]
    name = await _source_name(db, update.effective_user.id, kind, source_id)
    if name is None:
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)
    await _remove_callback_markup(query)
    context.user_data.pop("diet_ui_message_id", None)
    try:
        await reply_html(
            query.message,
            f"⭐ How much <b>{escape_html(name)}</b> is your usual? "
            "e.g. <code>2 bowl</code> or <code>150 g</code>.",
        )
    except TelegramError:
        logger.warning("Could not deliver default-amount prompt", exc_info=True)
        finish_conversation(update, context, "diet")
        return ConversationHandler.END
    return DEFAULT_AMOUNT


@authorized_callback
async def default_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Remove the stored usual, including one that no longer resolves."""
    query = await _consume_phase1_tap(
        update, context, owner_index=2, revision_index=3
    )
    if query is None:
        return DEFAULT_MENU
    target = _default_target(context)
    if target is None:
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)
    kind, source_id = target
    db = context.bot_data["db"]
    await db.clear_default_quantity(update.effective_user.id, kind, source_id)
    await _remove_callback_markup(query)
    return await _open_default_menu(update, context, query.message, kind, source_id)


@authorized_callback
async def default_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return to the picker with the draft untouched."""
    query = await _consume_phase1_tap(
        update, context, owner_index=2, revision_index=3
    )
    if query is None:
        return DEFAULT_MENU
    await _remove_callback_markup(query)
    context.user_data.pop("diet_default_source_type", None)
    context.user_data.pop("diet_default_source_id", None)
    context.user_data.pop("diet_pending_default", None)
    return await _reprompt_food_choice(update, context, query.message)


async def receive_default_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Resolve a typed default against the live source, then ask to confirm."""
    target = _default_target(context)
    if target is None:
        finish_conversation(update, context, "diet")
        await update.message.reply_text(
            "⚠️ Lost track of that item. Start again with /diet."
        )
        return ConversationHandler.END
    kind, source_id = target
    db = context.bot_data["db"]
    uid = update.effective_user.id
    tokens = update.message.text.split()
    try:
        entry = await db.resolve_quantity(uid, kind, source_id, tokens)
    except LookupError:
        finish_conversation(update, context, "diet")
        await update.message.reply_text(
            "⚠️ That item is no longer available. Start again with /diet."
        )
        return ConversationHandler.END
    except NutritionError as exc:
        await update.message.reply_text(
            f"❌ {exc}\nTry again, e.g. 2 bowl or 150 g."
        )
        return DEFAULT_AMOUNT

    item = entry.as_item()
    context.user_data["diet_pending_default"] = {
        "entered_amount": item["entered_amount"],
        "entered_unit": item["entered_unit"],
    }
    revision = _bump_revision(context)
    cal = item.get("calories")
    cal_text = f" — {cal} kcal" if cal is not None else ""
    prompt = await _send_tap_keyboard(
        update,
        context,
        update.effective_message,
        f"⭐ Save <b>{_quantity_text(item['entered_amount'], item['entered_unit'])}"
        f"</b> as your usual?{escape_html(cal_text)}",
        default_confirm_keyboard(uid, revision),
    )
    return DEFAULT_CONFIRM if prompt is not None else ConversationHandler.END


@authorized_callback
async def default_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the confirmed pair. Never a toggle: it writes an explicit value."""
    query = await _consume_phase1_tap(
        update, context, owner_index=2, revision_index=3
    )
    if query is None:
        return DEFAULT_CONFIRM
    target = _default_target(context)
    pending = context.user_data.get("diet_pending_default")
    if target is None or not isinstance(pending, dict):
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)
    kind, source_id = target
    db = context.bot_data["db"]
    try:
        await db.set_default_quantity(
            update.effective_user.id,
            kind,
            source_id,
            DefaultQuantity(
                amount=float(pending["entered_amount"]),
                unit=str(pending["entered_unit"]),
            ),
        )
    except (NutritionError, ValueError) as exc:
        await query.answer(str(exc)[:190], show_alert=True)
        await _remove_callback_markup(query)
        return await _open_default_menu(
            update, context, query.message, kind, source_id
        )
    context.user_data.pop("diet_pending_default", None)
    await _remove_callback_markup(query)
    return await _open_default_menu(update, context, query.message, kind, source_id)


@authorized_callback
async def default_reenter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Discard only the pending pair and ask again."""
    query = await _consume_phase1_tap(
        update, context, owner_index=2, revision_index=3
    )
    if query is None:
        return DEFAULT_CONFIRM
    context.user_data.pop("diet_pending_default", None)
    await _remove_callback_markup(query)
    context.user_data.pop("diet_ui_message_id", None)
    try:
        await query.message.reply_text("✍️ Enter the amount again, e.g. 2 bowl.")
    except TelegramError:
        logger.warning("Could not deliver default re-entry prompt", exc_info=True)
        finish_conversation(update, context, "diet")
        return ConversationHandler.END
    return DEFAULT_AMOUNT


@authorized_callback
async def default_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Drop the pending pair and go back to the menu, changing nothing."""
    query = await _consume_phase1_tap(
        update, context, owner_index=2, revision_index=3
    )
    if query is None:
        return DEFAULT_CONFIRM
    target = _default_target(context)
    context.user_data.pop("diet_pending_default", None)
    await _remove_callback_markup(query)
    if target is None:
        return await _reprompt_food_choice(update, context, query.message)
    return await _open_default_menu(update, context, query.message, *target)


# ---------------------------------------------------------------------------
# Phase 1b — "Log again at today's values" (plan §10.4/§10.5)
# ---------------------------------------------------------------------------
_ISSUE_TEXT = {
    CurrentValueIssueCode.SOURCE_MISSING: "that food/recipe is gone",
    CurrentValueIssueCode.QUANTITY_MISSING: "no amount was recorded",
    CurrentValueIssueCode.QUANTITY_INVALID: "the old amount no longer works",
    CurrentValueIssueCode.RECIPE_EMPTY: "that recipe has no ingredients",
}


def _nutrient_delta_line(preview) -> str:
    """The old → new calorie/macro line, or a note that it is unknown."""
    old, new, delta = (
        preview.original_totals,
        preview.proposed_totals,
        preview.delta,
    )
    if new.calories is None or old.calories is None:
        return "🔥 Total: some values unknown"
    sign = "+" if delta.calories and delta.calories > 0 else ""
    change = f" ({sign}{delta.calories})" if delta.calories else " (no change)"
    return f"🔥 Total: {old.calories} → <b>{new.calories}</b> kcal{change}"


def _current_values_text(preview) -> str:
    """Render each item's old → new value and what still needs a decision."""
    lines = [
        f"🔄 <b>{escape_html(preview.meal_type.title())}</b> at today's values:"
    ]
    for position, proposal in enumerate(preview.items, 1):
        name = escape_html(proposal.original.display_name)
        if proposal.decision is CurrentValueDecision.UNRESOLVED:
            reason = _ISSUE_TEXT.get(
                proposal.issue.code if proposal.issue else None, "needs a decision"
            )
            lines.append(f"{position}. ⚠️ <b>{name}</b> — {reason}")
        elif proposal.decision is CurrentValueDecision.REMOVE:
            lines.append(f"{position}. 🗑 <s>{name}</s> — dropped")
        elif proposal.decision is CurrentValueDecision.KEEP_ORIGINAL:
            lines.append(f"{position}. ↩️ {name} — kept as originally logged")
        else:
            old_cal = proposal.original.calories
            new_cal = proposal.proposed.calories if proposal.proposed else None
            if old_cal is not None and new_cal is not None and old_cal != new_cal:
                lines.append(f"{position}. {name} — {old_cal} → <b>{new_cal}</b> kcal")
            else:
                cal = "kcal ?" if new_cal is None else f"{new_cal} kcal"
                lines.append(f"{position}. {name} — {cal}")
    lines.append(_nutrient_delta_line(preview))
    if not preview.can_save:
        lines.append("Pick Keep or Drop for each ⚠️ item to continue.")
    return "\n".join(lines)


def _current_decisions(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """The child-id → decision map for the review in progress."""
    decisions = context.user_data.get("diet_current_decisions")
    if not isinstance(decisions, dict):
        decisions = {}
        context.user_data["diet_current_decisions"] = decisions
    return decisions


async def _render_current_values(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: object,
    preview,
) -> int:
    """Store the server-created preview and draw its review screen."""
    context.user_data["diet_current_source_meal_id"] = preview.source_meal_id
    context.user_data["diet_current_digest"] = preview.digest
    context.user_data["diet_current_child_ids"] = [
        p.source_child_id for p in preview.items
    ]
    revision = _bump_revision(context)
    prompt = await _send_tap_keyboard(
        update,
        context,
        message,
        _current_values_text(preview),
        current_values_keyboard(
            update.effective_user.id,
            revision,
            preview.items,
            can_save=preview.can_save,
        ),
    )
    return CURRENT_VALUES_REVIEW if prompt is not None else ConversationHandler.END


@authorized_callback
async def current_values_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """``Log again at today's values`` on a receipt: open the review screen.

    Distinct from Repeat on purpose. Repeat copies the old numbers exactly; this
    re-prices the same items from the sources as they are now and shows the
    difference before anything is written.
    """
    query = update.callback_query
    parts = (query.data or "").split("_")
    try:
        owner = parse_base36(parts[2])
        source_meal_id = parse_base36(parts[3])
    except (IndexError, ValueError):
        await query.answer("This action isn't available.", show_alert=True)
        return ConversationHandler.END
    if owner != update.effective_user.id:
        await query.answer("This action belongs to another user.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    if not phase1_enabled_for(owner):
        try:
            await update.effective_message.reply_text(
                "Use /diet to log a meal.", reply_markup=reply_keyboard_remove()
            )
        except TelegramError:
            logger.warning("Could not deliver diet compatibility guidance", exc_info=True)
        return ConversationHandler.END
    if not await conversation_available(update, context, "diet"):
        return ConversationHandler.END

    db = context.bot_data["db"]
    preview = await db.get_current_value_preview(owner, source_meal_id)
    if preview is None:
        try:
            await query.message.reply_text("That meal is no longer available.")
        except TelegramError:
            logger.debug("Could not report missing source meal", exc_info=True)
        return ConversationHandler.END

    await db.ensure_user(
        owner, update.effective_user.username, update.effective_user.first_name
    )
    activate_conversation(update, context, "diet")
    context.user_data["diet_entry_mode"] = DietEntryMode.QUICK
    context.user_data["diet_meal_type"] = preview.meal_type
    context.user_data["diet_ui_revision"] = 0
    context.user_data["diet_current_decisions"] = {}
    return await _render_current_values(update, context, query.message, preview)


async def _apply_current_decision(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    decision: CurrentValueDecision,
) -> int:
    """Record one Keep/Drop repair choice and redraw the review."""
    query = await _consume_phase1_tap(
        update, context, owner_index=2, revision_index=3
    )
    if query is None:
        return CURRENT_VALUES_REVIEW
    parts = (query.data or "").split("_")
    try:
        child_id = parse_base36(parts[4])
    except (IndexError, ValueError):
        return CURRENT_VALUES_REVIEW
    # Identity is the child row id from the snapshot we rendered — item_order is
    # display order and is not unique, so it can never be the key here.
    known = context.user_data.get("diet_current_child_ids") or []
    meal_id = context.user_data.get("diet_current_source_meal_id")
    if child_id not in known or meal_id is None:
        await query.answer("That item is no longer part of this meal.")
        return CURRENT_VALUES_REVIEW

    _current_decisions(context)[child_id] = decision
    db = context.bot_data["db"]
    preview = await db.get_current_value_preview(
        update.effective_user.id, meal_id, _current_decisions(context)
    )
    await _remove_callback_markup(query)
    if preview is None:
        finish_conversation(update, context, "diet")
        try:
            await query.message.reply_text("That meal is no longer available.")
        except TelegramError:
            logger.debug("Could not report missing source meal", exc_info=True)
        return ConversationHandler.END
    return await _render_current_values(update, context, query.message, preview)


@authorized_callback
async def current_values_keep(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Keep one item exactly as it was originally logged."""
    return await _apply_current_decision(
        update, context, CurrentValueDecision.KEEP_ORIGINAL
    )


@authorized_callback
async def current_values_remove(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Drop one item from the re-logged meal."""
    return await _apply_current_decision(
        update, context, CurrentValueDecision.REMOVE
    )


@authorized_callback
async def current_values_save(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Commit the re-priced meal, re-checking that it still matches the preview."""
    query = await _consume_phase1_tap(
        update, context, owner_index=2, revision_index=3
    )
    if query is None:
        return CURRENT_VALUES_REVIEW
    uid = update.effective_user.id
    meal_id = context.user_data.get("diet_current_source_meal_id")
    digest = context.user_data.get("diet_current_digest")
    if meal_id is None or digest is None:
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)
    if not phase1_enabled_for(uid):
        await _remove_callback_markup(query)
        return await _reprompt_food_choice(update, context, query.message)

    db = context.bot_data["db"]
    try:
        result = await db.commit_current_value_meal(
            uid,
            meal_id,
            _current_decisions(context),
            digest,
            source=mutation_source(update),
        )
    except NutritionError as exc:
        await query.answer(str(exc)[:190], show_alert=True)
        return CURRENT_VALUES_REVIEW
    except Exception:
        logger.warning("Current-value save failed; keeping controls", exc_info=True)
        await query.answer("Couldn't log that — try again.", show_alert=True)
        return CURRENT_VALUES_REVIEW

    status = result.status
    if status is CurrentCommitStatus.REVIEW_REQUIRED:
        # The sources moved under us (or an issue is still open): show what it
        # would be *now* and make the user confirm that instead.
        await query.answer("Values changed — take another look.", show_alert=True)
        await _remove_callback_markup(query)
        return await _render_current_values(
            update, context, query.message, result.preview
        )
    if status is CurrentCommitStatus.SOURCE_MEAL_REMOVED:
        await _remove_callback_markup(query)
        finish_conversation(update, context, "diet")
        try:
            await query.message.reply_text("That meal is no longer available.")
        except TelegramError:
            logger.debug("Could not report missing source meal", exc_info=True)
        return ConversationHandler.END
    if status is CurrentCommitStatus.REPLAYED_REMOVED:
        await _remove_callback_markup(query)
        finish_conversation(update, context, "diet")
        try:
            await query.message.reply_text(
                "↩️ That entry was already undone; nothing changed."
            )
        except TelegramError:
            logger.debug("Could not report undone replay", exc_info=True)
        return ConversationHandler.END

    await _remove_callback_markup(query)
    headline = (
        "🔄 <b>Logged at today's values</b>"
        if status is CurrentCommitStatus.CREATED
        else "🔄 <b>Already logged</b>"
    )
    return await _finish_with_receipt(
        update, context, query.message, result.receipt, headline
    )


@authorized_callback
async def current_values_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Abandon the review; the original meal is untouched either way."""
    query = await _consume_phase1_tap(
        update, context, owner_index=2, revision_index=3
    )
    if query is None:
        return CURRENT_VALUES_REVIEW
    await _remove_callback_markup(query)
    finish_conversation(update, context, "diet")
    try:
        await query.message.reply_text("✖️ Cancelled — nothing was logged.")
    except TelegramError:
        logger.warning("Could not deliver current-values cancellation", exc_info=True)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Phase 1b — picker pagination, meal-type change, draft editing (plan §9.5-§9.7)
# ---------------------------------------------------------------------------
@authorized_callback
async def change_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Move to another page of suggestions.

    The payload carries a page, not a revision, so this validates owner plus the
    current picker message; the new message id is what invalidates a queued tap
    from the page being left.
    """
    query = await _consume_phase1_tap(
        update, context, owner_index=1, revision_index=None
    )
    if query is None:
        return FOOD_CHOICE
    try:
        page = parse_base36((query.data or "").split("_")[2])
    except (IndexError, ValueError):
        return FOOD_CHOICE
    context.user_data["diet_choice_page"] = page
    await _remove_callback_markup(query)
    meal_type = context.user_data.get("diet_meal_type", "")
    return await _prompt_food_choice(update, context, query.message, meal_type)


@authorized_callback
async def change_meal_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Reopen the meal-type picker without disturbing the draft (plan §9.5)."""
    query = await _consume_phase1_tap(
        update, context, owner_index=1, revision_index=None
    )
    if query is None:
        return FOOD_CHOICE

    await _remove_callback_markup(query)
    try:
        prompt = await reply_html(
            query.message,
            "🍽️ Which meal is this?",
            reply_markup=meal_type_keyboard(update.effective_user.id),
        )
    except TelegramError:
        logger.warning("Could not deliver meal-type keyboard", exc_info=True)
        finish_conversation(update, context, "diet")
        return ConversationHandler.END
    context.user_data["diet_meal_message_id"] = prompt.message_id
    return MEAL_TYPE


def _draft_index(context: ContextTypes.DEFAULT_TYPE, data: str) -> int | None:
    """The validated draft position a ``d(qty|edit|remove)`` payload names."""
    items = context.user_data.get("diet_items")
    if not isinstance(items, list):
        return None
    try:
        index = parse_base36((data or "").split("_")[3])
    except (IndexError, ValueError):
        return None
    return index if 0 <= index < len(items) else None


async def _rerender_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: object,
    *,
    bump: bool = True,
) -> int:
    """Redraw the meal preview from the current draft.

    ``bump=False`` is for a read-only redraw (the ``Meal`` control): nothing about
    the draft changed, so the existing callbacks stay meaningful — the new
    message id alone retires the previous keyboard (plan §9.2).
    """
    items = context.user_data.get("diet_items")
    meal_type = context.user_data.get("diet_meal_type", "")
    if not isinstance(items, list) or not items:
        return await _reprompt_food_choice(update, context, message)
    revision = (
        _bump_revision(context)
        if bump
        else int(context.user_data.get("diet_ui_revision", 0))
    )
    prompt = await _send_tap_keyboard(
        update,
        context,
        message,
        _meal_preview(meal_type, items),
        diet_save_keyboard(
            update.effective_user.id,
            phase1_enabled=phase1_enabled_for(update.effective_user.id),
            revision=revision,
            items=items,
        ),
    )
    return CONFIRM_ITEM if prompt is not None else ConversationHandler.END


@authorized_callback
async def draft_change_quantity(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Re-enter the amount for one drafted item, keeping the old one until it resolves."""
    query = await _consume_phase1_tap(
        update, context, owner_index=1, revision_index=2
    )
    if query is None:
        return CONFIRM_ITEM
    index = _draft_index(context, query.data or "")
    if index is None:
        await _remove_callback_markup(query)
        return await _rerender_draft(update, context, query.message)
    item = context.user_data["diet_items"][index]
    source_type = str(item.get("source_type", "freetext"))
    if source_type == "freetext" or item.get("source_id") is None:
        await query.answer("Type-it items have no amount to change.")
        return CONFIRM_ITEM

    # The item stays in the draft until a new amount successfully resolves.
    context.user_data["diet_edit_index"] = index
    context.user_data["diet_sel_kind"] = source_type
    context.user_data["diet_sel_id"] = item["source_id"]
    await _remove_callback_markup(query)
    db = context.bot_data["db"]
    uid = update.effective_user.id
    context.user_data["diet_recent_qtys"] = await db.get_recent_item_quantities(
        uid, source_type, item["source_id"]
    )
    return await _rerender_portion_choice(update, context, query.message)


@authorized_callback
async def draft_replace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pick a different source for one drafted item."""
    query = await _consume_phase1_tap(
        update, context, owner_index=1, revision_index=2
    )
    if query is None:
        return CONFIRM_ITEM
    index = _draft_index(context, query.data or "")
    if index is None:
        await _remove_callback_markup(query)
        return await _rerender_draft(update, context, query.message)
    context.user_data["diet_edit_index"] = index
    await _remove_callback_markup(query)
    return await _reprompt_food_choice(update, context, query.message)


@authorized_callback
async def draft_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Delete exactly one drafted item; an emptied draft reopens the picker."""
    query = await _consume_phase1_tap(
        update, context, owner_index=1, revision_index=2
    )
    if query is None:
        return CONFIRM_ITEM
    index = _draft_index(context, query.data or "")
    if index is None:
        await _remove_callback_markup(query)
        return await _rerender_draft(update, context, query.message)
    items = context.user_data["diet_items"]
    items.pop(index)
    await _remove_callback_markup(query)
    if not items:
        context.user_data.pop("diet_edit_index", None)
        return await _reprompt_food_choice(update, context, query.message)
    return await _rerender_draft(update, context, query.message)


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
    """Append the resolved item to the meal draft and preview the whole meal.

    In Quick mode the first resolved item is not a draft at all — it goes
    straight to a one-tap confirmation, which is the point of the mode. Once a
    draft exists (the user chose to add more), the Builder preview takes over.
    """
    edit_index = context.user_data.pop("diet_edit_index", None)
    if (
        edit_index is None
        and _quick_mode(context, update.effective_user.id)
        and not context.user_data.get("diet_items")
    ):
        return await _show_quick_confirm(update, context, message, entry)

    items: list[dict] = context.user_data.setdefault("diet_items", [])
    if isinstance(edit_index, int) and 0 <= edit_index < len(items):
        # Atomic replacement: the old item was kept until this one resolved.
        items[edit_index] = entry.as_item()
    else:
        items.append(entry.as_item())
    return await _rerender_draft(update, context, message)


@authorized_callback
async def add_another_item(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Keep the meal draft and return to the saved-item list for another item."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return CONFIRM_ITEM
    await _remove_callback_markup(query)
    context.user_data.pop("diet_edit_index", None)
    return await _reprompt_food_choice(update, context, query.message)


@authorized_callback
async def add_another_item_p1(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Revisioned ``Add another item`` (Phase 1 Builder keyboard)."""
    query = await _consume_phase1_tap(
        update, context, owner_index=1, revision_index=2
    )
    if query is None:
        return CONFIRM_ITEM
    await _remove_callback_markup(query)
    context.user_data.pop("diet_edit_index", None)
    return await _reprompt_food_choice(update, context, query.message)


@authorized_callback
async def save_item_p1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Revisioned ``Save meal`` (Phase 1 Builder keyboard)."""
    query = await _consume_phase1_tap(
        update, context, owner_index=1, revision_index=2
    )
    if query is None:
        return CONFIRM_ITEM
    return await _persist_draft(update, context, query)


@authorized_callback
async def save_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Persist the whole meal (header + items), then offer to log another meal."""
    query = await _consume_diet_tap(update, context)
    if query is None:
        return CONFIRM_ITEM
    return await _persist_draft(update, context, query)


async def _persist_draft(
    update: Update, context: ContextTypes.DEFAULT_TYPE, query: object
) -> int:
    """Write the assembled draft; on failure keep it intact for a retry."""
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

    db = context.bot_data["db"]
    # Write first, then retire the keyboard, so a transient write failure leaves
    # the draft and its Save/Add/Cancel controls intact for a retry instead of
    # stranding the conversation. A successful commit is the point of no return.
    try:
        meal_id = await db.log_diet_with_items(
            update.effective_user.id,
            meal_type,
            items,
            source=mutation_source(update),
        )
    except NutritionError as exc:
        # The assembled meal violates a bound (e.g. aggregate calories/macros).
        # Keep the draft and re-render the preview so the user can fix it.
        await _remove_callback_markup(query)
        try:
            await reply_html(query.message, f"⚠️ {escape_html(str(exc))}")
        except TelegramError:
            logger.debug("Could not report meal bound error", exc_info=True)
        return await _rerender_draft(update, context, query.message)
    except Exception:
        # Transient failure (e.g. database write error). Re-render the preview so
        # the Save/Add/Cancel controls remain available for a retry.
        logger.warning("Diet meal save failed; offering retry", exc_info=True)
        await _remove_callback_markup(query)
        try:
            await reply_html(
                query.message, "⚠️ Couldn't save that meal — try again."
            )
        except TelegramError:
            logger.debug("Could not report meal save failure", exc_info=True)
        return await _rerender_draft(update, context, query.message)

    await _remove_callback_markup(query)
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
    return await _offer_log_another(
        update, context, query.message, meal_id=meal_id, items=items
    )


async def _offer_log_another(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: object,
    *,
    meal_id: int | None = None,
    items: Sequence[Mapping[str, object]] = (),
) -> int:
    """After a save, keep the flow open with a one-tap 'Log another' / 'Done'.

    Clears the finished meal's data but leaves the conversation active; the
    normal 300s idle timeout closes it when the user stops logging.

    A typed item of the meal just saved also gets a 💾 row here, because this is
    the one place where its name and its complete nutrition are both already
    known — see :mod:`bot.handlers.keep_food`. Building the offers must not be
    able to cost the user their keyboard, so a failed read degrades to the plain
    two-button version rather than propagating.
    """
    uid = update.effective_user.id
    keepable: list[tuple[int, str]] = []
    if meal_id is not None and items:
        try:
            keepable = await keep_food_offers(context.bot_data["db"], uid, items)
        except Exception:
            logger.warning("Could not build keep-food offers", exc_info=True)
    _clear_diet_entry_data(context)
    prompt = await _send_tap_keyboard(
        update,
        context,
        message,
        "➕ Log another meal?",
        log_another_keyboard(
            uid,
            meal_id=meal_id if keepable else None,
            keepable=keepable,
        ),
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
) -> int | None:
    """Cancel button on the tap keyboards (owner- and current-UI-checked).

    Requires the tap to land on the message we last sent, so a stale Cancel from
    an older keyboard cannot end a newer meal draft. When the tap is stale or
    belongs to another user, this returns ``None`` to leave the active flow's
    state untouched (this handler is a fallback, so ``None`` keeps the current
    state). ``/cancel`` remains the forgiving, always-available escape route.
    """
    query = await _consume_diet_tap(update, context)
    if query is None:
        return None
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


# All revisioned base-36 Phase 1b callback families (plan §9.2). Release A
# installs one inert global stale handler for the whole set: it answers, retires
# the markup best-effort, and performs no DB read/write. Release B registers the
# real state handlers before this fallback.
_DIET_PHASE1_CALLBACK_RE = re.compile(
    r"^(?:"
    r"dpage_[0-9a-z]+_[0-9a-z]+|dchangemeal_[0-9a-z]+|"
    r"dmanage_[0-9a-z]+_[0-9a-z]+_[frc]_[0-9a-z]+|"
    r"d(?:pin|hide)_[0-9a-z]+_[0-9a-z]+_[01]|"
    r"dq_(?:log|default|amount|cancel)_[0-9a-z]+_[0-9a-z]+|"
    r"dd_(?:use|edit|clear|back|save|reenter|cancel)_[0-9a-z]+_[0-9a-z]+|"
    r"d(?:add|save)_[0-9a-z]+_[0-9a-z]+|"
    r"d(?:qty|edit|remove)_[0-9a-z]+_[0-9a-z]+_[0-9a-z]+|"
    r"cv_(?:keep|remove)_[0-9a-z]+_[0-9a-z]+_[0-9a-z]+|"
    r"cv_(?:save|cancel)_[0-9a-z]+_[0-9a-z]+"
    r")$"
)
# Durable receipt controls (plan §10.1). Owner token is base-36; some carry a
# meal id. Release A retires them inertly and synchronizes keyboard removal.
_RECEIPT_CALLBACK_RE = re.compile(
    r"^mr_(?:undo|current)_[0-9a-z]+_[0-9a-z]+$|^mr_more_[0-9a-z]+$"
)


def _owned_base36(data: str, group_index: int) -> int | None:
    """Decode the owner token from a base-36 callback, or ``None`` if malformed."""
    parts = (data or "").split("_")
    if len(parts) <= group_index:
        return None
    try:
        return parse_base36(parts[group_index])
    except ValueError:
        return None


@authorized_callback
async def stale_phase1_diet_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Inertly retire any Phase 1b base-36 diet callback with no active flow."""
    query = update.callback_query
    owner = _owned_base36(query.data or "", 1)
    # dmanage/dpin/dhide/dq/dd/dqty/dedit/dremove/cv/dpage/dchangemeal all place
    # the owner immediately after the family token.
    if owner is not None and owner != update.effective_user.id:
        await query.answer("This menu belongs to another user.", show_alert=True)
        return
    await query.answer("This menu has expired.", show_alert=True)
    await _remove_callback_markup(query)


@authorized_callback
async def stale_receipt_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Inertly retire a receipt control that no live handler claimed.

    Reached by ``Use current values`` (not yet implemented) and by ``Log another``
    while a Diet flow is active — its entry point cannot fire then. Either way
    this answers and, unless a flow is simply in the way, retires the inline
    markup and synchronizes keyboard removal, with no DB read/write
    (plan §8.2 / §10.1).
    """
    query = update.callback_query
    owner = _owned_base36(query.data or "", 2)
    if owner is not None and owner != update.effective_user.id:
        await query.answer("This action belongs to another user.", show_alert=True)
        return
    if active_conversation_flow(context) is not None:
        # The receipt is still good; the user just has something else open. Keep
        # its buttons so the action works once that flow finishes.
        await query.answer(
            "Finish the flow you're in (or /cancel) first.", show_alert=True
        )
        return
    await query.answer("This action isn't available.", show_alert=True)
    await _remove_callback_markup(query)
    try:
        await query.message.reply_text(
            "That quick action isn't available.",
            reply_markup=reply_keyboard_remove(),
        )
    except TelegramError:
        logger.debug("Could not synchronize keyboard on stale receipt", exc_info=True)


# ---------------------------------------------------------------------------
# Phase 1a Home entry (the "Meal" reply label) + per-state routing guards
# ---------------------------------------------------------------------------
async def diet_home_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The ``Meal`` reply label: infer the meal and open Quick ``FOOD_CHOICE``.

    The active-flow guard runs before any flag check or DB call, so an active
    Study/Gym/Habit/Diet flow keeps ownership of the update. The Phase 1 flag is
    then checked before ``ensure_user`` or any other DB call; when disabled the
    handler sends compatibility guidance plus keyboard removal and ends without
    touching the ledger or even the user/settings bootstrap rows (plan §8.2).
    """
    if not await conversation_available(update, context, "diet"):
        return ConversationHandler.END

    user = update.effective_user
    if not phase1_enabled_for(user.id):
        try:
            await update.effective_message.reply_text(
                "Use /diet to log a meal.",
                reply_markup=reply_keyboard_remove(),
            )
        except TelegramError:
            logger.warning("Could not deliver diet compatibility guidance", exc_info=True)
        return ConversationHandler.END

    return await _start_quick_meal(update, context)


async def _start_quick_meal(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Open a fresh Quick meal draft at ``FOOD_CHOICE`` for the acting user.

    The caller owns the active-flow and Phase 1 checks; this is only the draft
    setup shared by the ``Meal`` label and a receipt's ``Log another``.
    """
    user = update.effective_user
    db = context.bot_data["db"]
    await db.ensure_user(user.id, user.username, user.first_name)
    activate_conversation(update, context, "diet")
    context.user_data["diet_entry_mode"] = DietEntryMode.QUICK
    meal_type = infer_meal_type(now_local().time())
    context.user_data["diet_meal_type"] = meal_type
    context.user_data["diet_choice_page"] = 0
    context.user_data["diet_ui_revision"] = 0
    return await _prompt_food_choice(
        update, context, update.effective_message, meal_type
    )


@authorized_callback
async def diet_receipt_more_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """``Log another`` on a meal receipt: start a new Quick meal (plan §10.1).

    A receipt outlives its conversation, so this validates the embedded owner and
    re-checks the Phase 1 flag before any DB call. Unlike ``Undo``, it needs an
    idle user: it opens a flow, so another active flow gets the standard hint and
    keeps ownership.
    """
    query = update.callback_query
    owner = _owned_base36(query.data or "", 2)
    if owner is None or owner != update.effective_user.id:
        await query.answer("This action belongs to another user.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    if not phase1_enabled_for(owner):
        try:
            await update.effective_message.reply_text(
                "Use /diet to log a meal.",
                reply_markup=reply_keyboard_remove(),
            )
        except TelegramError:
            logger.warning("Could not deliver diet compatibility guidance", exc_info=True)
        return ConversationHandler.END
    if not await conversation_available(update, context, "diet"):
        return ConversationHandler.END
    return await _start_quick_meal(update, context)


async def diet_timeout_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Report a timed-out Diet flow, naming anything that was discarded.

    The generic handler said only "send the command again", which left a user
    guessing whether an assembled draft had been saved. Nothing here was ever
    written to the ledger — the Builder's Save is the only write — so the honest
    message is to list what is being thrown away.
    """
    items = context.user_data.get("diet_items")
    pending = context.user_data.get("diet_quick_pending_item")
    lost: list[str] = []
    if isinstance(items, list):
        lost.extend(str(item.get("display_name", "?")) for item in items)
    elif isinstance(pending, dict):
        lost.append(str(pending.get("display_name", "?")))

    finish_conversation(update, context, "diet")
    # finish_conversation only clears keys when the active-flow marker is intact.
    # This message promises the draft is gone, so make that unconditionally true
    # rather than leaving a stale draft behind for the next entry to inherit.
    _clear_diet_entry_data(context)
    message = getattr(update, "effective_message", None)
    if message is None:
        return ConversationHandler.END

    if lost:
        shown = ", ".join(lost[:5])
        if len(lost) > 5:
            shown += f", and {len(lost) - 5} more"
        text = (
            "⏰ <b>Meal draft discarded</b> after 15 min idle — nothing was "
            f"logged.\nYou had: {escape_html(shown)}\n"
            "Tap 🍽️ <b>Meal</b> or /diet to start again."
        )
    else:
        text = (
            "⏰ Timed out — nothing was logged. Tap 🍽️ <b>Meal</b> or /diet "
            "to start again."
        )
    try:
        await reply_html(message, text)
    except TelegramError:
        logger.warning("Could not deliver diet timeout notice", exc_info=True)
    return ConversationHandler.END


async def _diet_draft_expired(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Fail closed when a re-render finds required state data missing."""
    finish_conversation(update, context, "diet")
    try:
        await update.effective_message.reply_text(
            "⚠️ That meal draft expired. Start again with /diet."
        )
    except TelegramError:
        logger.warning("Could not report expired diet draft", exc_info=True)
    return ConversationHandler.END


async def _rerender_portion_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message: object
) -> int:
    """Redraw the quantity keyboard for the currently selected source."""
    uid = update.effective_user.id
    db = context.bot_data["db"]
    kind = context.user_data.get("diet_sel_kind")
    sel_id = context.user_data.get("diet_sel_id")
    recent = context.user_data.get("diet_recent_qtys") or []
    revision = context.user_data.get("diet_ui_revision", 0)
    if kind == "food" and sel_id is not None:
        food = await db.get_food_by_id(uid, sel_id)
        if food is None:
            return await _diet_draft_expired(update, context)
        portions = await db.get_food_portions(uid, sel_id)
        pref = await db.get_food_preference(uid, "food", sel_id) or {}
        prompt = await _send_tap_keyboard(
            update,
            context,
            message,
            f"🥗 <b>{escape_html(food['name'])}</b> — how much?",
            food_portion_keyboard(
                uid,
                portions,
                recent,
                is_pinned=bool(pref.get("is_pinned")),
                hidden=bool(pref.get("hidden")),
                revision=revision,
            ),
        )
        return PORTION_CHOICE if prompt is not None else ConversationHandler.END
    if kind == "recipe" and sel_id is not None:
        recipe = await db.get_recipe_by_id(uid, sel_id)
        if recipe is None:
            return await _diet_draft_expired(update, context)
        pref = await db.get_food_preference(uid, "recipe", sel_id) or {}
        prompt = await _send_tap_keyboard(
            update,
            context,
            message,
            f"🍲 <b>{escape_html(recipe['name'])}</b> — how much?",
            recipe_quantity_keyboard(
                uid,
                recipe["yield_unit"],
                recent,
                is_pinned=bool(pref.get("is_pinned")),
                hidden=bool(pref.get("hidden")),
                revision=revision,
            ),
        )
        return PORTION_CHOICE if prompt is not None else ConversationHandler.END
    if kind == "catalog" and sel_id is not None:
        catalog_food = await db.get_catalog_food(sel_id)
        if catalog_food is None:
            return await _diet_draft_expired(update, context)
        portions = await db.get_catalog_portions(sel_id)
        prompt = await _send_tap_keyboard(
            update,
            context,
            message,
            f"🥫 <b>{escape_html(catalog_food['name'])}</b> — how much?",
            food_portion_keyboard(uid, portions, recent, show_prefs=False),
        )
        return PORTION_CHOICE if prompt is not None else ConversationHandler.END
    return await _diet_draft_expired(update, context)


async def _rerender_diet_state(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: int
) -> int:
    """Re-render the current Diet step for the ``Meal`` control (plan §8.5).

    Rebuilds from authoritative ``user_data`` plus fresh read-only source lists,
    sends a new tracked message, and returns the same state — the draft,
    selection, revision, DB, and flags are untouched. Missing/invalid state data
    fails closed via :func:`_diet_draft_expired`.
    """
    message = update.effective_message
    uid = update.effective_user.id
    meal_type = context.user_data.get("diet_meal_type")

    if state == MEAL_TYPE:
        try:
            prompt = await reply_html(
                message,
                "🍽️ <b>Log Meal</b>\n\nWhich meal?",
                reply_markup=meal_type_keyboard(uid),
            )
        except TelegramError:
            return await _diet_draft_expired(update, context)
        context.user_data["diet_meal_message_id"] = prompt.message_id
        return MEAL_TYPE

    if state == FOOD_CHOICE:
        if not meal_type:
            return await _diet_draft_expired(update, context)
        return await _prompt_food_choice(update, context, message, meal_type)

    if state == PORTION_CHOICE:
        return await _rerender_portion_choice(update, context, message)

    if state == CONFIRM_ITEM:
        items = context.user_data.get("diet_items")
        if not isinstance(items, list) or not items or not meal_type:
            return await _diet_draft_expired(update, context)
        return await _rerender_draft(update, context, message, bump=False)

    if state == LOG_ANOTHER:
        prompt = await _send_tap_keyboard(
            update, context, message, "➕ Log another meal?",
            log_another_keyboard(uid),
        )
        return LOG_ANOTHER if prompt is not None else ConversationHandler.END

    if state == QUICK_CONFIRM:
        pending = context.user_data.get("diet_quick_pending_item")
        if not isinstance(pending, dict) or not meal_type:
            return await _diet_draft_expired(update, context)
        revision = _bump_revision(context)
        prompt = await _send_tap_keyboard(
            update,
            context,
            message,
            f"👀 <b>{escape_html(str(meal_type).title())}</b>\n"
            f"{escape_html(str(pending.get('display_name', '')))}",
            quick_confirm_keyboard(
                uid,
                revision,
                can_set_default=pending.get("source_type") in PREFERENCE_SOURCE_TYPES,
            ),
        )
        return QUICK_CONFIRM if prompt is not None else ConversationHandler.END

    if state in (DEFAULT_MENU, DEFAULT_CONFIRM):
        target = _default_target(context)
        if target is None:
            return await _diet_draft_expired(update, context)
        # A re-render of the confirmation step drops back to the menu on
        # purpose: the pending pair is not worth re-showing out of context.
        context.user_data.pop("diet_pending_default", None)
        return await _open_default_menu(update, context, message, *target)

    if state == CURRENT_VALUES_REVIEW:
        meal_id = context.user_data.get("diet_current_source_meal_id")
        if meal_id is None:
            return await _diet_draft_expired(update, context)
        db = context.bot_data["db"]
        preview = await db.get_current_value_preview(
            uid, meal_id, _current_decisions(context)
        )
        if preview is None:
            return await _diet_draft_expired(update, context)
        return await _render_current_values(update, context, message, preview)

    if state == DEFAULT_AMOUNT:
        target = _default_target(context)
        db = context.bot_data["db"]
        name = (
            await _source_name(db, uid, *target) if target is not None else None
        )
        if name is None:
            return await _diet_draft_expired(update, context)
        context.user_data.pop("diet_ui_message_id", None)
        try:
            await reply_html(
                message,
                f"⭐ How much <b>{escape_html(name)}</b> is your usual? "
                "e.g. <code>2 bowl</code> or <code>150 g</code>.",
            )
        except TelegramError:
            return await _diet_draft_expired(update, context)
        return DEFAULT_AMOUNT

    if state == SEARCH:
        context.user_data.pop("diet_ui_message_id", None)
        try:
            await reply_html(
                message,
                "🔎 Type a food to search the catalog (e.g. <code>banana</code>):",
            )
        except TelegramError:
            return await _diet_draft_expired(update, context)
        return SEARCH

    if state == CUSTOM_AMOUNT:
        kind = context.user_data.get("diet_sel_kind")
        sel_id = context.user_data.get("diet_sel_id")
        db = context.bot_data["db"]
        if kind == "food" and sel_id is not None:
            food = await db.get_food_by_id(uid, sel_id)
            if food is None:
                return await _diet_draft_expired(update, context)
            return await _prompt_custom_amount_text(
                update, context, message, food["name"], food["base_unit"]
            )
        if kind == "recipe" and sel_id is not None:
            recipe = await db.get_recipe_by_id(uid, sel_id)
            if recipe is None:
                return await _diet_draft_expired(update, context)
            return await _prompt_custom_amount_text(
                update, context, message, recipe["name"], recipe["yield_unit"]
            )
        if kind == "catalog" and sel_id is not None:
            catalog_food = await db.get_catalog_food(sel_id)
            if catalog_food is None:
                return await _diet_draft_expired(update, context)
            return await _prompt_custom_amount_text(
                update, context, message, catalog_food["name"],
                catalog_food["base_unit"],
            )
        return await _diet_draft_expired(update, context)

    if state == FOOD_ITEMS:
        try:
            await reply_html(message, "🍽️ What did you eat?")
        except TelegramError:
            return await _diet_draft_expired(update, context)
        return FOOD_ITEMS

    if state == CALORIES:
        try:
            await reply_html(message, f"🔢 How many calories?\n{_REQUIRED_HINT}")
        except TelegramError:
            return await _diet_draft_expired(update, context)
        return CALORIES

    if state == MACROS:
        try:
            await reply_html(
                message,
                "⚖️ Protein, carbs, fat in grams? e.g. <code>30 80 15</code>\n"
                f"{_REQUIRED_HINT}",
            )
        except TelegramError:
            return await _diet_draft_expired(update, context)
        return MACROS

    return await _diet_draft_expired(update, context)


def _diet_meal_guard(state: int) -> MessageHandler:
    """A ``Meal``-label handler that re-renders exactly ``state`` in place."""

    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        return await _rerender_diet_state(update, context, state)

    return MessageHandler(MEAL_LABEL_FILTER, _handler)


# Shared, stateless per-state guards. Voice is rejected without a download;
# non-meal control words nudge; arbitrary text in a callback-only state is
# absorbed so it never falls through to the Home router.
_diet_voice_guard = MessageHandler(filters.VOICE, voice_mid_flow_interceptor)
_diet_control_guard = MessageHandler(
    DIET_NONMEAL_CONTROL_FILTER, active_flow_control_interceptor
)
_diet_text_catchall = MessageHandler(
    filters.TEXT & ~filters.COMMAND, buttons_or_cancel_catchall
)


# ---------------------------------------------------------------------------
# ConversationHandler
# ---------------------------------------------------------------------------
diet_conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("diet", diet_command, filters=AUTH_FILTER),
        CallbackQueryHandler(diet_menu_entry, pattern=r"^menu_diet$"),
        # The persistent-keyboard "Meal" label is a real Quick entry point, not a
        # global Home handler; it re-validates the flag/active-flow internally.
        MessageHandler(AUTH_FILTER & MEAL_LABEL_FILTER, diet_home_entry),
        # "Log another" on a durable receipt opens the next Quick meal. As an
        # entry point it is claimed here whenever no Diet flow is active; with
        # one active, the stale receipt handler answers with the flow hint.
        CallbackQueryHandler(
            diet_receipt_more_entry, pattern=RECEIPT_MORE_PATTERN
        ),
        # "Log again at today's values" also opens a flow (a review screen), so
        # it is an entry point for the same reason.
        CallbackQueryHandler(
            current_values_entry, pattern=RECEIPT_CURRENT_PATTERN
        ),
    ],
    states={
        # Every state leads with a voice guard (reject, no download), then the
        # "Meal" re-render, then the non-meal control nudge, then that state's
        # real handlers; callback-only states end with a text catchall so
        # arbitrary text never falls through to the Home router (plan §8.5/§8.6).
        MEAL_TYPE: [
            _diet_voice_guard,
            _diet_meal_guard(MEAL_TYPE),
            _diet_control_guard,
            CallbackQueryHandler(
                receive_meal_type,
                pattern=r"^meal_\d+_(breakfast|lunch|dinner|snack)$",
            ),
            _diet_text_catchall,
        ],
        FOOD_CHOICE: [
            _diet_voice_guard,
            _diet_meal_guard(FOOD_CHOICE),
            _diet_control_guard,
            CallbackQueryHandler(choose_food, pattern=r"^dfood_\d+_\d+$"),
            CallbackQueryHandler(choose_recipe, pattern=r"^drecipe_\d+_\d+$"),
            CallbackQueryHandler(choose_catalog, pattern=r"^dcatalog_\d+_\d+$"),
            CallbackQueryHandler(start_search, pattern=r"^dsearch_\d+$"),
            CallbackQueryHandler(type_food_instead, pattern=r"^dtype_\d+$"),
            CallbackQueryHandler(
                manage_default,
                pattern=r"^dmanage_[0-9a-z]+_[0-9a-z]+_[frc]_[0-9a-z]+$",
            ),
            CallbackQueryHandler(
                change_page, pattern=r"^dpage_[0-9a-z]+_[0-9a-z]+$"
            ),
            CallbackQueryHandler(
                change_meal_type, pattern=r"^dchangemeal_[0-9a-z]+$"
            ),
            _diet_text_catchall,
        ],
        SEARCH: [
            _diet_voice_guard,
            _diet_meal_guard(SEARCH),
            _diet_control_guard,
            # The zero-result escape hatches. Same callback families as the
            # picker, so they inherit its ownership/revision retirement rules.
            CallbackQueryHandler(search_back_to_choices, pattern=r"^dback_\d+$"),
            CallbackQueryHandler(start_search, pattern=r"^dsearch_\d+$"),
            CallbackQueryHandler(type_food_instead, pattern=r"^dtype_\d+$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_search_query),
        ],
        PORTION_CHOICE: [
            _diet_voice_guard,
            _diet_meal_guard(PORTION_CHOICE),
            _diet_control_guard,
            CallbackQueryHandler(choose_portion, pattern=r"^dport_\d+_\d+$"),
            CallbackQueryHandler(use_recent_quantity, pattern=r"^drecent_\d+_\d+$"),
            CallbackQueryHandler(recipe_quick_amount, pattern=r"^drq_\d+$"),
            CallbackQueryHandler(prompt_custom_amount, pattern=r"^dcustom_\d+$"),
            CallbackQueryHandler(back_to_food_choice, pattern=r"^dback_\d+$"),
            CallbackQueryHandler(set_pin, pattern=r"^dpin_[0-9a-z]+_[0-9a-z]+_[01]$"),
            CallbackQueryHandler(set_hide, pattern=r"^dhide_[0-9a-z]+_[0-9a-z]+_[01]$"),
            _diet_text_catchall,
        ],
        CUSTOM_AMOUNT: [
            _diet_voice_guard,
            _diet_meal_guard(CUSTOM_AMOUNT),
            _diet_control_guard,
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_amount),
        ],
        CONFIRM_ITEM: [
            _diet_voice_guard,
            _diet_meal_guard(CONFIRM_ITEM),
            _diet_control_guard,
            # Legacy decimal payloads stay live so a Release A rollback (or a
            # keyboard drawn before the flag flipped) can still save its draft.
            CallbackQueryHandler(add_another_item, pattern=r"^dadd_\d+$"),
            CallbackQueryHandler(save_item, pattern=r"^dsave_\d+$"),
            CallbackQueryHandler(
                add_another_item_p1, pattern=r"^dadd_[0-9a-z]+_[0-9a-z]+$"
            ),
            CallbackQueryHandler(
                save_item_p1, pattern=r"^dsave_[0-9a-z]+_[0-9a-z]+$"
            ),
            CallbackQueryHandler(
                draft_change_quantity,
                pattern=r"^dqty_[0-9a-z]+_[0-9a-z]+_[0-9a-z]+$",
            ),
            CallbackQueryHandler(
                draft_replace, pattern=r"^dedit_[0-9a-z]+_[0-9a-z]+_[0-9a-z]+$"
            ),
            CallbackQueryHandler(
                draft_remove, pattern=r"^dremove_[0-9a-z]+_[0-9a-z]+_[0-9a-z]+$"
            ),
            _diet_text_catchall,
        ],
        LOG_ANOTHER: [
            _diet_voice_guard,
            _diet_meal_guard(LOG_ANOTHER),
            _diet_control_guard,
            CallbackQueryHandler(log_another, pattern=r"^dmore_\d+_(yes|no)$"),
            _diet_text_catchall,
        ],
        # --- Phase 1b: Quick confirmation and default management ---
        QUICK_CONFIRM: [
            _diet_voice_guard,
            _diet_meal_guard(QUICK_CONFIRM),
            _diet_control_guard,
            CallbackQueryHandler(
                quick_log, pattern=r"^dq_log_[0-9a-z]+_[0-9a-z]+$"
            ),
            CallbackQueryHandler(
                quick_log_and_default, pattern=r"^dq_default_[0-9a-z]+_[0-9a-z]+$"
            ),
            CallbackQueryHandler(
                quick_change_amount, pattern=r"^dq_amount_[0-9a-z]+_[0-9a-z]+$"
            ),
            CallbackQueryHandler(
                quick_cancel, pattern=r"^dq_cancel_[0-9a-z]+_[0-9a-z]+$"
            ),
            _diet_text_catchall,
        ],
        DEFAULT_MENU: [
            _diet_voice_guard,
            _diet_meal_guard(DEFAULT_MENU),
            _diet_control_guard,
            CallbackQueryHandler(
                default_use_other, pattern=r"^dd_use_[0-9a-z]+_[0-9a-z]+$"
            ),
            CallbackQueryHandler(
                default_edit, pattern=r"^dd_edit_[0-9a-z]+_[0-9a-z]+$"
            ),
            CallbackQueryHandler(
                default_clear, pattern=r"^dd_clear_[0-9a-z]+_[0-9a-z]+$"
            ),
            CallbackQueryHandler(
                default_back, pattern=r"^dd_back_[0-9a-z]+_[0-9a-z]+$"
            ),
            _diet_text_catchall,
        ],
        DEFAULT_AMOUNT: [
            _diet_voice_guard,
            _diet_meal_guard(DEFAULT_AMOUNT),
            _diet_control_guard,
            MessageHandler(
                filters.TEXT & ~filters.COMMAND, receive_default_amount
            ),
        ],
        DEFAULT_CONFIRM: [
            _diet_voice_guard,
            _diet_meal_guard(DEFAULT_CONFIRM),
            _diet_control_guard,
            CallbackQueryHandler(
                default_save, pattern=r"^dd_save_[0-9a-z]+_[0-9a-z]+$"
            ),
            CallbackQueryHandler(
                default_reenter, pattern=r"^dd_reenter_[0-9a-z]+_[0-9a-z]+$"
            ),
            CallbackQueryHandler(
                default_cancel, pattern=r"^dd_cancel_[0-9a-z]+_[0-9a-z]+$"
            ),
            _diet_text_catchall,
        ],
        CURRENT_VALUES_REVIEW: [
            _diet_voice_guard,
            _diet_meal_guard(CURRENT_VALUES_REVIEW),
            _diet_control_guard,
            CallbackQueryHandler(
                current_values_keep,
                pattern=r"^cv_keep_[0-9a-z]+_[0-9a-z]+_[0-9a-z]+$",
            ),
            CallbackQueryHandler(
                current_values_remove,
                pattern=r"^cv_remove_[0-9a-z]+_[0-9a-z]+_[0-9a-z]+$",
            ),
            CallbackQueryHandler(
                current_values_save, pattern=r"^cv_save_[0-9a-z]+_[0-9a-z]+$"
            ),
            CallbackQueryHandler(
                current_values_cancel, pattern=r"^cv_cancel_[0-9a-z]+_[0-9a-z]+$"
            ),
            _diet_text_catchall,
        ],
        FOOD_ITEMS: [
            _diet_voice_guard,
            _diet_meal_guard(FOOD_ITEMS),
            _diet_control_guard,
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_food_items),
        ],
        CALORIES: [
            CommandHandler("skip", _skip_retired(CALORIES)),
            _diet_voice_guard,
            _diet_meal_guard(CALORIES),
            _diet_control_guard,
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_calories),
        ],
        MACROS: [
            CommandHandler("skip", skip_macros),
            _diet_voice_guard,
            _diet_meal_guard(MACROS),
            _diet_control_guard,
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_macros),
        ],
        ConversationHandler.TIMEOUT: [TypeHandler(Update, diet_timeout_handler)],
    },
    fallbacks=[
        cancel_handler,
        CallbackQueryHandler(cancel_diet_callback, pattern=r"^dcancel_\d+$"),
        CommandHandler("diet", active_conversation_hint, filters=AUTH_FILTER),
        # Home is always reachable: it ends this flow and reports anything
        # unsaved. Must be a fallback — a handler outside the conversation
        # cannot return END into it, so the state would linger and swallow
        # the next ordinary message.
        *home_fallback_handlers(),
    ],
    conversation_timeout=CONVERSATION_TIMEOUT,
    # per_message=False is correct: the code manually validates callback
    # ownership via ``diet_meal_message_id`` in user_data rather than
    # relying on PTB's per-message tracking.
    per_message=False,
)
