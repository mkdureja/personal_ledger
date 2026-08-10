"""/monitor handler — counted behaviours measured against a target.

/monitor        → today's board: counts, targets, and one tap to log
/monitor setup  → add/remove monitors and their targets

This module is the deliberate *opposite* of ``habits.py``. A habit asks whether
you did the good thing today and a tick is a win. A monitor asks how often
something happened — smoking, drinking, cannabis, cups of tea — and the answer is
a count that may carry a quantity and a variant.

Two consequences run through everything here:

**A tap is not idempotent.** ``habit_logs`` is uniquely keyed per day, so a
double tap is a no-op. A monitor's second tap is a second occurrence, because
that is what a second cigarette is. Every surface that logs one therefore renders
an undo beside it, and the undo removes exactly one row from exactly one day.

**A stale board is refused, not re-dated.** Habit and supplement checklists
accept today or yesterday: re-ticking an idempotent box on the wrong day costs
nothing. Filing an occurrence on the wrong day is a silent lie about a count that
somebody is trying to keep at zero, so a board from an earlier day is rejected
with an instruction to open a fresh one.

The arithmetic of "over" and "under" is not here. It lives in
:mod:`bot.monitor_targets`, which is pure and shared, so the meaning of a limit
cannot drift between this screen and any other reader of the same numbers.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from telegram import Message, Update
from telegram.error import BadRequest, TelegramError
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
    cancel_handler,
    conversation_available,
    conversation_is_active,
    escape_html,
    finish_conversation,
    reply_html,
    timeout_handler,
    voice_mid_flow_interceptor,
)
from ..keyboards import (
    MONITOR_VARIANT_CHOICES,
    monitor_board_keyboard,
    monitor_label,
    monitor_setup_keyboard,
    monitor_variant_keyboard,
    parse_monitor_tap,
)
from ..database import (
    MAX_ACTIVE_MONITORS,
    MAX_MONITOR_NAME_LENGTH,
    MAX_MONITOR_QUANTITY,
)
from ..monitor_targets import (
    Target,
    TargetParseError,
    format_progress,
    looks_like_target,
    parse_target,
    period_bounds,
    split_emoji_prefix,
)
from ..config import CONVERSATION_TIMEOUT, today_local

logger = logging.getLogger(__name__)

# Conversation states
ADDING_MONITOR = 0
LOGGING_DETAIL = 1

_SETUP_PROMPT_KEY = "monitor_setup_prompt"
_DETAIL_ID_KEY = "monitor_detail_id"
_DETAIL_PROMPT_KEY = "monitor_detail_prompt"

#: ``q:g`` / ``qty=ml`` — declares that occurrences carry a quantity, in this unit.
_QUANTITY_FIELD_RE = re.compile(r"^q(?:ty)?\s*[:=]\s*(.+)$", re.IGNORECASE)
#: The bare word that switches on per-occurrence variant text.
_VARIANT_FIELD_RE = re.compile(r"^(?:variant|variants|v)$", re.IGNORECASE)
#: A leading positive number and the rest of the field as its unit — the same
#: grammar the supplement dose uses, so "0.3 g" means one thing across the bot.
_QUANTITY_RE = re.compile(r"^(\d+(?:\.\d+)?|\.\d+)\s*(.*)$")
#: Whether a field was *meant* as a quantity. Anything opening with a sign or a
#: digit is a quantity attempt, so ``-1 g`` is rejected outright rather than
#: quietly stored as a variant named "-1 g" — the same rule the supplement dose
#: uses, for the same reason: guessing here files nonsense as if it were asked for.
_QUANTITY_ATTEMPT_RE = re.compile(r"^[+-]?\.?\d")


class MonitorInputError(ValueError):
    """A user-facing reason that typed monitor input was rejected."""


# ---------------------------------------------------------------------------
# Typed input grammars
# ---------------------------------------------------------------------------
def parse_monitor_input(text: str) -> dict[str, object]:
    """Parse ``Name, target, q:unit, variant`` into validated monitor fields.

    Kept pure and separate from Telegram so the grammar can be tested directly.
    Setup is a rare, one-time action, so a typed line is proportionate — and
    every field after the name is optional:

    * ``Smoking, zero`` → a zero target
    * ``🍺 Drinking, 1/month`` → a glyph, and a monthly ceiling
    * ``🌿 Cannabis, 2-4/week, q:g, variant`` → a band, plus quantity and
      variant recorded per occurrence
    * ``Tea, <=5/day`` → a daily cap
    * ``Screens`` → counted, with no target at all

    Raises :class:`MonitorInputError` with a message meant for the user.
    """
    parts = [part.strip() for part in str(text).split(",")]
    if len(parts) > 4:
        raise MonitorInputError(
            "Use at most: name, target, q:unit, variant — for example "
            "<i>🌿 Cannabis, 2-4/week, q:g, variant</i>."
        )

    emoji, name = split_emoji_prefix(parts[0])
    if not name:
        raise MonitorInputError("Monitor name can't be empty.")
    if len(name) > MAX_MONITOR_NAME_LENGTH:
        raise MonitorInputError(
            f"Monitor name too long (max {MAX_MONITOR_NAME_LENGTH} chars)."
        )

    target = Target()
    quantity_unit: str | None = None
    tracks_quantity = False
    tracks_variant = False
    seen_target = False

    for field in (part for part in parts[1:] if part):
        match = _QUANTITY_FIELD_RE.match(field)
        if match is not None:
            quantity_unit = match.group(1).strip() or None
            tracks_quantity = True
            continue
        if _VARIANT_FIELD_RE.match(field):
            tracks_variant = True
            continue
        if looks_like_target(field):
            if seen_target:
                raise MonitorInputError("Only one target per monitor.")
            try:
                target = parse_target(field)
            except TargetParseError as exc:
                raise MonitorInputError(str(exc)) from exc
            seen_target = True
            continue
        raise MonitorInputError(
            f"I couldn't read <i>{escape_html(field)}</i>. Fields after the name "
            "are a target (<i>zero</i>, <i>2-4/week</i>), <i>q:unit</i>, "
            "or <i>variant</i>."
        )

    return {
        "name": name,
        "emoji": emoji,
        "target": target,
        "tracks_quantity": tracks_quantity,
        "quantity_unit": quantity_unit,
        "tracks_variant": tracks_variant,
    }


def parse_occurrence_detail(text: str) -> dict[str, object]:
    """Parse ``0.3 g, hybrid`` into ``quantity``/``quantity_unit``/``variant``.

    One field that opens with a number is a quantity; one that does not is a
    variant. That is the same "does it look like a number" rule the supplement
    dose uses, and it means ``hybrid`` alone and ``0.3 g`` alone both work
    without the user having to remember an order.
    """
    parts = [part.strip() for part in str(text).split(",") if part.strip()]
    if not parts:
        raise MonitorInputError("Send a quantity, a variant, or both.")
    if len(parts) > 2:
        raise MonitorInputError(
            "Use at most: quantity, variant — for example <i>0.3 g, hybrid</i>."
        )

    quantity: float | None = None
    quantity_unit: str | None = None
    variant: str | None = None

    for field in parts:
        if _QUANTITY_ATTEMPT_RE.match(field):
            if quantity is not None:
                raise MonitorInputError("Only one quantity per occurrence.")
            match = _QUANTITY_RE.match(field)
            if match is None:
                raise MonitorInputError(
                    "The quantity should start with a positive number — for "
                    "example <i>0.3 g</i>."
                )
            try:
                quantity = float(match.group(1))
            except (TypeError, ValueError, OverflowError):
                raise MonitorInputError("That quantity isn't a number.") from None
            if quantity <= 0 or quantity > MAX_MONITOR_QUANTITY:
                raise MonitorInputError(
                    f"The quantity must be greater than 0 and at most "
                    f"{MAX_MONITOR_QUANTITY:g}."
                )
            quantity_unit = match.group(2).strip() or None
        elif variant is None:
            variant = field
        else:
            raise MonitorInputError(
                "Use at most: quantity, variant — for example <i>0.3 g, hybrid</i>."
            )

    return {
        "quantity": quantity,
        "quantity_unit": quantity_unit,
        "variant": variant,
    }


# ---------------------------------------------------------------------------
# Board rendering
# ---------------------------------------------------------------------------
def _target_of(monitor: dict) -> Target:
    """The stored target as a :class:`Target`, falling back to untargeted.

    A row whose stored bounds cannot form a coherent target must not take the
    whole board down: the monitor renders as merely counted, which is the honest
    reading of "we no longer know what the limit was".
    """
    try:
        return Target(
            period=monitor.get("target_period") or "day",
            minimum=monitor.get("target_min"),
            maximum=monitor.get("target_max"),
        )
    except ValueError:
        logger.warning("Monitor %s has an unreadable target", monitor.get("id"))
        return Target()


def _format_quantity(value: object) -> str:
    """Render a stored quantity without a needless trailing zero."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _detail_suffix(entries: list[dict]) -> str:
    """The day's recorded quantities and variants, one indented line each.

    A line per occurrence rather than one run-on line: three entries in a day is
    normal for a monitor that tracks detail, and "0.3 g · hybrid, 0.2 g · indica"
    on one line stops being scannable at exactly the point it starts being worth
    reading.
    """
    lines: list[str] = []
    for entry in entries:
        bits: list[str] = []
        if entry.get("quantity") is not None:
            rendered = _format_quantity(entry["quantity"])
            unit = (entry.get("quantity_unit") or "").strip()
            if rendered:
                bits.append(f"{rendered} {unit}".strip())
        if (entry.get("variant") or "").strip():
            bits.append(str(entry["variant"]).strip())
        if bits:
            lines.append("\n    └ " + escape_html(" · ".join(bits)))
    return "".join(lines)


async def _board_view(db, user_id: int, today: date) -> tuple[str, object]:
    """Compose the board's text and keyboard for ``today``.

    One counts query covers every monitor: the widest window any target can name
    is the calendar month, so the month-to-date rows are fetched once and each
    monitor's own period is summed out of them by
    :func:`bot.monitor_targets.period_bounds`. Three separate period queries
    would put the definition of "this week" in this file as well as in the pure
    module, and the two would eventually disagree.
    """
    monitors = await db.get_active_monitors(user_id)
    if not monitors:
        return (
            "🎯 <b>Monitors</b>\n\n"
            "Nothing is being monitored yet.\n"
            "Use /monitor setup to add something — a monitor counts how often "
            "something happens and compares it to a target you set.",
            None,
        )

    window_start = today.replace(day=1)
    # A week can start in the previous calendar month, so the fetch has to open
    # at whichever boundary is earlier or a Monday-to-Sunday count would be cut.
    week_start, _ = period_bounds("week", today)
    window_start = min(window_start, week_start)
    counts = await db.get_monitor_daily_counts(user_id, window_start, today)

    today_key = today.isoformat()
    today_counts: dict[int, int] = {}
    lines: list[str] = []

    for monitor in monitors:
        monitor_id = int(monitor["id"])
        by_day = counts.get(monitor_id, {})
        today_counts[monitor_id] = by_day.get(today_key, 0)

        target = _target_of(monitor)
        start, end = period_bounds(target.period, today)
        period_count = sum(
            occurrences
            for day, occurrences in by_day.items()
            if start.isoformat() <= day <= end.isoformat()
        )

        line = (
            f"{escape_html(monitor_label(monitor))} — "
            f"{escape_html(format_progress(period_count, target))}"
        )
        if today_counts[monitor_id] and target.period != "day":
            # The period count answers "how am I doing"; today's count answers
            # "did I already log this today". A weekly target hides the second.
            line += f"\n    <i>{today_counts[monitor_id]} today</i>"
        if today_counts[monitor_id] and (
            monitor.get("tracks_quantity") or monitor.get("tracks_variant")
        ):
            entries = await db.get_monitor_day_entries(user_id, monitor_id, today)
            line += _detail_suffix(entries)
        lines.append(line)

    text = (
        f"🎯 <b>Monitors</b> — {today:%a %d %b}\n\n"
        + "\n".join(lines)
        + "\n\nTap to log one. ↩️ removes the last one from today."
    )
    return text, monitor_board_keyboard(monitors, user_id, today_counts)


async def show_monitor_board(
    message: Message, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    """Display today's monitor board."""
    db = context.bot_data["db"]
    text, keyboard = await _board_view(db, user_id, today_local())
    await reply_html(message, text, reply_markup=keyboard)


async def _refresh_board(query, context, user_id: int) -> None:
    """Re-render the board in place after a change."""
    db = context.bot_data["db"]
    text, keyboard = await _board_view(db, user_id, today_local())
    try:
        await query.edit_message_text(
            text, reply_markup=keyboard, parse_mode="HTML"
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
        logger.debug("Monitor board was already up to date")


# ---------------------------------------------------------------------------
# /monitor — entry point
# ---------------------------------------------------------------------------
async def monitor_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int | None:
    """Handle /monitor and /monitor setup."""
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)

    args = context.args or []
    if args and args[0].lower() == "setup":
        if not await conversation_available(update, context, "monitors"):
            return ConversationHandler.END
        activate_conversation(update, context, "monitors")
        try:
            return await _show_setup(update.message, context, user.id)
        except BaseException:
            finish_conversation(update, context, "monitors")
            raise

    await show_monitor_board(update.message, context, user.id)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Board callbacks
# ---------------------------------------------------------------------------
async def _reject_callback(query, text: str, *, clear_keyboard: bool = True) -> None:
    """Acknowledge an invalid callback and retire its stale keyboard."""
    try:
        await query.answer(text, show_alert=True)
    except TelegramError:
        logger.debug("Could not answer stale monitor callback", exc_info=True)
    if clear_keyboard:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            logger.debug("Could not remove stale monitor keyboard", exc_info=True)


async def _active_monitor(db, user_id: int, monitor_id: int) -> dict | None:
    """The user's active monitor with this id, or ``None``."""
    monitors = await db.get_active_monitors(user_id)
    for monitor in monitors:
        if int(monitor["id"]) == monitor_id:
            return monitor
    return None


async def _parse_board_tap(query, user_id: int, expected: str):
    """Validate an owned board callback and return its monitor id."""
    parsed = parse_monitor_tap(query.data or "", user_id)
    if parsed is None:
        await _reject_callback(
            query,
            "This monitor button belongs to another user or is no longer valid.",
            clear_keyboard=False,
        )
        return None
    action, monitor_id, extra = parsed
    if action != expected:
        await _reject_callback(query, "This monitor button is no longer valid.")
        return None
    return monitor_id, extra


@authorized_callback
async def monitor_add_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Log one occurrence with no detail — the fast path."""
    query = update.callback_query
    user_id = update.effective_user.id
    parsed = await _parse_board_tap(query, user_id, "a")
    if parsed is None:
        return
    monitor_id, _extra = parsed

    db = context.bot_data["db"]
    today = today_local()
    if await db.log_monitor_occurrence(user_id, monitor_id, today) is None:
        await _reject_callback(query, "This monitor is no longer active.")
        return

    await query.answer("Logged")
    await _refresh_board(query, context, user_id)


@authorized_callback
async def monitor_undo_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Remove today's most recent occurrence for one monitor."""
    query = update.callback_query
    user_id = update.effective_user.id
    parsed = await _parse_board_tap(query, user_id, "z")
    if parsed is None:
        return
    monitor_id, _extra = parsed

    db = context.bot_data["db"]
    today = today_local()
    if not await db.delete_last_monitor_occurrence(user_id, monitor_id, today):
        # Nothing left to undo — a second tap on a board that has already been
        # emptied. Say so and refresh rather than implying something was removed.
        await query.answer("Nothing logged today for that one.")
        await _refresh_board(query, context, user_id)
        return

    await query.answer("Removed")
    await _refresh_board(query, context, user_id)


@authorized_callback
async def monitor_variant_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Log one occurrence with a recently used variant, by index."""
    query = update.callback_query
    user_id = update.effective_user.id
    parsed = await _parse_board_tap(query, user_id, "v")
    if parsed is None:
        return LOGGING_DETAIL
    monitor_id, index = parsed
    if index is None:
        await _reject_callback(query, "This monitor button is no longer valid.")
        return ConversationHandler.END

    db = context.bot_data["db"]
    variants = await db.get_recent_monitor_variants(
        user_id, monitor_id, MONITOR_VARIANT_CHOICES
    )
    if index < 0 or index >= len(variants):
        await _reject_callback(
            query, "That shortcut is out of date. Open /monitor for a fresh one."
        )
        return ConversationHandler.END

    today = today_local()
    logged = await db.log_monitor_occurrence(
        user_id, monitor_id, today, variant=variants[index]
    )
    if logged is None:
        await _reject_callback(query, "This monitor is no longer active.")
        return ConversationHandler.END

    await query.answer("Logged")
    context.user_data.pop(_DETAIL_ID_KEY, None)
    context.user_data.pop(_DETAIL_PROMPT_KEY, None)
    finish_conversation(update, context, "monitors")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        logger.debug("Could not retire monitor detail keyboard", exc_info=True)
    await show_monitor_board(query.message, context, user_id)
    return ConversationHandler.END


@authorized_callback
async def monitor_detail_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Leave the detail prompt without logging anything."""
    query = update.callback_query
    user_id = update.effective_user.id
    parsed = await _parse_board_tap(query, user_id, "x")
    if parsed is None:
        return ConversationHandler.END

    await query.answer()
    context.user_data.pop(_DETAIL_ID_KEY, None)
    context.user_data.pop(_DETAIL_PROMPT_KEY, None)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        logger.debug("Could not retire monitor detail keyboard", exc_info=True)
    finish_conversation(update, context, "monitors")
    return ConversationHandler.END


@authorized_callback
async def monitor_noop_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Answer the inert name label in setup without ever writing."""
    query = update.callback_query
    user_id = update.effective_user.id
    parsed = parse_monitor_tap(query.data or "", user_id)
    if parsed is None or parsed[0] != "noop":
        await _reject_callback(
            query, "This monitor button is no longer valid.", clear_keyboard=False
        )
        return
    await query.answer(
        "Monitor Setup: use ❌ Remove, or type a new monitor to add.",
        show_alert=True,
    )


# ---------------------------------------------------------------------------
# Detail flow — quantity and variant for one occurrence
# ---------------------------------------------------------------------------
@authorized_callback
async def monitor_detail_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Open the prompt that logs one occurrence with quantity and/or variant."""
    query = update.callback_query
    user_id = update.effective_user.id
    parsed = await _parse_board_tap(query, user_id, "d")
    if parsed is None:
        return ConversationHandler.END
    monitor_id, _extra = parsed

    db = context.bot_data["db"]
    monitor = await _active_monitor(db, user_id, monitor_id)
    if monitor is None:
        await _reject_callback(query, "This monitor is no longer active.")
        return ConversationHandler.END

    if not await conversation_available(update, context, "monitors"):
        await query.answer()
        return ConversationHandler.END
    activate_conversation(update, context, "monitors")

    await query.answer()
    context.user_data[_DETAIL_ID_KEY] = monitor_id

    wants: list[str] = []
    unit = (monitor.get("quantity_unit") or "").strip()
    if monitor.get("tracks_quantity"):
        wants.append(f"a quantity (<i>0.3 {escape_html(unit)}</i>)" if unit else "a quantity")
    if monitor.get("tracks_variant"):
        wants.append("a variant (<i>hybrid</i>)")
    if not wants:
        # Only reachable from a board drawn before the monitor's detail flags
        # were switched off. Prompt for either rather than for nothing.
        wants.append("a quantity or a variant")
    example = "0.3 g, hybrid" if len(wants) > 1 else ("0.3 g" if unit else "hybrid")

    variants = (
        await db.get_recent_monitor_variants(
            user_id, monitor_id, MONITOR_VARIANT_CHOICES
        )
        if monitor.get("tracks_variant")
        else []
    )

    prompt = await reply_html(
        query.message,
        f"📝 <b>{escape_html(monitor_label(monitor))}</b>\n\n"
        f"Send {' and '.join(wants)} — for example <i>{escape_html(example)}</i>.\n"
        "Either part on its own is fine."
        + ("\nOr tap one you used recently:" if variants else ""),
        reply_markup=monitor_variant_keyboard(user_id, monitor_id, variants),
    )
    location = _message_location(prompt)
    if location is not None:
        context.user_data[_DETAIL_PROMPT_KEY] = location
    return LOGGING_DETAIL


async def log_detail_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Log one occurrence from a typed ``quantity, variant`` line."""
    monitor_id = context.user_data.get(_DETAIL_ID_KEY)
    if not isinstance(monitor_id, int):
        await update.message.reply_text(
            "❌ That prompt expired. Open /monitor and tap 📝 again."
        )
        finish_conversation(update, context, "monitors")
        return ConversationHandler.END

    try:
        fields = parse_occurrence_detail(update.message.text or "")
    except MonitorInputError as exc:
        await reply_html(update.message, f"❌ {exc}")
        return LOGGING_DETAIL

    db = context.bot_data["db"]
    user_id = update.effective_user.id
    logged = await db.log_monitor_occurrence(
        user_id,
        monitor_id,
        today_local(),
        quantity=fields["quantity"],
        quantity_unit=fields["quantity_unit"],
        variant=fields["variant"],
    )
    if logged is None:
        await update.message.reply_text("❌ That monitor is no longer active.")
    context.user_data.pop(_DETAIL_ID_KEY, None)
    await _retire_prompt(context, _DETAIL_PROMPT_KEY)
    finish_conversation(update, context, "monitors")
    await show_monitor_board(update.message, context, user_id)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Setup flow — add/remove monitors
# ---------------------------------------------------------------------------
def _message_location(message) -> tuple[int, int] | None:
    """Return a stable ``(chat_id, message_id)`` pair when available."""
    chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    if isinstance(chat_id, int) and isinstance(message_id, int):
        return chat_id, message_id
    return None


async def _retire_prompt(context, key: str) -> None:
    """Best-effort retirement of a previously active keyboard."""
    previous = context.user_data.pop(key, None)
    bot = getattr(context, "bot", None)
    if previous is None or bot is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=previous[0], message_id=previous[1], reply_markup=None
        )
    except TelegramError:
        logger.debug("Could not retire a previous monitor keyboard", exc_info=True)


def _is_current_setup_callback(update: Update, context) -> bool:
    """Validate that a setup callback belongs to the active prompt and chat."""
    return conversation_is_active(update, context, "monitors") and context.user_data.get(
        _SETUP_PROMPT_KEY
    ) == _message_location(update.callback_query.message)


def _setup_view(monitors: list[dict], user_id: int):
    """Build the setup text and keyboard."""
    if monitors:
        at_limit = len(monitors) >= MAX_ACTIVE_MONITORS
        limit_note = (
            f"\nMaximum reached ({MAX_ACTIVE_MONITORS}); remove one before adding."
            if at_limit
            else ""
        )
        listed = "\n".join(
            f"• {escape_html(monitor_label(monitor))} — "
            f"{escape_html(_target_of(monitor).describe())}"
            for monitor in monitors
        )
        text = (
            "⚙️ <b>Monitor Setup</b>\n\n"
            f"{listed}\n\n"
            "Tap ❌ to remove, or type a new monitor to add:"
            f"{limit_note}"
        )
    else:
        text = (
            "⚙️ <b>Monitor Setup</b>\n\n"
            "Nothing monitored yet. Type one to add it:\n"
            "<i>Smoking, zero</i>\n"
            "<i>🍺 Drinking, 1/month</i>\n"
            "<i>🌿 Cannabis, 2-4/week, q:g, variant</i>\n\n"
            "The target is optional, <i>q:unit</i> records how much, and "
            "<i>variant</i> records which kind.\n"
            "Use /cancel when done."
        )
    return text, monitor_setup_keyboard(monitors, user_id)


async def _show_setup(
    message: Message, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> int:
    """Show current monitors with remove buttons and prompt to add."""
    db = context.bot_data["db"]
    monitors = await db.get_active_monitors(user_id)
    await _retire_prompt(context, _SETUP_PROMPT_KEY)

    text, keyboard = _setup_view(monitors, user_id)
    prompt = await reply_html(message, text, reply_markup=keyboard)

    location = _message_location(prompt)
    if location is not None:
        context.user_data[_SETUP_PROMPT_KEY] = location
    return ADDING_MONITOR


async def add_monitor_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Add a monitor from a typed ``name, target, q:unit, variant`` line."""
    raw = (update.message.text or "").strip()
    if not raw:
        await update.message.reply_text("❌ Monitor name can't be empty.")
        return ADDING_MONITOR

    try:
        fields = parse_monitor_input(raw)
    except MonitorInputError as exc:
        await reply_html(update.message, f"❌ {exc}")
        return ADDING_MONITOR

    db = context.bot_data["db"]
    user_id = update.effective_user.id

    active = await db.get_active_monitors(user_id)
    if len(active) >= MAX_ACTIVE_MONITORS:
        await update.message.reply_text(
            f"❌ You can have at most {MAX_ACTIVE_MONITORS} active monitors. "
            "Remove one before adding another."
        )
        return ADDING_MONITOR

    target: Target = fields["target"]  # type: ignore[assignment]
    _monitor_id, status = await db.add_monitor(
        user_id,
        str(fields["name"]),
        emoji=fields["emoji"],  # type: ignore[arg-type]
        target_period=target.period,
        target_min=target.minimum,
        target_max=target.maximum,
        tracks_quantity=bool(fields["tracks_quantity"]),
        quantity_unit=fields["quantity_unit"],  # type: ignore[arg-type]
        tracks_variant=bool(fields["tracks_variant"]),
    )

    safe_name = escape_html(str(fields["name"]))
    detail = escape_html(f" — {target.describe()}")
    if status == "reactivated":
        await reply_html(update.message, f"♻️ Reactivated: <b>{safe_name}</b>{detail}")
    elif status == "already_active":
        await reply_html(update.message, f"ℹ️ Already active: <b>{safe_name}</b>")
    else:
        await reply_html(update.message, f"✅ Added: <b>{safe_name}</b>{detail}")

    return await _show_setup(update.message, context, user_id)


@authorized_callback
async def remove_monitor_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Remove (deactivate) a monitor, preserving its occurrence history."""
    query = update.callback_query
    user_id = update.effective_user.id
    parsed = parse_monitor_tap(query.data or "", user_id)
    if parsed is None or parsed[0] != "rm":
        await _reject_callback(
            query, "This remove button is no longer valid.", clear_keyboard=False
        )
        return
    if not _is_current_setup_callback(update, context):
        await _reject_callback(query, "This monitor setup has expired.")
        return

    db = context.bot_data["db"]
    if not await db.deactivate_monitor(user_id, parsed[1]):
        await _reject_callback(query, "This monitor is no longer active.")
        return

    await query.answer()
    monitors = await db.get_active_monitors(user_id)
    text, keyboard = _setup_view(monitors, user_id)
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")


@authorized_callback
async def monitor_setup_done_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Close monitor setup and show a fresh board."""
    query = update.callback_query
    user_id = update.effective_user.id
    parsed = parse_monitor_tap(query.data or "", user_id)
    if parsed is None or parsed[0] != "done":
        await _reject_callback(query, "This setup button is no longer valid.")
        if conversation_is_active(update, context, "monitors"):
            return ADDING_MONITOR
        return ConversationHandler.END
    if not _is_current_setup_callback(update, context):
        await _reject_callback(query, "This monitor setup has expired.")
        if conversation_is_active(update, context, "monitors"):
            return ADDING_MONITOR
        return ConversationHandler.END

    await query.answer()
    context.user_data.pop(_SETUP_PROMPT_KEY, None)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        logger.debug("Could not retire monitor setup keyboard", exc_info=True)
    try:
        await show_monitor_board(query.message, context, user_id)
    except TelegramError:
        logger.warning("Could not deliver monitor board", exc_info=True)
    finish_conversation(update, context, "monitors")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler
# ---------------------------------------------------------------------------
_voice_guard = MessageHandler(filters.VOICE, voice_mid_flow_interceptor)
_control_guard = MessageHandler(ACTIVE_CONTROL_FILTER, active_flow_control_interceptor)

# Two entry points, two states. ``/monitor setup`` opens the setup list; tapping
# 📝 on the board enters the detail prompt directly. They share a conversation so
# only one of them can be live at a time — a half-typed occurrence and a
# half-typed monitor definition would otherwise both be waiting for the same
# next message.
monitor_conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("monitor", monitor_command, filters=AUTH_FILTER),
        CommandHandler("monitors", monitor_command, filters=AUTH_FILTER),
        CallbackQueryHandler(monitor_detail_callback, pattern=r"^mon_d_"),
    ],
    states={
        ADDING_MONITOR: [
            _voice_guard,
            CallbackQueryHandler(monitor_setup_done_callback, pattern=r"^mon_done_"),
            CallbackQueryHandler(remove_monitor_callback, pattern=r"^mon_rm_"),
            CallbackQueryHandler(monitor_noop_callback, pattern=r"^mon_noop_"),
            _control_guard,
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_monitor_text),
        ],
        LOGGING_DETAIL: [
            _voice_guard,
            CallbackQueryHandler(monitor_variant_callback, pattern=r"^mon_v_"),
            CallbackQueryHandler(
                monitor_detail_cancel_callback, pattern=r"^mon_x_"
            ),
            _control_guard,
            MessageHandler(filters.TEXT & ~filters.COMMAND, log_detail_text),
        ],
        ConversationHandler.TIMEOUT: [TypeHandler(Update, timeout_handler)],
    },
    fallbacks=[
        cancel_handler,
        CommandHandler("monitor", active_conversation_hint, filters=AUTH_FILTER),
        CommandHandler("monitors", active_conversation_hint, filters=AUTH_FILTER),
        # Home is always reachable: it ends this flow and reports anything
        # unsaved. Must be a fallback — a handler outside the conversation
        # cannot return END into it, so the state would linger and swallow
        # the next ordinary message.
        *home_fallback_handlers(),
    ],
    conversation_timeout=CONVERSATION_TIMEOUT,
)
