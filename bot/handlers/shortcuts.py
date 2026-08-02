"""``/shortcuts`` — say which items belong to which meal, before any history.

The picker was already meal-type aware: :mod:`bot.suggestions` weights
same-meal-type frequency three times general use, so what you eat at snack time
rises to the top of the snack list on its own. That learning is the right default
and needs no configuration — but it has to watch you repeat yourself first, and a
shared-catalog food can never be personalised at all.

This is the manual half. Mark Skyr as a snack once and it is the first button in
the snack picker from then on, whether or not it has ever been logged. It is
still only a *pointer*: no nutrition, no amount, nothing snapshotted. The item
itself stays the single source of those.

Deliberately its own small conversation rather than more buttons inside the diet
flow. The diet handler is the largest and most heavily tested module in the app,
and a shortcut is configuration — something done rarely, not mid-meal.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

from .home import home_fallback_handlers
from .common import (
    ACTIVE_CONTROL_FILTER,
    AUTH_FILTER,
    active_conversation_hint,
    activate_conversation,
    active_flow_control_interceptor,
    authorized_callback,
    buttons_or_cancel_catchall,
    cancel_handler,
    conversation_available,
    escape_html,
    finish_conversation,
    reply_html,
    timeout_handler,
    voice_mid_flow_interceptor,
)
from ..config import CONVERSATION_TIMEOUT
from ..database import MAX_MEAL_SHORTCUTS

logger = logging.getLogger(__name__)

MEAL, LIST, SEARCH = range(3)

MEALS = ("breakfast", "lunch", "dinner", "snack")
_MEAL_EMOJI = {
    "breakfast": "🌅",
    "lunch": "🥗",
    "dinner": "🍽️",
    "snack": "🍎",
}
#: One letter per source type, so a toggle fits Telegram's 64-byte callback cap
#: even for a long catalog id.
_SOURCE_CODE = {"food": "f", "recipe": "r", "catalog": "c"}
_CODE_SOURCE = {code: name for name, code in _SOURCE_CODE.items()}

_MAX_QUERY = 50
_MAX_ROWS = 12


def _tap(action: str, user_id: int, payload: str = "") -> str:
    return f"sc_{action}_{user_id}" + (f"_{payload}" if payload else "")


def _parse(query, user_id: int) -> tuple[str, str] | None:
    """Split a ``sc_*`` callback and confirm it belongs to the tapping user."""
    parts = (query.data or "").split("_", 3)
    if len(parts) < 3 or parts[0] != "sc":
        return None
    try:
        if int(parts[2]) != user_id:
            return None
    except ValueError:
        return None
    return parts[1], (parts[3] if len(parts) > 3 else "")


def _meal_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{_MEAL_EMOJI[meal]} {meal.title()}",
                callback_data=_tap("m", user_id, meal),
            )
            for meal in MEALS[index : index + 2]
        ]
        for index in (0, 2)
    ]
    rows.append([InlineKeyboardButton("✖️ Done", callback_data=_tap("x", user_id))])
    return InlineKeyboardMarkup(rows)


def _row_label(name: str, marked: bool) -> str:
    return f"{'⭐' if marked else '☆'} {name}"


def _list_keyboard(user_id: int, rows: list[dict], marked: set) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                _row_label(str(row["name"]), (row["source_type"], row["id"]) in marked),
                callback_data=_tap(
                    "t",
                    user_id,
                    f"{_SOURCE_CODE[row['source_type']]}{row['id']}",
                ),
            )
        ]
        for row in rows[:_MAX_ROWS]
    ]
    buttons.append(
        [InlineKeyboardButton("🔍 Search the catalog", callback_data=_tap("s", user_id))]
    )
    buttons.append(
        [InlineKeyboardButton("⬅️ Another meal", callback_data=_tap("b", user_id))]
    )
    return InlineKeyboardMarkup(buttons)


async def _candidate_rows(db, user_id: int, meal_type: str) -> list[dict]:
    """What to offer as togglable: current shortcuts first, then own items.

    A current shortcut is listed even when it points at the shared catalog, so
    the only way in is also a way out.
    """
    rows: list[dict] = []
    seen: set[tuple[str, int]] = set()

    for row in await db.get_shortcut_targets(user_id, meal_type):
        key = (row["source_type"], row["id"])
        seen.add(key)
        rows.append({"source_type": row["source_type"], "id": row["id"], "name": row["name"]})

    for source_type, items in (
        ("food", await db.list_foods(user_id)),
        ("recipe", await db.list_recipes(user_id)),
    ):
        for item in items:
            key = (source_type, item["id"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {"source_type": source_type, "id": item["id"], "name": item["name"]}
            )
    return rows


async def _render_list(message, context, user_id: int, *, prefix: str = "") -> int:
    db = context.bot_data["db"]
    meal_type = context.user_data.get("shortcut_meal", "snack")
    marked = await db.get_meal_shortcuts(user_id, meal_type)
    rows = await _candidate_rows(db, user_id, meal_type)

    header = (
        f"{prefix}⭐ <b>{meal_type.title()} shortcuts</b>\n\n"
        "Tap to add or remove. Starred items go to the top of the "
        f"{escape_html(meal_type)} picker."
    )
    if not rows:
        header += (
            "\n\n<i>You have no saved foods or recipes yet — search the catalog "
            "to pick one, or add your own with /food add.</i>"
        )
    await reply_html(message, header, reply_markup=_list_keyboard(user_id, rows, marked))
    return LIST


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
async def shortcuts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """``/shortcuts`` — pick a meal, then star what belongs to it."""
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)
    if not await conversation_available(update, context, "shortcuts"):
        return ConversationHandler.END

    activate_conversation(update, context, "shortcuts")
    try:
        await reply_html(
            update.effective_message,
            "⭐ <b>Meal shortcuts</b>\n\nWhich meal do you want to set up?",
            reply_markup=_meal_keyboard(user.id),
        )
    except BaseException:
        finish_conversation(update, context, "shortcuts")
        raise
    return MEAL


@authorized_callback
async def meal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A meal type was picked (or Done)."""
    query = update.callback_query
    parsed = _parse(query, update.effective_user.id)
    if parsed is None:
        await query.answer("That button is no longer valid.", show_alert=True)
        return MEAL
    action, payload = parsed
    await query.answer()
    await _retire(query)

    if action == "x":
        finish_conversation(update, context, "shortcuts")
        await reply_html(query.message, "⭐ Done.")
        return ConversationHandler.END

    context.user_data["shortcut_meal"] = payload if payload in MEALS else "snack"
    return await _render_list(query.message, context, update.effective_user.id)


@authorized_callback
async def list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Toggle a row, go back to the meal list, or start a catalog search."""
    query = update.callback_query
    parsed = _parse(query, update.effective_user.id)
    if parsed is None:
        await query.answer("That button is no longer valid.", show_alert=True)
        return LIST
    action, payload = parsed
    db = context.bot_data["db"]
    user_id = update.effective_user.id
    meal_type = context.user_data.get("shortcut_meal", "snack")

    if action == "b":
        await query.answer()
        await _retire(query)
        await reply_html(
            query.message,
            "⭐ <b>Meal shortcuts</b>\n\nWhich meal?",
            reply_markup=_meal_keyboard(user_id),
        )
        return MEAL

    if action == "s":
        await query.answer()
        await _retire(query)
        await reply_html(
            query.message,
            f"🔍 Type part of a food name to add it to <b>{escape_html(meal_type)}</b>.",
        )
        return SEARCH

    if action != "t" or len(payload) < 2:
        await query.answer("That button is no longer valid.", show_alert=True)
        return LIST

    source_type = _CODE_SOURCE.get(payload[0])
    try:
        source_id = int(payload[1:])
    except ValueError:
        source_type = None
    if source_type is None:
        await query.answer("That button is no longer valid.", show_alert=True)
        return LIST

    marked = await db.get_meal_shortcuts(user_id, meal_type)
    if (source_type, source_id) in marked:
        await db.remove_meal_shortcut(user_id, meal_type, source_type, source_id)
        note = "Removed."
    else:
        if len(marked) >= MAX_MEAL_SHORTCUTS:
            await query.answer(
                f"That's the {MAX_MEAL_SHORTCUTS}-shortcut limit for "
                f"{meal_type}. Remove one first.",
                show_alert=True,
            )
            return LIST
        await db.add_meal_shortcut(user_id, meal_type, source_type, source_id)
        note = "Added."
    await query.answer(note)
    await _retire(query)
    return await _render_list(query.message, context, user_id)


async def receive_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Search the shared catalog and offer the matches as shortcuts."""
    db = context.bot_data["db"]
    user_id = update.effective_user.id
    meal_type = context.user_data.get("shortcut_meal", "snack")
    query_text = (update.message.text or "").strip()[:_MAX_QUERY]
    if not query_text:
        await update.message.reply_text("❌ Type part of a food name.")
        return SEARCH

    try:
        matches = await db.search_catalog(query_text)
    except Exception:
        logger.warning("Catalog search failed in shortcuts", exc_info=False)
        matches = []

    if not matches:
        await reply_html(
            update.message,
            f"Nothing in the catalog matches <b>{escape_html(query_text)}</b>.\n"
            "Try a shorter word, or save it with <code>/food add</code> first.",
        )
        return SEARCH

    marked = await db.get_meal_shortcuts(user_id, meal_type)
    rows = [
        {"source_type": "catalog", "id": row["id"], "name": row["name"]}
        for row in matches
    ]
    await reply_html(
        update.message,
        f"🔍 Tap one to add it to <b>{escape_html(meal_type)}</b>:",
        reply_markup=_list_keyboard(user_id, rows, marked),
    )
    return LIST


async def _retire(query) -> None:
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        logger.debug("Could not retire a shortcuts keyboard", exc_info=True)


_voice_guard = MessageHandler(filters.VOICE, voice_mid_flow_interceptor)
_control_guard = MessageHandler(ACTIVE_CONTROL_FILTER, active_flow_control_interceptor)
_text_catchall = MessageHandler(
    filters.TEXT & ~filters.COMMAND, buttons_or_cancel_catchall
)
_PATTERN = r"^sc_[a-z]+_\d+(?:_.+)?$"

shortcuts_conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("shortcuts", shortcuts_command, filters=AUTH_FILTER),
    ],
    states={
        MEAL: [
            _voice_guard,
            CallbackQueryHandler(meal_callback, pattern=_PATTERN),
            _control_guard,
            _text_catchall,
        ],
        LIST: [
            _voice_guard,
            CallbackQueryHandler(list_callback, pattern=_PATTERN),
            _control_guard,
            _text_catchall,
        ],
        SEARCH: [
            _voice_guard,
            CallbackQueryHandler(list_callback, pattern=_PATTERN),
            _control_guard,
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_search),
        ],
        ConversationHandler.TIMEOUT: [TypeHandler(Update, timeout_handler)],
    },
    fallbacks=[
        cancel_handler,
        CommandHandler("shortcuts", active_conversation_hint, filters=AUTH_FILTER),
        *home_fallback_handlers(),
    ],
    conversation_timeout=CONVERSATION_TIMEOUT,
    per_message=False,
)
