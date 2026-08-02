"""Phase 1 Home surface: the greeting snapshot, the late text/voice routers, and
``/keyboard hide|show``.

Everything here is private-chat + allowlist gated and, crucially, checks
``phase1_enabled_for`` before any DB call. With Phase 1 disabled (the Release A
production configuration) these handlers expose no snapshot and perform no
mutation — greetings/Home/actions get plain ``/menu`` compatibility guidance and
a persistent-keyboard removal, and arbitrary text gets no Phase 1 surface at all
(plan §8.3 / §8.6).
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from .. import config
from ..config import (
    home_keyboard_action_for,
    phase1_enabled_for,
    today_local,
)
from ..keyboards import (
    MEAL_BUTTON_LABEL,
    REPEAT_BUTTON_LABEL,
    home_reply_keyboard,
    main_menu_keyboard,
    reply_keyboard_remove,
)
from ..meal_models import RepeatStatus
from .common import (
    GREETINGS,
    HOME_ACTIONS,
    HOME_WORDS,
    active_conversation_flow,
    escape_html,
    mutation_source,
    normalize_control_text,
    reply_html,
)
from .receipts import send_meal_receipt

logger = logging.getLogger(__name__)

_MENU_GUIDANCE = "ℹ️ Use /menu to open the main menu."
_ACTIVE_FLOW_HINT = "⏳ Finish this flow or /cancel first."
_UNSUPPORTED_TEXT = (
    "🤔 I didn't recognize that. Pick an action below, say <b>hi</b> for today's "
    "totals, or send /help for everything."
)


async def _unsupported_text_reply(update: Update) -> None:
    """Answer unrecognized idle text once, carrying the Home actions.

    Silence leaves a user wondering whether the bot is alive; a full Home refresh
    for a typo is two messages of noise. One bounded reply with the buttons
    attached is the recovery path.
    """
    await reply_html(
        update.effective_message,
        _UNSUPPORTED_TEXT,
        reply_markup=main_menu_keyboard(),
    )


def _keyboard_markup(user_id: int):
    """The persistent keyboard this user should see now, or a removal."""
    if home_keyboard_action_for(user_id) == "send":
        return home_reply_keyboard()
    return reply_keyboard_remove()


async def _sync_keyboard(update: Update, text: str, user_id: int) -> None:
    """Send ``text`` carrying either the eligible keyboard or a removal."""
    await update.effective_message.reply_text(
        text, reply_markup=_keyboard_markup(user_id)
    )


async def _remove_keyboard(update: Update, text: str) -> None:
    """Send ``text`` that always removes any stale persistent keyboard."""
    await update.effective_message.reply_text(
        text, reply_markup=reply_keyboard_remove()
    )


# ---------------------------------------------------------------------------
# Home snapshot
# ---------------------------------------------------------------------------
# Bound the meal description shown on Home. Home is a navigation surface, not a
# report: a long multi-item meal must still fit on one readable line.
_LAST_MEAL_SUMMARY_CHARS = 60


def _last_meal_line(summary: dict | None) -> str | None:
    """One bounded line naming the meal Repeat would re-log, or ``None``.

    Kept honest: the caller reads the same row Repeat copies, so this never
    advertises a meal the button would not log.
    """
    if not summary:
        return None
    description = str(summary.get("food_items") or "").strip()
    if not description:
        return None
    if len(description) > _LAST_MEAL_SUMMARY_CHARS:
        description = description[: _LAST_MEAL_SUMMARY_CHARS - 1] + "…"
    meal_type = str(summary.get("meal_type") or "meal")
    return (
        f"🔁 Repeat last meal: <b>{escape_html(description)}</b> "
        f"({escape_html(meal_type)})"
    )


async def show_home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    prelude: str | None = None,
) -> None:
    """Render today's cross-section snapshot plus the quick-action bar.

    This is *the* idle surface: ``/start``, ``/home``, ``/menu``, and a supported
    greeting all land here, so a user never has to remember which entry point
    shows the buttons. ``prelude`` carries the one-time ``/start`` welcome, which
    is prepended rather than sent separately so the buttons still arrive
    immediately.

    Sends two messages: the snapshot with the inline Home actions, then a short
    quick-action line carrying the reply keyboard (or its removal). Inline and
    reply keyboards are never combined on one message (plan §8.3).
    """
    db = context.bot_data["db"]
    user = update.effective_user
    uid = user.id
    today = today_local()

    meal_count = await db.get_today_meal_count(uid, today)
    calories, incomplete = await db.get_today_calories(uid, today)
    study_min = await db.get_today_study_total(uid, today)
    gym_count = await db.get_today_gym_count(uid, today)
    active_habits = await db.get_active_habits(uid)
    checked = await db.get_checked_habits(uid, today)
    checked_count = sum(1 for h in active_habits if h["id"] in checked)

    first_name = escape_html(user.first_name or "there")
    cal_suffix = " (some incomplete)" if incomplete else ""
    lines = [
        f"👋 <b>{first_name}</b> — here's today:",
        "",
        f"🍽️ Diet: {meal_count} meal(s), {calories} cal{cal_suffix}",
        f"📖 Study: {study_min} min",
        f"🏋️ Gym: {gym_count} exercise(s)",
        f"✅ Habits: {checked_count}/{len(active_habits)} done",
    ]

    # Name the meal Repeat would re-log, but only when Repeat is actually
    # available to this user — otherwise Home would describe an action they have
    # no button for.
    if phase1_enabled_for(uid):
        last_meal = _last_meal_line(await db.get_last_meal_summary(uid))
        if last_meal:
            lines.extend(["", last_meal])

    text = "\n".join(lines)
    if prelude:
        text = f"{prelude}\n\n{text}"
    await reply_html(update.effective_message, text, reply_markup=main_menu_keyboard())

    # The persistent quick-action bar is a Phase 1 (B) extra, not part of Home
    # itself. Send it only to keyboard-eligible users; send an explicit removal
    # only during a rollback (mode "remove"); otherwise say nothing — a plain
    # dark Home is just the snapshot + inline menu above, with no keyboard noise.
    if home_keyboard_action_for(uid) == "send":
        await update.effective_message.reply_text(
            f"Tap 🍽️ <b>{MEAL_BUTTON_LABEL}</b> to log, "
            f"or 🔁 <b>{REPEAT_BUTTON_LABEL}</b>.",
            parse_mode="HTML",
            reply_markup=home_reply_keyboard(),
        )
    elif config.HOME_KEYBOARD_MODE == "remove":
        await update.effective_message.reply_text(
            "Quick-action bar is off.",
            reply_markup=reply_keyboard_remove(),
        )


async def open_home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    prelude: str | None = None,
) -> None:
    """Open Home, or preserve a live guided flow and return its hint.

    Every idle entry point routes through here, so none of them can silently
    replace or end a draft the user is in the middle of.
    """
    if active_conversation_flow(context) is not None:
        await update.effective_message.reply_text(_ACTIVE_FLOW_HINT)
        return
    await show_home(update, context, prelude=prelude)


async def home_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/home`` — Today and the action buttons."""
    await open_home(update, context)


# ---------------------------------------------------------------------------
# Repeat — the one-tap exact re-log
# ---------------------------------------------------------------------------
async def repeat_last_meal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-log the user's most recent meal exactly, then show its receipt.

    No confirmation and no live re-resolution: what was logged before is what is
    logged again (plan §10.2). Callers have already established that no guided
    flow is active and that Phase 1 is enabled; both are re-checked here because
    this is the last point before a ledger write.
    """
    message = update.effective_message
    user = update.effective_user
    if active_conversation_flow(context) is not None:
        await message.reply_text(_ACTIVE_FLOW_HINT)
        return
    if not phase1_enabled_for(user.id):
        await _sync_keyboard(update, _MENU_GUIDANCE, user.id)
        return

    db = context.bot_data["db"]
    try:
        await db.ensure_user(user.id, user.username, user.first_name)
        result = await db.repeat_last_meal(user.id, mutation_source(update))
    except Exception:
        # A failed tap must read as a failed tap. Report it in bounded terms and
        # let the error handler log the detail; nothing was committed. The log
        # line carries no Telegram ID — the traceback identifies the fault, and
        # naming the user would publish their identity into service logs.
        logger.exception("Repeat failed; nothing was written")
        await message.reply_text("⚠️ Couldn't repeat that meal. Try again.")
        return

    if result.status is RepeatStatus.EMPTY:
        await _sync_keyboard(
            update, "🔁 Nothing to repeat yet — log a meal first.", user.id
        )
        return
    if result.status is RepeatStatus.REPLAYED_REMOVED:
        await _sync_keyboard(
            update,
            "↩️ That repeated meal was already undone; nothing changed.",
            user.id,
        )
        return

    headline = (
        "🔁 <b>Repeated</b>"
        if result.status is RepeatStatus.CREATED
        else "🔁 <b>Already repeated</b>"
    )
    await send_meal_receipt(message, result.receipt, headline)


# ---------------------------------------------------------------------------
# Late text router (registered after every ConversationHandler + command)
# ---------------------------------------------------------------------------
async def home_text_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route greetings / Home / Home-action text that no conversation consumed."""
    message = update.effective_message
    text = getattr(message, "text", None)
    if not text:
        return

    # 1. Defense in depth: a state handler should already have consumed control
    # text during an active flow. If one leaks here, nudge and never mutate.
    if active_conversation_flow(context) is not None:
        await message.reply_text(_ACTIVE_FLOW_HINT)
        return

    normalized = normalize_control_text(text)

    # 2. Home is the app's home page — a greeting or "home" always opens it (some
    # text + the main menu), for every authorized user, regardless of the Phase 1
    # flag. The flag only governs the B quick-action bar inside show_home.
    if normalized in GREETINGS or normalized in HOME_WORDS:
        await show_home(update, context)
        return

    # 3. Meal/Repeat/Describe are the Phase 1 (B) fast actions, matched through
    # HOME_ACTIONS so the current and legacy Repeat labels share one branch.
    # ("Meal" text is normally claimed by the Diet entry point before it reaches
    # here.)
    uid = update.effective_user.id
    action = HOME_ACTIONS.get(normalized)
    if action is None:
        # 4. Unsupported idle text, in either flag state: one concise recovery
        # reply carrying the Home actions, rather than silence or a separate
        # multi-message Home refresh. No snapshot query, no mutation, and no
        # persistent keyboard — so this stays safe with Phase 1 disabled.
        await _unsupported_text_reply(update)
        return
    if not phase1_enabled_for(uid):
        # The text may have come from a persistent keyboard sent by an older
        # build. Disabled actions must retire that stale control as well as refuse
        # the mutation, otherwise the user is left with a button that can only
        # fail repeatedly.
        await _remove_keyboard(
            update, 'Not enabled yet — say "hi" for your menu.'
        )
        return

    if action == "repeat":
        await repeat_last_meal(update, context)
    elif action == "describe":
        # The bar label alone carries no meal text, so this teaches the command
        # rather than opening a text-capturing state — Home stays idle, and a
        # half-finished describe draft can never collide with a guided flow.
        from .describe import describe_usage_reply

        await describe_usage_reply(message)
    else:  # action == "meal"
        logger.warning("Home router received a Meal label; expected Diet entry point")
        await message.reply_text("Use /diet to log a meal.")


# ---------------------------------------------------------------------------
# Home voice router
# ---------------------------------------------------------------------------
async def home_voice_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle a voice note at Home, or explain why it cannot be handled.

    A note arriving *during* a guided flow is still refused without downloading
    anything (plan §8.6) — transcribing it would either disturb a live draft or
    silently discard the recording. Only idle Home accepts one.
    """
    if active_conversation_flow(context) is not None:
        await update.effective_message.reply_text(_ACTIVE_FLOW_HINT)
        return

    uid = update.effective_user.id
    if not phase1_enabled_for(uid):
        await _remove_keyboard(update, _MENU_GUIDANCE)
        return

    from .voice import handle_voice_meal

    await handle_voice_meal(update, context)


# ---------------------------------------------------------------------------
# /keyboard hide|show
# ---------------------------------------------------------------------------
async def keyboard_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show or hide the persistent keyboard without touching ledger/conversation.

    ``hide`` is always allowed and momentary — the keyboard may reappear on the
    next eligible Home response. ``show`` renders the bar only for a keyboard-
    eligible user and, during an active flow, only nudges + removes (plan §8.6).
    """
    args = context.args or []
    sub = args[0].lower() if args else ""
    uid = update.effective_user.id
    flow_active = active_conversation_flow(context) is not None

    if sub == "hide":
        await _remove_keyboard(
            update,
            "⌨️ Hidden for now; an eligible Home response may show it again.",
        )
        return

    if sub == "show":
        if flow_active:
            await _remove_keyboard(update, _ACTIVE_FLOW_HINT)
            return
        if home_keyboard_action_for(uid) == "send":
            await update.effective_message.reply_text(
                "⌨️ Quick-action bar restored.",
                reply_markup=home_reply_keyboard(),
            )
        else:
            await _remove_keyboard(
                update, "⌨️ The quick-action bar isn't enabled for you."
            )
        return

    await update.effective_message.reply_text(
        "Use /keyboard hide or /keyboard show."
    )
