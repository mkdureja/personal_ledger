"""
/supplements handler — Setup, daily adherence check-off, and yesterday toggle.

/supplements        → show today's adherence checklist
/supplements setup  → add/remove supplements

This module is a deliberate mirror of ``habits.py``. The two screens do the same
job — tap a name to record that you did the thing today — so they share an
interaction model, a date window, and an ownership-validation shape. What differs
is the data: a supplement carries an optional dose and timing label, and its
callback family is ``supp_*`` so no habit keyboard can ever route into a
supplement write.

**Adherence only.** Nothing here touches nutrition. A supplement has no calorie
or macro fields, is never read by meal resolution or diet analytics, and cannot
move a meal total.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

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
    paginate_habits,
    paginate_supplement_setup,
    supplement_checklist_keyboard,
    supplement_dose_label,
    supplement_progress_label,
    supplement_setup_keyboard,
    supplement_target,
    supplement_target_keyboard,
)
from ..database import (
    MAX_ACTIVE_SUPPLEMENTS,
    MAX_DOSE_AMOUNT,
    MAX_DOSE_TEXT_LENGTH,
    MAX_SUPPLEMENT_NAME_LENGTH,
    MAX_SUPPLEMENT_TARGET,
)
from ..config import CONVERSATION_TIMEOUT, today_local

logger = logging.getLogger(__name__)

# Conversation state for setup
ADDING_SUPPLEMENT = 0

_SUPP_ACTION_RE = re.compile(
    r"^supp_(c|u|m)_(\d+)_(\d+)_(\d{4}-\d{2}-\d{2})(?:_p(\d+))?$"
)
_SUPP_TOGGLE_RE = re.compile(r"^supp_toggle_(\d+)_(today|yesterday)(?:_p(\d+))?$")
_SUPP_PAGE_RE = re.compile(r"^supp_page_(\d+)_(\d{4}-\d{2}-\d{2})_(\d+)$")
_SUPP_NOOP_RE = re.compile(r"^supp_noop_(\d+)_(date|\d+)$")
_SUPP_REMOVE_RE = re.compile(r"^supp_remove_(\d+)_(\d+)(?:_p(\d+))?$")
_SUPP_SETUP_PAGE_RE = re.compile(r"^supp_setup_page_(\d+)_(\d+)$")
_SUPP_SETUP_DONE_RE = re.compile(r"^supp_setup_done_(\d+)$")
_SUPP_TARGET_RE = re.compile(r"^supp_tgt_(\d+)_(\d+)(?:_p(\d+))?$")
_SUPP_TARGET_SET_RE = re.compile(r"^supp_tset_(\d+)_(\d+)_(\d+)(?:_p(\d+))?$")
_SUPP_SETUP_PROMPT_KEY = "supplement_setup_prompt"

#: A leading positive number (``2``, ``0.5``, ``.5``; ``1,000`` is *not* accepted
#: — commas split fields) followed by the rest of the field as the unit.
_DOSE_RE = re.compile(r"^(\d+(?:\.\d+)?|\.\d+)\s*(.*)$")
#: Whether a field was *meant* as a dose. Anything opening with a sign or a digit
#: is a dose attempt, so ``-1 mg`` is rejected outright rather than quietly
#: reinterpreted as a timing label — guessing here would store nonsense as if the
#: user had asked for it.
_DOSE_ATTEMPT_RE = re.compile(r"^[+-]?\.?\d")


class SupplementInputError(ValueError):
    """A user-facing reason that typed supplement input was rejected."""


def parse_supplement_input(text: str) -> dict[str, object]:
    """Parse ``Name, dose, timing`` into validated supplement fields.

    Kept pure and separate from Telegram so the grammar can be tested directly.
    Supplement setup is a rare one-time action, unlike meal logging, so a short
    typed line is proportionate here — but it stays forgiving:

    * ``Vitamin D3`` → name only
    * ``Vitamin D3, 2 capsules`` → dose amount and unit
    * ``Vitamin D3, morning`` → a second field with no leading number is timing
    * ``Vitamin D3, 2 capsules, with breakfast`` → all three

    Raises :class:`SupplementInputError` with a message meant for the user.
    """
    parts = [part.strip() for part in str(text).split(",")]
    if len(parts) > 3:
        raise SupplementInputError(
            "Use at most: name, dose, timing — for example "
            "<i>Vitamin D3, 2 capsules, with breakfast</i>."
        )

    name = parts[0]
    if not name:
        raise SupplementInputError("Supplement name can't be empty.")
    if len(name) > MAX_SUPPLEMENT_NAME_LENGTH:
        raise SupplementInputError(
            f"Supplement name too long (max {MAX_SUPPLEMENT_NAME_LENGTH} chars)."
        )

    dose_field = ""
    timing = ""
    rest = [part for part in parts[1:] if part]
    if len(rest) == 1:
        # One extra field is a dose only when it looks like one, so
        # "Vitamin D3, morning" records timing rather than a nameless dose.
        if _DOSE_ATTEMPT_RE.match(rest[0]) is not None:
            dose_field = rest[0]
        else:
            timing = rest[0]
    elif len(rest) == 2:
        dose_field, timing = rest

    dose_amount: float | None = None
    dose_unit: str | None = None
    if dose_field:
        match = _DOSE_RE.match(dose_field)
        if match is None:
            raise SupplementInputError(
                "The dose should start with a number — for example "
                "<i>2 capsules</i> or <i>500 mg</i>."
            )
        try:
            dose_amount = float(match.group(1))
        except (TypeError, ValueError, OverflowError):
            raise SupplementInputError("That dose amount isn't a number.") from None
        if dose_amount <= 0 or dose_amount > MAX_DOSE_AMOUNT:
            raise SupplementInputError(
                f"The dose must be greater than 0 and at most {MAX_DOSE_AMOUNT:g}."
            )
        dose_unit = match.group(2).strip() or None
        if dose_unit and len(dose_unit) > MAX_DOSE_TEXT_LENGTH:
            raise SupplementInputError(
                f"The dose unit is too long (max {MAX_DOSE_TEXT_LENGTH} chars)."
            )

    if timing and len(timing) > MAX_DOSE_TEXT_LENGTH:
        raise SupplementInputError(
            f"The timing is too long (max {MAX_DOSE_TEXT_LENGTH} chars)."
        )

    return {
        "name": name,
        "dose_amount": dose_amount,
        "dose_unit": dose_unit,
        "timing": timing or None,
    }


def _message_location(message) -> tuple[int, int] | None:
    """Return a stable ``(chat_id, message_id)`` pair when available."""
    chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    if isinstance(chat_id, int) and isinstance(message_id, int):
        return chat_id, message_id
    return None


async def _retire_previous_setup_keyboard(context) -> None:
    """Best-effort retirement of the previously active setup keyboard."""
    previous = context.user_data.pop(_SUPP_SETUP_PROMPT_KEY, None)
    bot = getattr(context, "bot", None)
    if previous is None or bot is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=previous[0],
            message_id=previous[1],
            reply_markup=None,
        )
    except TelegramError:
        logger.debug("Could not retire previous supplement setup keyboard", exc_info=True)


def _is_current_setup_callback(update: Update, context) -> bool:
    """Validate that a setup callback belongs to the active prompt and chat."""
    return conversation_is_active(
        update, context, "supplements"
    ) and context.user_data.get(_SUPP_SETUP_PROMPT_KEY) == _message_location(
        update.callback_query.message
    )


# ---------------------------------------------------------------------------
# /supplements — entry point
# ---------------------------------------------------------------------------
async def supplements_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int | None:
    """Handle /supplements and /supplements setup."""
    db = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)

    args = context.args or []

    if args and args[0].lower() == "setup":
        if not await conversation_available(update, context, "supplements"):
            return ConversationHandler.END
        activate_conversation(update, context, "supplements")
        try:
            return await _show_setup(update.message, context, user.id)
        except BaseException:
            finish_conversation(update, context, "supplements")
            raise

    await show_supplements_checklist(update.message, context, user.id)
    return ConversationHandler.END


def _counts_for(
    supplements: list[dict], taken: set[int], counts: dict[int, int] | None
) -> dict[int, int]:
    """How many of each supplement stand today, from either read.

    ``taken`` alone still answers it for a plain check-off, so a caller that has
    only the set (an older keyboard path, a test) keeps working: a supplement in
    the set has one.
    """
    resolved = dict(counts or {})
    for item in supplements:
        sid = item["id"]
        if sid not in resolved:
            resolved[sid] = 1 if sid in taken else 0
    return resolved


def _is_complete(supplement: dict, count: int) -> bool:
    """Whether a day's count meets that supplement's daily target."""
    return count >= supplement_target(supplement)


def _checklist_text(
    supplements: list[dict],
    taken: set[int],
    page_note: str,
    counts: dict[int, int] | None = None,
) -> str:
    """Compose the checklist header. Adherence counts only — never nutrition.

    "Taken" means *finished*: a supplement aimed at twice a day with one scoop in
    it is not counted here, because the header exists to answer "what is still
    outstanding" and a half-done row is still outstanding.
    """
    resolved = _counts_for(supplements, taken, counts)
    done = sum(1 for item in supplements if _is_complete(item, resolved[item["id"]]))
    return (
        f"💊 <b>Supplements</b> — {done}/{len(supplements)} taken\n"
        f"Tap to check off:{page_note}"
    )


async def show_supplements_checklist(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    target_date=None,
) -> None:
    """Display the supplement checklist for a given date."""
    db = context.bot_data["db"]
    if target_date is None:
        target_date = today_local()

    supplements = await db.get_active_supplements(user_id)
    if not supplements:
        await reply_html(
            message,
            "💊 You have no supplements set up yet.\n"
            "Use /supplements setup to add some!",
        )
        return

    taken = await db.get_taken_supplements(user_id, target_date)
    counts = await db.get_supplement_counts(user_id, target_date)
    is_today = target_date == today_local()

    _page_items, current_page, page_count = paginate_habits(supplements)
    page_note = (
        f"\nShowing page {current_page + 1}/{page_count}." if page_count > 1 else ""
    )

    await reply_html(
        message,
        _checklist_text(supplements, taken, page_note, counts),
        reply_markup=supplement_checklist_keyboard(
            supplements, taken, target_date, user_id, is_today, counts=counts
        ),
    )


# ---------------------------------------------------------------------------
# Checklist callbacks (take/untake/toggle day)
# ---------------------------------------------------------------------------
@authorized_callback
async def supplement_take_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Record a supplement as taken — one more of it, for a counted one."""
    query = update.callback_query
    user_id = update.effective_user.id
    parsed = await _parse_supplement_action(query, user_id, "c")
    if parsed is None:
        return
    supplement_id, log_date, page = parsed

    db = context.bot_data["db"]
    supplement = await _active_supplement(db, user_id, supplement_id)
    if supplement is None:
        await _reject_callback(query, "This supplement is no longer active.")
        return

    target = supplement_target(supplement)
    if target > 1:
        count = await db.adjust_supplement_count(user_id, supplement_id, log_date, 1)
        # The count is answered on the tap as well as redrawn, because the
        # keyboard edit is the slower half and "did that register?" is exactly
        # the question a second, unwanted scoop gets tapped over.
        await query.answer(f"{supplement['name']}: {count}/{target}")
    else:
        await query.answer()
        await db.take_supplement(user_id, supplement_id, log_date)
    await _refresh_checklist(query, context, user_id, log_date, page)


@authorized_callback
async def supplement_decrement_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """➖ on a counted supplement: take one back off the day.

    Reaching zero removes the day's record entirely, so this is the exact
    inverse of the first tap rather than a stored zero.
    """
    query = update.callback_query
    user_id = update.effective_user.id
    parsed = await _parse_supplement_action(query, user_id, "m")
    if parsed is None:
        return
    supplement_id, log_date, page = parsed

    db = context.bot_data["db"]
    supplement = await _active_supplement(db, user_id, supplement_id)
    if supplement is None:
        await _reject_callback(query, "This supplement is no longer active.")
        return

    count = await db.adjust_supplement_count(user_id, supplement_id, log_date, -1)
    await query.answer(
        f"{supplement['name']}: {count}/{supplement_target(supplement)}"
    )
    await _refresh_checklist(query, context, user_id, log_date, page)


@authorized_callback
async def supplement_untake_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Remove a supplement adherence record."""
    query = update.callback_query
    user_id = update.effective_user.id
    parsed = await _parse_supplement_action(query, user_id, "u")
    if parsed is None:
        return
    supplement_id, log_date, page = parsed

    db = context.bot_data["db"]
    if not await _is_active_supplement(db, user_id, supplement_id):
        await _reject_callback(query, "This supplement is no longer active.")
        return

    await query.answer()
    await db.untake_supplement(user_id, supplement_id, log_date)
    await _refresh_checklist(query, context, user_id, log_date, page)


@authorized_callback
async def supplement_toggle_day_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Toggle between today and yesterday."""
    query = update.callback_query
    user_id = update.effective_user.id
    match = _SUPP_TOGGLE_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This supplement button is no longer valid.")
        return
    if int(match.group(1)) != user_id:
        await _reject_callback(
            query,
            "This supplement checklist belongs to another user.",
            clear_keyboard=False,
        )
        return

    if match.group(2) == "yesterday":
        target_date = today_local() - timedelta(days=1)
    else:
        target_date = today_local()
    page = int(match.group(3) or 0)

    await query.answer()
    await _refresh_checklist(query, context, user_id, target_date, page)


@authorized_callback
async def supplement_page_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Move between pages in an oversized supplement checklist."""
    query = update.callback_query
    user_id = update.effective_user.id
    match = _SUPP_PAGE_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This supplement page button is no longer valid.")
        return
    if int(match.group(1)) != user_id:
        await _reject_callback(
            query,
            "This supplement checklist belongs to another user.",
            clear_keyboard=False,
        )
        return

    try:
        target_date = date.fromisoformat(match.group(2))
    except ValueError:
        await _reject_callback(query, "This supplement page has an invalid date.")
        return
    today = today_local()
    if target_date not in {today, today - timedelta(days=1)}:
        await _reject_callback(
            query, "This checklist has expired. Open /supplements for a current one."
        )
        return

    await query.answer()
    await _refresh_checklist(query, context, user_id, target_date, int(match.group(3)))


@authorized_callback
async def supplement_noop_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Answer an inert label button without ever writing."""
    query = update.callback_query
    user_id = update.effective_user.id
    match = _SUPP_NOOP_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This supplement button is no longer valid.")
        return
    if int(match.group(1)) != user_id:
        await _reject_callback(
            query,
            "This supplement checklist belongs to another user.",
            clear_keyboard=False,
        )
        return

    target = match.group(2)
    if target != "date":
        if _is_current_setup_callback(update, context):
            await query.answer(
                "Supplement Setup: use ❌ Remove, or type a new supplement to add.",
                show_alert=True,
            )
            return
        db = context.bot_data["db"]
        if not await _is_active_supplement(db, user_id, int(target)):
            await _reject_callback(query, "This supplement is no longer active.")
            return
        await query.answer(
            "Send /supplements to refresh this checklist, then tap the name.",
            show_alert=True,
        )
        return

    await query.answer()


async def _parse_supplement_action(query, user_id: int, expected_action: str):
    """Validate an owned take/untake callback and its permitted date."""
    match = _SUPP_ACTION_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This supplement button is no longer valid.")
        return None

    if match.group(1) != expected_action:
        await _reject_callback(query, "This supplement button is no longer valid.")
        return None

    if int(match.group(2)) != user_id:
        await _reject_callback(
            query,
            "This supplement checklist belongs to another user.",
            clear_keyboard=False,
        )
        return None

    try:
        log_date = date.fromisoformat(match.group(4))
    except ValueError:
        await _reject_callback(query, "This supplement button has an invalid date.")
        return None

    # Same window as habits: today or yesterday only, so an old keyboard left in
    # a chat cannot silently backfill adherence for an arbitrary past date.
    today = today_local()
    if log_date not in {today, today - timedelta(days=1)}:
        await _reject_callback(
            query, "This checklist has expired. Open /supplements for a current one."
        )
        return None

    return int(match.group(3)), log_date, int(match.group(5) or 0)


async def _active_supplement(db, user_id: int, supplement_id: int) -> dict | None:
    """The requesting user's active supplement row, or ``None``.

    Read through ``get_active_supplements`` so ownership and activity are
    checked by exactly the query that draws the keyboard.
    """
    supplements = await db.get_active_supplements(user_id)
    return next(
        (item for item in supplements if item["id"] == supplement_id), None
    )


async def _is_active_supplement(db, user_id: int, supplement_id: int) -> bool:
    """Return whether a supplement is active and owned by the requesting user."""
    return await _active_supplement(db, user_id, supplement_id) is not None


async def _reject_callback(query, text: str, *, clear_keyboard: bool = True) -> None:
    """Acknowledge an invalid callback and retire its stale keyboard."""
    try:
        await query.answer(text, show_alert=True)
    except TelegramError:
        logger.debug("Could not answer stale supplement callback", exc_info=True)

    if clear_keyboard:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            logger.debug("Could not remove stale supplement keyboard", exc_info=True)


async def _refresh_checklist(
    query, context, user_id: int, target_date, page: int = 0
) -> None:
    """Re-render the checklist in-place after a change."""
    db = context.bot_data["db"]
    supplements = await db.get_active_supplements(user_id)
    taken = await db.get_taken_supplements(user_id, target_date)
    counts = await db.get_supplement_counts(user_id, target_date)
    is_today = target_date == today_local()

    _page_items, current_page, page_count = paginate_habits(supplements, page)
    page_note = (
        f"\nShowing page {current_page + 1}/{page_count}." if page_count > 1 else ""
    )

    try:
        await query.edit_message_text(
            _checklist_text(supplements, taken, page_note, counts),
            reply_markup=supplement_checklist_keyboard(
                supplements,
                taken,
                target_date,
                user_id,
                is_today,
                current_page,
                counts=counts,
            ),
            parse_mode="HTML",
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
        logger.debug("Supplement checklist was already up to date")


# ---------------------------------------------------------------------------
# Setup flow — add/remove supplements
# ---------------------------------------------------------------------------
def _setup_view(supplements: list[dict], user_id: int, page: int = 0):
    """Build bounded setup text and keyboard.

    Paginated by the *setup* page size, not the checklist's: this screen draws
    three buttons per supplement, so the two disagree about how many fit — and a
    page note that names a page the keyboard is not showing is worse than none.
    """
    _page_items, current_page, page_count = paginate_supplement_setup(
        supplements, page
    )
    if supplements:
        at_limit = len(supplements) >= MAX_ACTIVE_SUPPLEMENTS
        limit_note = (
            f"\nMaximum reached ({MAX_ACTIVE_SUPPLEMENTS}); "
            "remove supplements before adding."
            if at_limit
            else ""
        )
        page_note = (
            f"\nShowing page {current_page + 1}/{page_count}."
            if page_count > 1
            else ""
        )
        count = len(supplements)
        text = (
            "⚙️ <b>Supplement Setup</b>\n\n"
            f"You have {count} active supplement{'s' if count != 1 else ''}.\n"
            "Tap 🎯 to set a daily target (e.g. 2 scoops a day), ❌ to remove, "
            "or type a new one to add:"
            f"{limit_note}{page_note}"
        )
    else:
        text = (
            "⚙️ <b>Supplement Setup</b>\n\n"
            "You have no supplements yet. Type one to add it:\n"
            "<i>Vitamin D3, 2 capsules, with breakfast</i>\n\n"
            "Dose and timing are optional — <i>Magnesium</i> works too.\n"
            "Use /cancel when done."
        )
    return text, supplement_setup_keyboard(supplements, user_id, current_page)


async def _show_setup(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    page: int = 0,
) -> int:
    """Show current supplements with remove buttons and prompt to add."""
    db = context.bot_data["db"]
    supplements = await db.get_active_supplements(user_id)
    await _retire_previous_setup_keyboard(context)

    text, keyboard = _setup_view(supplements, user_id, page)
    prompt = await reply_html(message, text, reply_markup=keyboard)

    location = _message_location(prompt)
    if location is not None:
        context.user_data[_SUPP_SETUP_PROMPT_KEY] = location

    return ADDING_SUPPLEMENT


async def add_supplement_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Add a supplement from a typed ``name, dose, timing`` line."""
    raw = update.message.text.strip()
    if not raw:
        await update.message.reply_text("❌ Supplement name can't be empty.")
        return ADDING_SUPPLEMENT

    try:
        fields = parse_supplement_input(raw)
    except SupplementInputError as exc:
        await reply_html(update.message, f"❌ {exc}")
        return ADDING_SUPPLEMENT

    db = context.bot_data["db"]
    user_id = update.effective_user.id

    active = await db.get_active_supplements(user_id)
    if len(active) >= MAX_ACTIVE_SUPPLEMENTS:
        await update.message.reply_text(
            f"❌ You can have at most {MAX_ACTIVE_SUPPLEMENTS} active supplements. "
            "Remove one before adding another."
        )
        return ADDING_SUPPLEMENT

    _supplement_id, status = await db.add_supplement(
        user_id,
        fields["name"],
        dose_amount=fields["dose_amount"],
        dose_unit=fields["dose_unit"],
        timing=fields["timing"],
    )

    safe_name = escape_html(str(fields["name"]))
    detail = escape_html(supplement_dose_label(fields))
    if status == "reactivated":
        await reply_html(update.message, f"♻️ Reactivated: <b>{safe_name}</b>{detail}")
    elif status == "already_active":
        await reply_html(update.message, f"ℹ️ Already active: <b>{safe_name}</b>")
    else:
        await reply_html(update.message, f"✅ Added: <b>{safe_name}</b>{detail}")

    return await _show_setup(update.message, context, user_id)


@authorized_callback
async def remove_supplement_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Remove (deactivate) a supplement, preserving its adherence history."""
    query = update.callback_query
    user_id = update.effective_user.id
    match = _SUPP_REMOVE_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This remove button is no longer valid.")
        return
    if int(match.group(1)) != user_id:
        await _reject_callback(
            query, "This supplement setup belongs to another user.", clear_keyboard=False
        )
        return
    if not _is_current_setup_callback(update, context):
        await _reject_callback(query, "This supplement setup has expired.")
        return
    supplement_id = int(match.group(2))
    page = int(match.group(3) or 0)

    db = context.bot_data["db"]
    if not await _is_active_supplement(db, user_id, supplement_id):
        await _reject_callback(query, "This supplement is no longer active.")
        return

    if not await db.deactivate_supplement(user_id, supplement_id):
        await _reject_callback(query, "This supplement is no longer active.")
        return

    await query.answer()
    supplements = await db.get_active_supplements(user_id)
    text, keyboard = _setup_view(supplements, user_id, page)
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")


def _target_text(supplement: dict) -> str:
    """The target picker's prompt for one supplement."""
    target = supplement_target(supplement)
    current = (
        f"Currently <b>{target}× a day</b>."
        if target > 1
        else "Currently a plain check-off — one tap finishes the day."
    )
    return (
        f"🎯 <b>{escape_html(str(supplement['name']))}</b> — how many a day?\n\n"
        f"{current}\n"
        "With a target above one, its checklist row counts up "
        "(<i>1/2</i>, <i>2/2</i>) and the day only counts as taken once you "
        "reach it. ➖ takes one back off."
    )


@authorized_callback
async def supplement_target_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """🎯 in setup: offer the daily targets for one supplement."""
    query = update.callback_query
    user_id = update.effective_user.id
    match = _SUPP_TARGET_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This target button is no longer valid.")
        return ADDING_SUPPLEMENT
    if int(match.group(1)) != user_id:
        await _reject_callback(
            query, "This supplement setup belongs to another user.", clear_keyboard=False
        )
        return ADDING_SUPPLEMENT
    if not _is_current_setup_callback(update, context):
        await _reject_callback(query, "This supplement setup has expired.")
        if conversation_is_active(update, context, "supplements"):
            return ADDING_SUPPLEMENT
        return ConversationHandler.END

    db = context.bot_data["db"]
    supplement = await _active_supplement(db, user_id, int(match.group(2)))
    if supplement is None:
        await _reject_callback(query, "This supplement is no longer active.")
        return ADDING_SUPPLEMENT

    page = int(match.group(3) or 0)
    await query.answer()
    await query.edit_message_text(
        _target_text(supplement),
        reply_markup=supplement_target_keyboard(supplement, user_id, page),
        parse_mode="HTML",
    )
    return ADDING_SUPPLEMENT


@authorized_callback
async def supplement_set_target_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Store a supplement's daily target and return to setup."""
    query = update.callback_query
    user_id = update.effective_user.id
    match = _SUPP_TARGET_SET_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This target button is no longer valid.")
        return ADDING_SUPPLEMENT
    if int(match.group(1)) != user_id:
        await _reject_callback(
            query, "This supplement setup belongs to another user.", clear_keyboard=False
        )
        return ADDING_SUPPLEMENT
    if not _is_current_setup_callback(update, context):
        await _reject_callback(query, "This supplement setup has expired.")
        if conversation_is_active(update, context, "supplements"):
            return ADDING_SUPPLEMENT
        return ConversationHandler.END

    supplement_id = int(match.group(2))
    target = int(match.group(3))
    page = int(match.group(4) or 0)
    if target < 1 or target > MAX_SUPPLEMENT_TARGET:
        await _reject_callback(query, "That daily target isn't available.")
        return ADDING_SUPPLEMENT

    db = context.bot_data["db"]
    supplement = await _active_supplement(db, user_id, supplement_id)
    if supplement is None:
        await _reject_callback(query, "This supplement is no longer active.")
        return ADDING_SUPPLEMENT

    # 1× is stored as "no target" rather than as the number one: the two behave
    # identically everywhere, and the absent value is what every untouched
    # supplement already carries.
    await db.set_supplement_target(user_id, supplement_id, target if target > 1 else None)
    await query.answer(
        f"{supplement['name']}: {target}× a day"
        if target > 1
        else f"{supplement['name']}: plain check-off"
    )

    supplements = await db.get_active_supplements(user_id)
    text, keyboard = _setup_view(supplements, user_id, page)
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    return ADDING_SUPPLEMENT


@authorized_callback
async def supplement_setup_page_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Move between pages of supplements in the active setup prompt."""
    query = update.callback_query
    user_id = update.effective_user.id
    match = _SUPP_SETUP_PAGE_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This setup page button is no longer valid.")
        if conversation_is_active(update, context, "supplements"):
            return ADDING_SUPPLEMENT
        return ConversationHandler.END
    if int(match.group(1)) != user_id:
        await _reject_callback(
            query, "This supplement setup belongs to another user.", clear_keyboard=False
        )
        return ADDING_SUPPLEMENT
    if not _is_current_setup_callback(update, context):
        await _reject_callback(query, "This supplement setup has expired.")
        if conversation_is_active(update, context, "supplements"):
            return ADDING_SUPPLEMENT
        return ConversationHandler.END

    supplements = await context.bot_data["db"].get_active_supplements(user_id)
    text, keyboard = _setup_view(supplements, user_id, int(match.group(2)))
    await query.answer()
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    return ADDING_SUPPLEMENT


@authorized_callback
async def supplement_setup_done_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Close supplement setup and show a fresh checklist."""
    query = update.callback_query
    user_id = update.effective_user.id
    match = _SUPP_SETUP_DONE_RE.fullmatch(query.data or "")
    if match is None:
        await _reject_callback(query, "This setup button is no longer valid.")
        if conversation_is_active(update, context, "supplements"):
            return ADDING_SUPPLEMENT
        return ConversationHandler.END
    if int(match.group(1)) != user_id:
        await _reject_callback(
            query, "This supplement setup belongs to another user.", clear_keyboard=False
        )
        return ADDING_SUPPLEMENT
    if not _is_current_setup_callback(update, context):
        await _reject_callback(query, "This supplement setup has expired.")
        if conversation_is_active(update, context, "supplements"):
            return ADDING_SUPPLEMENT
        return ConversationHandler.END

    await query.answer()
    context.user_data.pop(_SUPP_SETUP_PROMPT_KEY, None)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        logger.debug("Could not retire supplement setup keyboard", exc_info=True)
    try:
        await show_supplements_checklist(query.message, context, user_id)
    except TelegramError:
        logger.warning("Could not deliver supplement checklist", exc_info=True)
    finish_conversation(update, context, "supplements")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler for setup
# ---------------------------------------------------------------------------
_voice_guard = MessageHandler(filters.VOICE, voice_mid_flow_interceptor)
_control_guard = MessageHandler(ACTIVE_CONTROL_FILTER, active_flow_control_interceptor)

supplements_setup_conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("supplements", supplements_command, filters=AUTH_FILTER)
    ],
    states={
        ADDING_SUPPLEMENT: [
            _voice_guard,
            CallbackQueryHandler(
                supplement_setup_done_callback, pattern=r"^supp_setup_done_"
            ),
            CallbackQueryHandler(
                remove_supplement_callback, pattern=r"^supp_remove_"
            ),
            CallbackQueryHandler(
                supplement_setup_page_callback, pattern=r"^supp_setup_page_"
            ),
            # Both target taps stay inside setup: they edit the *intention*, and
            # the prompt-location check they share with ❌ Remove is what keeps a
            # stale setup screen from rewriting a target later.
            CallbackQueryHandler(
                supplement_set_target_callback, pattern=r"^supp_tset_"
            ),
            CallbackQueryHandler(
                supplement_target_callback, pattern=r"^supp_tgt_"
            ),
            _control_guard,
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_supplement_text),
        ],
        ConversationHandler.TIMEOUT: [TypeHandler(Update, timeout_handler)],
    },
    fallbacks=[
        cancel_handler,
        CommandHandler("supplements", active_conversation_hint, filters=AUTH_FILTER),
        # Home is always reachable: it ends this flow and reports anything
        # unsaved. Must be a fallback — a handler outside the conversation
        # cannot return END into it, so the state would linger and swallow
        # the next ordinary message.
        *home_fallback_handlers(),
    ],
    conversation_timeout=CONVERSATION_TIMEOUT,
)
