"""
Common handler utilities: auth filter, error handler, /cancel, /undo, validators.
"""

from __future__ import annotations

import html
import logging
import math
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, TypeVar

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    ConversationHandler,
    filters,
)

from ..config import ALLOWED_USER_IDS
from ..database import MutationSource
from ..keyboards import (
    LEGACY_REPEAT_BUTTON_LABEL,
    MEAL_BUTTON_LABEL,
    REPEAT_BUTTON_LABEL,
)

logger = logging.getLogger(__name__)

CallbackResult = TypeVar("CallbackResult")

# ---------------------------------------------------------------------------
# Auth filter — compose into every handler
# ---------------------------------------------------------------------------
# Restrict to the allowlist AND to private chats, so a command accidentally
# sent in a group never exposes personal activity to other members.
AUTH_FILTER = filters.User(user_id=ALLOWED_USER_IDS) & filters.ChatType.PRIVATE

def normalize_control_text(text: str) -> str:
    """Trim and case-fold a message for whole-string control matching.

    Phase 1 controls are matched on the trimmed, case-folded, *whole* string; no
    Unicode compatibility normalization or internal-whitespace collapsing is
    applied (plan §8.1), so ``hi there`` and ``meal prep`` stay ordinary text.
    """
    return text.strip().casefold()


# Normalized control text -> the action it performs. Built from the keyboard's own
# label constants, so renaming a button cannot leave a handler matching only the
# old text. Both the current and legacy Repeat labels map to one action for the
# compatibility window: a persistent keyboard already on a user's client keeps
# sending the old text until they receive a new one.
HOME_ACTIONS = {
    normalize_control_text(MEAL_BUTTON_LABEL): "meal",
    normalize_control_text(REPEAT_BUTTON_LABEL): "repeat",
    normalize_control_text(LEGACY_REPEAT_BUTTON_LABEL): "repeat",
    "describe": "describe",
}
HOME_WORDS = {"home"}
GREETINGS = {"hi", "hello", "hey"}
#: Normalized labels that are a real Diet entry point (kept unchanged in this
#: slice, but derived rather than duplicated).
MEAL_LABELS = frozenset(
    key for key, action in HOME_ACTIONS.items() if action == "meal"
)


class _NormalizedControlFilter(filters.MessageFilter):
    """Match a message whose whole normalized text is one of ``allowed``."""

    def __init__(self, allowed: set[str], name: str) -> None:
        super().__init__(name=name)
        self._allowed = frozenset(allowed)

    def filter(self, message: Any) -> bool:
        text = getattr(message, "text", None)
        if not text:
            return False
        return normalize_control_text(text) in self._allowed


# The reply-keyboard label(s) that are a real Diet entry point.
MEAL_LABEL_FILTER = _NormalizedControlFilter(set(MEAL_LABELS), name="MealLabel")
# Any Home action (meal/repeat/describe), including the legacy Repeat label.
HOME_ACTION_FILTER = _NormalizedControlFilter(set(HOME_ACTIONS), name="HomeAction")
# Greetings or the word "home".
GREETING_HOME_FILTER = _NormalizedControlFilter(
    set(GREETINGS) | set(HOME_WORDS), name="GreetingHome"
)
# Every active-flow control word (used by state control interceptors).
ACTIVE_CONTROL_FILTER = _NormalizedControlFilter(
    set(HOME_ACTIONS) | set(GREETINGS) | set(HOME_WORDS), name="ActiveControl"
)
# Diet states re-render on a Meal label but nudge on every other control word, so
# a non-meal control interceptor excludes them.
DIET_NONMEAL_CONTROL_FILTER = _NormalizedControlFilter(
    (set(HOME_ACTIONS) - set(MEAL_LABELS)) | set(GREETINGS) | set(HOME_WORDS),
    name="DietNonMealControl",
)


_ACTIVE_CONVERSATION_KEY = "_ledger_active_conversation"
_CONVERSATION_LABELS = {
    "study": "a study session",
    "gym": "a workout",
    "diet": "a meal",
    "habits": "habit setup",
    "supplements": "supplement setup",
}
_CONVERSATION_DATA_KEYS = {
    "study": ("study_subject", "study_duration"),
    "gym": (
        "gym_exercises",
        "gym_current_exercise",
        "gym_current_sets",
        "gym_current_reps",
        "gym_more_message_id",
    ),
    "diet": (
        "diet_meal_message_id",
        "diet_meal_type",
        "diet_food_items",
        "diet_calories",
        "diet_ui_message_id",
        "diet_ui_revision",
        "diet_entry_mode",
        "diet_choice_page",
        "diet_sel_kind",
        "diet_sel_id",
        "diet_recent_qtys",
        "diet_items",
        "diet_quick_pending_item",
        "diet_default_source_type",
        "diet_default_source_id",
        "diet_pending_default",
        "diet_current_source_meal_id",
        "diet_current_child_ids",
        "diet_current_decisions",
        "diet_current_digest",
        "diet_edit_index",
    ),
    "habits": ("habit_setup_prompt",),
    "supplements": ("supplement_setup_prompt",),
}
_MAX_UNDO_TEXT_LENGTH = 400
_MAX_UNDO_VALUE_LENGTH = 32


def _conversation_chat_id(update: Update) -> int | None:
    """Extract the chat owning a conversation marker."""
    chat = getattr(update, "effective_chat", None)
    return getattr(chat, "id", None)


def conversation_is_active(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    flow: str,
) -> bool:
    """Return whether ``flow`` owns the current user's marker in this chat."""
    return current_conversation(update, context) == flow


def active_conversation_flow(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Return any guided flow marked active for the current user."""
    active = context.user_data.get(_ACTIVE_CONVERSATION_KEY)
    if not isinstance(active, tuple) or len(active) != 2:
        return None
    return str(active[0])


def current_conversation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> str | None:
    """Return the guided flow active in this chat, if any."""
    active = context.user_data.get(_ACTIVE_CONVERSATION_KEY)
    if not isinstance(active, tuple) or len(active) != 2:
        return None
    active_flow, active_chat_id = active
    if active_chat_id != _conversation_chat_id(update):
        return None
    return str(active_flow)


def escape_html(value: object) -> str:
    """Escape a dynamic value for inclusion in a Telegram HTML message."""
    return html.escape(str(value))


def _bounded_html(value: object, max_chars: int) -> str:
    """Escape a legacy value after bounding its contribution to a message."""
    text = str(value)
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return escape_html(text)


async def conversation_available(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    flow: str,
) -> bool:
    """Return whether ``flow`` may start without overlapping another flow."""
    active = context.user_data.get(_ACTIVE_CONVERSATION_KEY)
    if active is None or conversation_is_active(update, context, flow):
        return True

    active_flow = active[0] if isinstance(active, tuple) else str(active)
    label = _CONVERSATION_LABELS.get(active_flow, "another guided log")
    await update.effective_message.reply_text(
        f"⏳ You're already in {label}. Finish it or use /cancel in the chat "
        "where it started."
    )
    return False


def activate_conversation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    flow: str,
) -> None:
    """Mark one guided flow as active for this user."""
    context.user_data[_ACTIVE_CONVERSATION_KEY] = (
        flow,
        _conversation_chat_id(update),
    )


def finish_conversation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    flow: str | None = None,
) -> bool:
    """Release the active-flow marker without disturbing another flow's data."""
    active = context.user_data.get(_ACTIVE_CONVERSATION_KEY)
    if not isinstance(active, tuple):
        return False
    active_flow, active_chat_id = active
    if active_chat_id != _conversation_chat_id(update):
        return False
    if flow is None or active_flow == flow:
        context.user_data.pop(_ACTIVE_CONVERSATION_KEY, None)
        for key in _CONVERSATION_DATA_KEYS.get(active_flow, ()):
            context.user_data.pop(key, None)
        return True
    return False


async def deliver_or_end(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    flow: str,
    coro: Awaitable[Any],
) -> bool:
    """Await a state-advancing prompt; end and clear the flow if it fails to send.

    Returns ``True`` when the prompt was delivered (the caller then returns the
    next conversation state) and ``False`` when delivery raised a ``TelegramError``
    (the caller returns ``ConversationHandler.END``). Because
    :func:`finish_conversation` also clears the flow's pending ``user_data``, the
    stored state and the conversation position can never disagree after a handled
    send failure — a half-advanced flow is impossible.
    """
    try:
        await coro
        return True
    except TelegramError:
        logger.warning(
            "Prompt delivery failed in '%s' flow; ending it cleanly",
            flow,
            exc_info=True,
        )
        finish_conversation(update, context, flow)
        return False


async def reply_html(message: Any, text: str, **kwargs: Any) -> Any:
    """Reply with trusted HTML markup and an explicit Telegram parse mode.

    Callers must pass dynamic values through :func:`escape_html` before
    interpolating them into ``text``.
    """
    kwargs["parse_mode"] = ParseMode.HTML
    return await message.reply_text(text, **kwargs)


def authorized_callback(
    handler: Callable[..., Awaitable[CallbackResult]],
) -> Callable[..., Awaitable[CallbackResult | None]]:
    """Reject inline-button presses from users outside the configured allowlist."""

    @wraps(handler)
    async def wrapped(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args: Any, **kwargs: Any
    ) -> CallbackResult | None:
        user = update.effective_user
        query = update.callback_query
        chat = getattr(update, "effective_chat", None)
        chat_type = getattr(chat, "type", None)
        # Fail closed: require an explicit private chat. Missing chat context
        # (chat_type is None) is denied rather than allowed, so a callback that
        # arrives without a resolvable private chat can never act on user data.
        denied = (
            user is None
            or user.id not in ALLOWED_USER_IDS
            or chat_type != ChatType.PRIVATE
        )
        if denied:
            if query is not None:
                # Acknowledge the press to stop Telegram's loading spinner, but
                # preserve the repository's silent-denial access-control policy.
                await query.answer()
            return None
        return await handler(update, context, *args, **kwargs)

    return wrapped


def mutation_source(update: Update) -> MutationSource | None:
    """Build a replay-idempotency source from the update that triggered a write.

    Returns ``None`` when the update carries no ``update_id`` (e.g. a synthetic
    test update or a programmatic call), so the mutation falls back to plain,
    non-idempotent behavior. Real Telegram updates always have a globally unique
    ``update_id``.
    """
    update_id = getattr(update, "update_id", None)
    if update_id is None:
        return None
    chat = getattr(update, "effective_chat", None)
    message = getattr(update, "effective_message", None)
    return MutationSource(
        update_id=update_id,
        chat_id=getattr(chat, "id", None),
        message_id=getattr(message, "message_id", None),
    )


# ---------------------------------------------------------------------------
# Input validators
# ---------------------------------------------------------------------------


def parse_int(
    text: str,
    field_name: str,
    *,
    max_value: int | None = None,
) -> tuple[int | None, str | None]:
    """Parse a positive integer from text.

    Returns (value, None) on success or (None, error_message) on failure.
    """
    try:
        val = int(text.strip())
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None, f"❌ Please enter a valid number for {field_name}."
    if val <= 0:
        return None, f"❌ {field_name} must be a positive number."
    if max_value is not None and val > max_value:
        return None, f"❌ {field_name} must be {max_value} or less."
    return val, None


def parse_float(
    text: str,
    field_name: str,
    *,
    max_value: float | None = None,
) -> tuple[float | None, str | None]:
    """Parse a positive float from text.

    Returns (value, None) on success or (None, error_message) on failure.
    """
    try:
        val = float(text.strip())
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None, f"❌ Please enter a valid number for {field_name}."
    if not math.isfinite(val):
        return None, f"❌ Please enter a finite number for {field_name}."
    if val <= 0:
        return None, f"❌ {field_name} must be a positive number."
    if max_value is not None and val > max_value:
        return None, f"❌ {field_name} must be {max_value:g} or less."
    return val, None


# ---------------------------------------------------------------------------
# /cancel — fallback for all ConversationHandlers
# ---------------------------------------------------------------------------
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the current conversation."""
    cleared = finish_conversation(update, context)
    text = "✖️ Cancelled." if cleared else "ℹ️ No active guided log in this chat."
    try:
        await update.message.reply_text(text)
    except TelegramError:
        logger.warning("Could not deliver conversation cancellation", exc_info=True)
    return ConversationHandler.END


cancel_handler = CommandHandler("cancel", cancel_command)


async def active_conversation_hint(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Explain why a guided command cannot re-enter its current conversation."""
    flow = active_conversation_flow(context)
    label = _CONVERSATION_LABELS.get(flow or "", "a guided log")
    await update.effective_message.reply_text(
        f"⏳ You're already in {label}. Finish it or use /cancel in this chat."
    )


# ---------------------------------------------------------------------------
# Shared per-state interceptors (Phase 1 routing matrix, plan §8.5/§8.6)
# ---------------------------------------------------------------------------
async def _safe_reply(update: Update, text: str) -> None:
    """Reply plainly, swallowing a transient send failure."""
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    try:
        await message.reply_text(text)
    except TelegramError:
        logger.warning("Could not deliver a routing hint", exc_info=True)


async def active_flow_control_interceptor(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """A Home control word arrived during an active flow: nudge, do not consume.

    Registered before a state's ordinary text handler so ``Repeat``/``Describe``/
    a greeting/``Home`` (and ``Meal`` outside Diet) can never become a subject,
    exercise, habit name, or food description. Returns ``None`` so PTB keeps the
    current conversation state and every draft key is untouched.
    """
    await _safe_reply(update, "⏳ Finish this flow or /cancel first.")
    return None


async def voice_not_enabled_interceptor(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Reject a voice note mid-flow without downloading it (plan §8.6).

    Never calls ``get_file`` or downloads content. Returns ``None`` to preserve
    the current state.
    """
    await _safe_reply(
        update, "🎤 Voice logging isn't enabled yet. Use the buttons or /cancel."
    )
    return None


async def buttons_or_cancel_catchall(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Absorb arbitrary text in a callback-only state so it never reaches Home."""
    await _safe_reply(update, "Use the buttons or /cancel.")
    return None


# ---------------------------------------------------------------------------
# Conversation timeout handler
# ---------------------------------------------------------------------------
async def timeout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Called when a conversation times out."""
    finish_conversation(update, context)
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⏰ Timed out. Send the command again to start over."
            )
        except TelegramError:
            logger.warning("Could not deliver conversation timeout", exc_info=True)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /undo — delete most recent log entry
# ---------------------------------------------------------------------------
# Compact single-character tokens keep undo callback data well under Telegram's
# 64-byte limit while still naming the exact source table.
_UNDO_TABLE_TOKENS = {"study_logs": "s", "gym_logs": "g", "diet_logs": "d"}
_UNDO_TOKEN_TABLES = {token: table for table, token in _UNDO_TABLE_TOKENS.items()}


def _undo_detail_lines(entry: dict[str, Any]) -> list[str]:
    """Describe an undoable entry: a bold category line plus its specifics."""
    category = _bounded_html(entry.get("category", "Unknown"), 64)
    lines = [f"<b>{category}</b>"]

    if "subject" in entry:
        subject = _bounded_html(entry["subject"], _MAX_UNDO_TEXT_LENGTH)
        duration = _bounded_html(entry["duration_min"], _MAX_UNDO_VALUE_LENGTH)
        lines.append(f"📖 {subject} — {duration} min")
    elif "exercise" in entry:
        exercise = _bounded_html(entry["exercise"], _MAX_UNDO_TEXT_LENGTH)
        sets = _bounded_html(entry["sets"], _MAX_UNDO_VALUE_LENGTH)
        reps = _bounded_html(entry["reps"], _MAX_UNDO_VALUE_LENGTH)
        w = (
            f" @ {_bounded_html(entry['weight_kg'], _MAX_UNDO_VALUE_LENGTH)}kg"
            if entry.get("weight_kg") is not None
            else " (bodyweight)"
        )
        lines.append(f"🏋️ {exercise} — {sets}×{reps}{w}")
    elif "food_items" in entry:
        meal_type = _bounded_html(entry["meal_type"], _MAX_UNDO_VALUE_LENGTH)
        food_items = _bounded_html(entry["food_items"], _MAX_UNDO_TEXT_LENGTH)
        cal = (
            f" — {_bounded_html(entry['calories'], _MAX_UNDO_VALUE_LENGTH)} cal"
            if entry.get("calories") is not None
            else ""
        )
        lines.append(f"🍽️ {meal_type}: {food_items}{cal}")
        macro_parts: list[str] = []
        for key, label in (
            ("protein_g", "P"),
            ("carbs_g", "C"),
            ("fat_g", "F"),
        ):
            value = entry.get(key)
            if value is None:
                continue
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                rendered = f"{float(value):g}"
            else:
                rendered = _bounded_html(value, _MAX_UNDO_VALUE_LENGTH)
            macro_parts.append(f"{label} {rendered}g")
        if macro_parts:
            lines.append(f"⚖️ {' · '.join(macro_parts)}")

    return lines


async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Preview the most recent log entry (within 24h) and ask to confirm.

    Nothing is deleted until the user taps Confirm, and the deletion targets
    that exact row — so a transient failure can never make a retry delete a
    different, newer entry.
    """
    if active_conversation_flow(context) is not None:
        await update.message.reply_text(
            "⏳ Finish the current guided log or use /cancel in the chat where "
            "it started before /undo."
        )
        return

    db = context.bot_data["db"]
    user_id = update.effective_user.id

    entry = await db.peek_last(user_id)
    if entry is None:
        await update.message.reply_text(
            "🤷 Nothing to undo — no entries in the last 24 hours."
        )
        return

    token = _UNDO_TABLE_TOKENS.get(entry.get("_table", ""))
    if token is None:
        # Defensive: an unrecognized source table should never offer a button
        # whose confirmation could not be routed.
        await update.message.reply_text("🤷 Nothing to undo right now.")
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Yes, undo",
                    callback_data=f"undo_do_{user_id}_{token}_{entry['id']}",
                ),
                InlineKeyboardButton("✖️ Keep", callback_data=f"undo_keep_{user_id}"),
            ]
        ]
    )
    text = "↩️ <b>Undo this entry?</b>\n\n" + "\n".join(_undo_detail_lines(entry))
    await reply_html(update.message, text, reply_markup=keyboard)


@authorized_callback
async def undo_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Delete the exact previewed entry; idempotent on repeated taps."""
    query = update.callback_query
    await query.answer()

    payload = (query.data or "").removeprefix("undo_do_")
    parts = payload.split("_")
    if len(parts) != 3:
        return
    owner_id, token, entry_id_raw = parts
    if str(update.effective_user.id) != owner_id:
        return
    table = _UNDO_TOKEN_TABLES.get(token)
    if table is None:
        return
    try:
        entry_id = int(entry_id_raw)
    except ValueError:
        return

    db = context.bot_data["db"]
    entry = await db.delete_log_by_id(update.effective_user.id, table, entry_id)
    if entry is None:
        text = "🤷 Already undone — nothing changed."
    else:
        text = "↩️ <b>Undone</b>\n\n" + "\n".join(_undo_detail_lines(entry))

    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
    except TelegramError:
        logger.warning("Could not update undo confirmation", exc_info=True)


@authorized_callback
async def undo_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Dismiss an undo prompt without deleting anything."""
    query = update.callback_query
    await query.answer()

    payload = (query.data or "").removeprefix("undo_keep_")
    if payload and str(update.effective_user.id) != payload:
        return

    try:
        await query.edit_message_text("✖️ Kept — nothing was undone.")
    except TelegramError:
        logger.warning("Could not update undo cancellation", exc_info=True)


# ---------------------------------------------------------------------------
# Global error handler
# ---------------------------------------------------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors and notify the user."""
    error = context.error
    logger.error(
        "Exception while handling an update",
        exc_info=(type(error), error, error.__traceback__),
    )

    # Notify user (if we have an update with a message). Never let the
    # notification itself raise — the same outage that caused the error can
    # also break this reply.
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. The error has been logged. "
                "Try again or use /cancel if you're stuck in a conversation."
            )
        except TelegramError:
            logger.warning("Could not deliver error notification", exc_info=True)
