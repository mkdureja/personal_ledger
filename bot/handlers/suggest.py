"""``/suggest`` — tell the app what it should do differently.

Every other command in this bot writes down something about the person using
it. This one writes down something about the *bot*, and that difference runs
through the whole design:

* **It is not a log entry.** A suggestion changes no total, streak, or chart,
  and ``/undo`` deliberately does not reach it. Nothing here can alter what a
  day looks like in the ledger.
* **The words are kept exactly as sent.** No parsing, no categories, no
  required format — deciding the shape in advance would decide which kinds of
  complaint are expressible, and the useful ones are usually the awkward ones.
* **Everything after the command is the suggestion**, including a word like
  "list" that another design would have claimed as a subcommand. A capture
  command that silently eats one phrasing is worse than one with no shortcuts
  at all, so the read-back lives on the bare ``/suggest`` prompt instead.

``/suggest <text>`` files it in one message. Bare ``/suggest`` shows what this
user has already sent, then waits for the next message — which is the form that
matters on a phone, where typing the command and the thought together is the
awkward part.
"""

from __future__ import annotations

import logging
from datetime import datetime

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

from ..config import CONVERSATION_TIMEOUT, localize
from ..database import MAX_SUGGESTION_LENGTH
from ..keyboards import (
    app_suggestion_receipt_keyboard,
    parse_app_suggestion_remove,
)
from .common import (
    ACTIVE_CONTROL_FILTER,
    AUTH_FILTER,
    activate_conversation,
    active_flow_control_interceptor,
    authorized_callback,
    cancel_handler,
    conversation_available,
    escape_html,
    finish_conversation,
    mutation_source,
    reply_html,
    timeout_handler,
    voice_mid_flow_interceptor,
)
from .home import home_fallback_handlers

logger = logging.getLogger(__name__)

SAY = 0

#: How many of a user's own suggestions the prompt shows back. Enough to answer
#: "did I already say this?" without turning the prompt into an archive.
_RECENT_SHOWN = 3

#: Each of those is trimmed to a recognizable opening line rather than repeated
#: in full — the prompt is asking for the next one, not re-reading the old ones.
_RECENT_PREVIEW_CHARS = 80

_LENGTH_HINT = (
    f"Keep it under {MAX_SUGGESTION_LENGTH} characters — if there is more to "
    "say, send it as a second suggestion."
)


def _preview(text: str, limit: int = _RECENT_PREVIEW_CHARS) -> str:
    """Trim a suggestion to a single readable line."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


def _parse_utc(value: object) -> datetime | None:
    """Parse a stored timestamp (with or without microseconds) as naive UTC."""
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt)
        except (ValueError, TypeError):
            continue
    return None


def _when(row: dict) -> str:
    parsed = _parse_utc(row.get("created_at"))
    return localize(parsed).strftime("%d %b") if parsed else "?"


# ---------------------------------------------------------------------------
# Writing one down
# ---------------------------------------------------------------------------
async def _file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message,
    text: str,
    *,
    retry_state: int = SAY,
) -> int:
    """Store one suggestion and confirm it, or explain why it was refused.

    ``retry_state`` is where a refusal leaves the user: ``SAY`` inside the
    guided flow, where the prompt is still on screen, and ``END`` for the
    one-shot ``/suggest <text>`` form, which opened no conversation and must not
    leave PTB in a state the rest of the app does not believe is active.
    """
    db = context.bot_data["db"]
    user = update.effective_user
    suggestion = (text or "").strip()

    if not suggestion:
        await reply_html(
            message,
            "💡 Tell me what to change — for example "
            "<code>/suggest let me log water</code>.",
        )
        return retry_state
    if len(suggestion) > MAX_SUGGESTION_LENGTH:
        await reply_html(
            message,
            f"❌ That is {len(suggestion)} characters. {_LENGTH_HINT}",
        )
        return retry_state

    try:
        await db.ensure_user(user.id, user.username, user.first_name)
        suggestion_id = await db.add_app_suggestion(
            user.id, suggestion, source=mutation_source(update)
        )
    except ValueError:
        # The write-path backstop refusing what the checks above should have
        # caught. Report it as a refusal rather than as a failure.
        await reply_html(message, f"❌ {_LENGTH_HINT}")
        return retry_state
    except Exception:
        # Nothing was committed. Say so plainly instead of leaving the user to
        # guess whether to send it again. The log carries no suggestion text and
        # no Telegram ID.
        logger.exception("Suggestion not saved; nothing was written")
        await reply_html(message, "⚠️ Couldn't save that. Try again.")
        return retry_state

    finish_conversation(update, context, "suggest")
    await reply_html(
        message,
        "💡 <b>Noted.</b>\n"
        f"<i>{escape_html(suggestion)}</i>\n\n"
        "It goes to whoever maintains this bot. Nothing in your ledger changed.",
        reply_markup=app_suggestion_receipt_keyboard(user.id, suggestion_id),
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
async def _prompt(message, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    """Ask for a suggestion, after showing what this user already sent."""
    db = context.bot_data["db"]
    recent = await db.get_app_suggestions(user_id, limit=_RECENT_SHOWN)
    total = await db.count_app_suggestions(user_id)

    lines = ["💡 <b>Suggest something</b>", ""]
    if recent:
        older = total - len(recent)
        heading = "You've suggested:" if not older else f"Your latest of {total}:"
        lines.append(heading)
        lines.extend(
            f"• <i>{escape_html(_preview(row['suggestion']))}</i> "
            f"<code>{_when(row)}</code>"
            for row in recent
        )
        lines.append("")
    lines.append(
        "Send your idea as one message — anything that would make this bot "
        "easier to live with. /cancel if you've changed your mind."
    )

    await reply_html(message, "\n".join(lines))
    return SAY


async def suggest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """``/suggest <text>`` files it now; bare ``/suggest`` asks for it.

    The one-shot form deliberately opens no conversation: a complete thought
    needs no follow-up state, and leaving one behind would swallow the next
    ordinary message.
    """
    db = context.bot_data["db"]
    user = update.effective_user
    message = update.effective_message

    # Everything after the command is the suggestion, kept as typed.
    # ``context.args`` splits on whitespace and would silently reflow a
    # multi-line thought into one line, so the raw text is used and only the
    # command word removed. Splitting on any whitespace (not just a space) also
    # catches the newline a desktop user gets from shift+enter.
    parts = (message.text or "").split(maxsplit=1)
    remainder = parts[1] if len(parts) > 1 else ""
    if remainder.strip():
        return await _file(
            update,
            context,
            message,
            remainder,
            retry_state=ConversationHandler.END,
        )

    await db.ensure_user(user.id, user.username, user.first_name)
    if not await conversation_available(update, context, "suggest"):
        return ConversationHandler.END

    activate_conversation(update, context, "suggest")
    try:
        return await _prompt(message, context, user.id)
    except BaseException:
        finish_conversation(update, context, "suggest")
        raise


async def receive_suggestion(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """The message sent in answer to the prompt."""
    message = update.effective_message
    return await _file(update, context, message, message.text or "")


# ---------------------------------------------------------------------------
# Withdrawing one
# ---------------------------------------------------------------------------
@authorized_callback
async def withdraw_suggestion_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """🗑 Withdraw on a suggestion receipt.

    Registered outside every conversation, like targeted meal Undo: the receipt
    stays in the scrollback long after the flow ended, and a button that goes
    inert once the conversation closes is a button that looks broken. There is
    no time limit — a suggestion is not ledger history, so withdrawing one
    rewrites nothing.
    """
    query = update.callback_query
    user_id = update.effective_user.id
    suggestion_id = parse_app_suggestion_remove(query.data or "", user_id)
    if suggestion_id is None:
        await query.answer(
            "That button belongs to another user or is no longer valid.",
            show_alert=True,
        )
        return

    db = context.bot_data["db"]
    removed = await db.delete_app_suggestion(user_id, suggestion_id)
    await query.answer("Withdrawn." if removed else "Already withdrawn.")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        logger.debug("Could not retire a suggestion receipt", exc_info=True)
    await reply_html(
        query.message,
        "🗑 Suggestion withdrawn." if removed else "🗑 That suggestion is already gone.",
    )


_voice_guard = MessageHandler(filters.VOICE, voice_mid_flow_interceptor)
_control_guard = MessageHandler(ACTIVE_CONTROL_FILTER, active_flow_control_interceptor)
#: Matches ``sug_x_<user>_<id>``. Ownership is checked when decoding; the pattern
#: only keeps foreign callback families out.
_PATTERN = r"^sug_x_\d+_\d+$"

suggest_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("suggest", suggest_command, filters=AUTH_FILTER)],
    states={
        SAY: [
            _voice_guard,
            _control_guard,
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_suggestion),
        ],
        ConversationHandler.TIMEOUT: [TypeHandler(Update, timeout_handler)],
    },
    fallbacks=[
        cancel_handler,
        # Not the usual "you're already in a flow" nudge: ``/suggest <text>``
        # means the same thing at the prompt as it does from idle, and the one
        # promise this command makes is that text after it is filed. Re-entering
        # the same handler files it and closes the flow; a bare ``/suggest``
        # simply re-renders the prompt.
        CommandHandler("suggest", suggest_command, filters=AUTH_FILTER),
        *home_fallback_handlers(),
    ],
    conversation_timeout=CONVERSATION_TIMEOUT,
    per_message=False,
)

#: The withdraw button, for registration outside the conversation.
withdraw_suggestion_handler = CallbackQueryHandler(
    withdraw_suggestion_callback, pattern=_PATTERN
)
