"""/meals — one day's food, item by item, with what each thing cost.

The ledger could already answer *how much* (``/summary``: one line of totals)
and *did it save* (``/recent``: the last few entries across every section). It
could not answer the question you actually ask when you look back at a day:
what did I eat, and what did each thing cost me. Every number needed for that
has been stored per item since structured meals landed — this reads it back.

Read-only, so unlike the check-off screens it is not confined to today and
yesterday: ◀ ▶ walk any day you have logged. The layout and the date grammar
live in :mod:`bot.meal_day`, which knows nothing about Telegram.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from telegram import Message, Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from .. import meal_day
from ..config import local_date_from_utc, localize, today_local
from ..keyboards import MEAL_DAY_PREFIX, meal_day_keyboard, parse_meal_day
from .common import AUTH_FILTER, authorized_callback, escape_html, reply_html

logger = logging.getLogger(__name__)

#: Headroom under Telegram's 4096-character ceiling, as in :mod:`.recent`.
_MESSAGE_LIMIT = 4000

MEAL_DAY_PATTERN = rf"^{MEAL_DAY_PREFIX}_[0-9a-z]+_\d{{8}}$"


def _parse_utc(value: object) -> datetime | None:
    """Parse a stored timestamp (with or without microseconds) as naive UTC."""
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _local_time(value: object) -> str:
    parsed = _parse_utc(value)
    return localize(parsed).strftime("%H:%M") if parsed else "—"


async def _meals_on(db, user_id: int, day: date) -> list[dict]:
    """That local day's meals, chronologically.

    ``get_diet_logs`` widens its window by a day at each end because it filters
    on stored UTC timestamps, so the local-date test has to be redone here —
    the same shape ``/summary`` uses. Getting this wrong shifts a late-night
    meal into the wrong day, which is the one error a day view must not make.
    """
    rows = await db.get_diet_logs(user_id, day, day)
    meals = [
        dict(row)
        for row in rows
        if (parsed := _parse_utc(row["logged_at"])) is not None
        and local_date_from_utc(parsed) == day
    ]
    meals.sort(key=lambda row: meal_day.sort_key(row, _parse_utc))
    return meals


def _pack(blocks: list[str], limit: int) -> list[str]:
    """Group blocks into messages, never splitting one across a boundary."""
    messages: list[str] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        block_size = len(block.encode("utf-16-le", errors="surrogatepass")) // 2 + 2
        if current and size + block_size > limit:
            messages.append("\n\n".join(current))
            current, size = [block], block_size
        else:
            current.append(block)
            size += block_size
    if current:
        messages.append("\n\n".join(current))
    return messages or [""]


async def render_day(message: Message, context, user_id: int, day: date) -> None:
    """Send one day's breakdown, with the day-stepping keyboard on the last part."""
    db = context.bot_data["db"]
    meals = await _meals_on(db, user_id, day)
    items = await db.get_diet_items_for_meals(user_id, [meal["id"] for meal in meals])

    totals = meal_day.day_totals(meals)
    blocks = [meal_day.day_heading(day, totals, len(meals))]
    blocks.extend(
        meal_day.meal_block(
            meal,
            items.get(int(meal["id"]), []),
            local_time=_local_time(meal["logged_at"]),
            escape=escape_html,
        )
        for meal in meals
    )

    today = today_local()
    parts = _pack(blocks, _MESSAGE_LIMIT)
    for index, text in enumerate(parts):
        last = index == len(parts) - 1
        try:
            await reply_html(
                message,
                text,
                reply_markup=(
                    meal_day_keyboard(user_id, day, has_next=day < today)
                    if last
                    else None
                ),
            )
        except TelegramError:
            logger.warning("Could not deliver the day's meals", exc_info=True)
            return


async def meals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/meals``, ``/meals yesterday``, ``/meals 11 aug``, ``/meals 2026-08-11``."""
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)

    raw = " ".join(context.args or [])
    try:
        day = meal_day.parse_day(raw, today_local())
    except meal_day.DayInputError as exc:
        await reply_html(update.effective_message, f"❌ {exc}")
        return
    await render_day(update.effective_message, context, user.id, day)


@authorized_callback
async def meal_day_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """◀ ▶ on a day view, and the 🍽️ button on a summary."""
    query = update.callback_query
    user_id = update.effective_user.id
    day = parse_meal_day(query.data or "", user_id)
    if day is None:
        await query.answer(
            "That button belongs to another user or is no longer valid.",
            show_alert=True,
        )
        return
    if day > today_local():
        # Reachable only from a keyboard that outlived the day it was drawn on.
        await query.answer("That day hasn't happened yet.", show_alert=True)
        return

    await query.answer()
    await render_day(query.message, context, user_id, day)


meals_handler = CommandHandler("meals", meals_command, filters=AUTH_FILTER)
meal_day_handler = CallbackQueryHandler(meal_day_callback, pattern=MEAL_DAY_PATTERN)
