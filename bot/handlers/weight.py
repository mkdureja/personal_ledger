"""``/weight`` — one number, once a day.

Weight is the only thing in this ledger that is a *measurement* rather than a
record of something done. That shapes the whole flow:

* **A day holds one weight.** Weighing twice corrects the day rather than
  appending to it, so the chart never has to choose between two answers.
* **Missing a day is normal**, not an error to be nagged about. Nothing here
  asks where yesterday went; the gap is handled at read time by
  :mod:`bot.weight_series`.
* **Most days the number is close to the last one**, which is what makes it
  tappable at all. The nudge row is built from the last weigh-in — however long
  ago that was — and typing stays available for the days it does not cover.

A first-ever weigh-in gets no nudge row on purpose. There is nothing to nudge
from, and a grid centred on a guess would invite a tap that records a weight
nobody measured.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from .. import weight_series
from ..config import CONVERSATION_TIMEOUT, today_local
from ..database import MAX_WEIGHT_KG, MIN_WEIGHT_KG
from ..keyboards import parse_weight_tap, weight_entry_keyboard
from ..weight_series import format_change, format_kg
from .common import (
    ACTIVE_CONTROL_FILTER,
    AUTH_FILTER,
    active_conversation_hint,
    activate_conversation,
    active_flow_control_interceptor,
    authorized_callback,
    cancel_handler,
    conversation_available,
    escape_html,
    reply_html,
    finish_conversation,
    timeout_handler,
    voice_mid_flow_interceptor,
)
from .home import home_fallback_handlers

logger = logging.getLogger(__name__)

ASK = 0

#: How much history the receipt's trend line reads. Two averaging windows plus
#: the carry limit, so "this week versus last week" is answerable even when the
#: older window was reached by forward-fill.
_TREND_DAYS = 2 * weight_series.ROLLING_WINDOW_DAYS + weight_series.CARRY_LIMIT_DAYS

#: Bound the typed input before parsing. A weight is at most a handful of
#: characters; anything longer is a sentence, and treating it as a number would
#: only produce a confusing refusal.
_MAX_INPUT_CHARS = 20

_RANGE_HINT = (
    f"Give a weight in kilograms between {MIN_WEIGHT_KG:g} and {MAX_WEIGHT_KG:g} "
    "— for example <code>72.4</code>."
)


def parse_weight_text(text: str) -> float | None:
    """Read a typed weight, or ``None`` if the text is not one.

    Accepts what people actually send: ``72``, ``72.4``, ``72,4``, ``72.4 kg``,
    ``72.4kgs``. The comma is treated as a decimal separator only when the text
    contains no point — ``1,234.5`` is a thousands separator and is left alone,
    where it will fail to parse rather than silently becoming ``1.2345``.

    Range is *not* checked here: this answers "is this a number", and the caller
    answers "is this a plausible weight", so an out-of-range value can be told
    apart from a typo in the reply.
    """
    cleaned = (text or "").strip().lower()[:_MAX_INPUT_CHARS]
    for suffix in ("kilograms", "kilogram", "kilos", "kilo", "kgs", "kg"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
            break
    if "." not in cleaned and cleaned.count(",") == 1:
        cleaned = cleaned.replace(",", ".")
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    # float() accepts "nan" and "inf"; neither is a weight.
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _day_phrase(day: date, today: date) -> str:
    """Name a past day the way a person would."""
    delta = (today - day).days
    if delta <= 0:
        return "today"
    if delta == 1:
        return "yesterday"
    if delta < 7:
        return f"{delta} days ago"
    return day.strftime("%b %d")


async def _trend(db, user_id: int, today: date) -> weight_series.WeightTrend:
    """Summarize the recent record, gaps filled by the shared rule."""
    start = today - timedelta(days=_TREND_DAYS - 1)
    rows = await db.get_weight_logs(user_id, start, today)
    series = weight_series.daily_series(
        [(row["log_date"], row["weight_kg"]) for row in rows], start, today
    )
    return weight_series.summarize(series)


def _trend_line(trend: weight_series.WeightTrend) -> str | None:
    """One line of context under a saved weight, or ``None`` when it would lie.

    A single measurement has no trend, and a "7-day average" computed from one
    day is that day's weight wearing a more authoritative label.
    """
    if trend.measured_days < 2 or trend.average_kg is None:
        return None
    line = f"📈 7-day average <b>{format_kg(trend.average_kg)} kg</b>"
    change = trend.change_kg
    if change is not None:
        line += f" · {format_change(change)} vs the week before"
    return line


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
async def _prompt(message, context, user_id: int) -> int:
    """Ask for today's weight, offering the nudge row when one is meaningful."""
    db = context.bot_data["db"]
    today = today_local()
    today_kg = await db.get_weight_on(user_id, today)
    latest = await db.get_latest_weight(user_id)

    lines = ["⚖️ <b>Today's weight</b>", ""]
    if today_kg is not None:
        lines.append(
            f"Already recorded today: <b>{format_kg(today_kg)} kg</b>. "
            "Tap or type to correct it."
        )
        anchor = today_kg
    elif latest is not None:
        lines.append(
            f"Last weigh-in: <b>{format_kg(latest['weight_kg'])} kg</b> "
            f"({_day_phrase(latest['log_date'], today)})."
        )
        lines.append("Tap one, or type an exact number.")
        anchor = latest["weight_kg"]
    else:
        lines.append("Type your weight in kilograms — for example <code>72.4</code>.")
        anchor = None

    await reply_html(
        message,
        "\n".join(lines),
        reply_markup=weight_entry_keyboard(
            user_id, anchor, has_today=today_kg is not None
        ),
    )
    return ASK


async def _record(
    update: Update,
    context,
    message,
    user_id: int,
    weight: float,
    *,
    retry_state: int = ASK,
) -> int:
    """Write one day's weight and reply with what it means, then end the flow.

    ``retry_state`` is where a refusal leaves the user. Inside the guided flow
    that is ``ASK`` — the prompt is still on screen, so another try is natural.
    For the one-shot ``/weight 72.4`` form it is ``END``: no conversation was
    ever started, and returning ASK there would put PTB in a state the rest of
    the app does not believe is active.
    """
    db = context.bot_data["db"]
    today = today_local()
    user = update.effective_user

    if not (MIN_WEIGHT_KG <= weight <= MAX_WEIGHT_KG):
        await reply_html(message, f"❌ {_RANGE_HINT}")
        return retry_state

    try:
        await db.ensure_user(user.id, user.username, user.first_name)
        result = await db.log_weight(user_id, weight, today)
    except ValueError:
        await reply_html(message, f"❌ {_RANGE_HINT}")
        return retry_state
    except Exception:
        # Nothing was committed; say so plainly rather than leaving the user to
        # guess whether to send it again. The log carries no Telegram ID.
        logger.exception("Weight log failed; nothing was written")
        await reply_html(message, "⚠️ Couldn't save that. Try again.")
        return retry_state

    lines = [f"⚖️ <b>{format_kg(result.weight_kg)} kg</b> logged for today."]
    if result.replaced and result.change_kg:
        lines[0] = (
            f"⚖️ Today updated: {format_kg(result.previous_kg)} → "
            f"<b>{format_kg(result.weight_kg)} kg</b>."
        )
    trend_line = _trend_line(await _trend(db, user_id, today))
    if trend_line:
        lines.append(trend_line)
    else:
        lines.append(
            "<i>Weigh in a few more days and I'll show the 7-day trend.</i>"
        )

    finish_conversation(update, context, "weight")
    await reply_html(message, "\n".join(lines))
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
async def weight_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """``/weight`` — prompt; ``/weight 72.4`` — log it in one message.

    The quick form deliberately opens no conversation: a complete instruction
    needs no follow-up state, and leaving one behind would swallow the next
    ordinary message.
    """
    db = context.bot_data["db"]
    user = update.effective_user
    message = update.effective_message
    args = context.args or []

    if args:
        weight = parse_weight_text(" ".join(args))
        if weight is None:
            await reply_html(
                message,
                f"❌ I couldn't read <b>{escape_html(' '.join(args)[:_MAX_INPUT_CHARS])}</b> "
                f"as a weight. {_RANGE_HINT}",
            )
            return ConversationHandler.END
        return await _record(
            update,
            context,
            message,
            user.id,
            weight,
            retry_state=ConversationHandler.END,
        )

    await db.ensure_user(user.id, user.username, user.first_name)
    if not await conversation_available(update, context, "weight"):
        return ConversationHandler.END

    activate_conversation(update, context, "weight")
    try:
        return await _prompt(message, context, user.id)
    except BaseException:
        finish_conversation(update, context, "weight")
        raise


@authorized_callback
async def weight_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The ⚖️ Weight tap on Home."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    db = context.bot_data["db"]
    await db.ensure_user(user.id, user.username, user.first_name)
    if not await conversation_available(update, context, "weight"):
        return ConversationHandler.END

    activate_conversation(update, context, "weight")
    try:
        return await _prompt(query.message, context, user.id)
    except BaseException:
        finish_conversation(update, context, "weight")
        raise


# ---------------------------------------------------------------------------
# In-flow handlers
# ---------------------------------------------------------------------------
@authorized_callback
async def weight_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A nudge button, or "clear today"."""
    query = update.callback_query
    user_id = update.effective_user.id
    parsed = parse_weight_tap(query.data or "", user_id)
    if parsed is None:
        await query.answer("That button is no longer valid.", show_alert=True)
        return ASK

    action, weight = parsed
    await query.answer()
    await _retire(query)

    if action == "x":
        db = context.bot_data["db"]
        removed = await db.delete_weight(user_id, today_local())
        finish_conversation(update, context, "weight")
        await reply_html(
            query.message,
            "🗑 Today's weight removed."
            if removed
            else "🗑 Nothing was recorded for today.",
        )
        return ConversationHandler.END

    return await _record(update, context, query.message, user_id, weight)


async def receive_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A typed weight."""
    message = update.effective_message
    raw = (message.text or "").strip()
    weight = parse_weight_text(raw)
    if weight is None:
        await reply_html(
            message,
            f"❌ I couldn't read <b>{escape_html(raw[:_MAX_INPUT_CHARS])}</b> as a "
            f"weight. {_RANGE_HINT}",
        )
        return ASK
    return await _record(update, context, message, update.effective_user.id, weight)


async def _retire(query) -> None:
    """Take the buttons off a message that has been acted on."""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        logger.debug("Could not retire a weight keyboard", exc_info=True)


@authorized_callback
async def stale_weight_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Answer a nudge button whose conversation has already ended.

    Registered outside the conversation, so it only ever sees taps the flow did
    not claim — after a timeout, or on a prompt left in the scrollback. It
    deliberately writes nothing: a weight tapped from an expired prompt is a
    reading from an unknown day, and logging it as today's would be a guess.
    """
    query = update.callback_query
    parsed = parse_weight_tap(query.data or "", update.effective_user.id)
    if parsed is None:
        await query.answer(
            "This weight prompt belongs to another user or is no longer valid.",
            show_alert=True,
        )
        return
    await query.answer(
        "That weight prompt has expired — tap ⚖️ Weight or send /weight again.",
        show_alert=True,
    )
    await _retire(query)


_voice_guard = MessageHandler(filters.VOICE, voice_mid_flow_interceptor)
_control_guard = MessageHandler(ACTIVE_CONTROL_FILTER, active_flow_control_interceptor)
#: Matches ``wt_v_<user>_<hundredths>`` and ``wt_x_<user>``. Ownership is checked
#: in the handler; the pattern only keeps foreign callback families out.
_PATTERN = r"^wt_[vx]_\d+(?:_-?\d+)?$"

weight_conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("weight", weight_command, filters=AUTH_FILTER),
        CallbackQueryHandler(weight_menu_entry, pattern=r"^menu_weight$"),
    ],
    states={
        ASK: [
            _voice_guard,
            CallbackQueryHandler(weight_tap, pattern=_PATTERN),
            _control_guard,
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_weight),
        ],
        ConversationHandler.TIMEOUT: [TypeHandler(Update, timeout_handler)],
    },
    fallbacks=[
        cancel_handler,
        CommandHandler("weight", active_conversation_hint, filters=AUTH_FILTER),
        *home_fallback_handlers(),
    ],
    conversation_timeout=CONVERSATION_TIMEOUT,
    per_message=False,
)
