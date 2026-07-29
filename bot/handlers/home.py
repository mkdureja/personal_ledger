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

from ..config import (
    home_keyboard_action_for,
    phase1_enabled_for,
    today_local,
)
from ..keyboards import (
    home_reply_keyboard,
    main_menu_keyboard,
    reply_keyboard_remove,
)
from .common import (
    GREETINGS,
    HOME_ACTIONS,
    HOME_WORDS,
    active_conversation_flow,
    escape_html,
    normalize_control_text,
    reply_html,
)

logger = logging.getLogger(__name__)

_MENU_GUIDANCE = "ℹ️ Use /menu to open the main menu."
_ACTIVE_FLOW_HINT = "⏳ Finish this flow or /cancel first."


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
async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render today's cross-section snapshot plus the quick-action bar.

    Sends two messages: the snapshot with the inline main menu, then a short
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
    text = (
        f"👋 <b>{first_name}</b> — here's today:\n\n"
        f"🍽️ Diet: {meal_count} meal(s), {calories} cal{cal_suffix}\n"
        f"📖 Study: {study_min} min\n"
        f"🏋️ Gym: {gym_count} exercise(s)\n"
        f"✅ Habits: {checked_count}/{len(active_habits)} done"
    )
    await reply_html(update.effective_message, text, reply_markup=main_menu_keyboard())

    if home_keyboard_action_for(uid) == "send":
        await update.effective_message.reply_text(
            "Tap 🍽️ <b>Meal</b> to log, or 🔁 <b>Repeat</b> your last meal.",
            parse_mode="HTML",
            reply_markup=home_reply_keyboard(),
        )
    else:
        await update.effective_message.reply_text(
            "Use /diet to log a meal.",
            reply_markup=reply_keyboard_remove(),
        )


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

    uid = update.effective_user.id
    normalized = normalize_control_text(text)
    is_control = (
        normalized in HOME_ACTIONS
        or normalized in HOME_WORDS
        or normalized in GREETINGS
    )

    # 2. Phase 1 disabled: control text gets /menu guidance + keyboard removal;
    # arbitrary text gets no Phase 1 surface (no snapshot, no mutation).
    if not phase1_enabled_for(uid):
        if is_control:
            await _remove_keyboard(update, _MENU_GUIDANCE)
        return

    # 3. Phase 1 enabled.
    if normalized in GREETINGS or normalized in HOME_WORDS:
        await show_home(update, context)
    elif normalized == "repeat":
        # Exact Repeat is a Release B (B1) mutation; the routing lands here now
        # but performs no DB write until that unit ships.
        await _sync_keyboard(
            update, "🔁 Fast Repeat isn't enabled in this build yet.", uid
        )
    elif normalized == "describe":
        await _sync_keyboard(
            update, "📝 Describe isn't enabled yet.", uid
        )
    elif normalized == "meal":
        # "Meal" is a real Diet entry point; reaching the router means an entry
        # point failed to claim it. Never mutate; point at /diet.
        logger.warning("Home router received 'meal'; expected Diet entry point")
        await message.reply_text("Use /diet to log a meal.")
    else:
        await show_home(update, context)
        await message.reply_text("Use 🍽️ Meal or /diet to log a meal.")


# ---------------------------------------------------------------------------
# Home voice router
# ---------------------------------------------------------------------------
async def home_voice_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Reject a voice note at Home without downloading it (plan §8.6)."""
    if active_conversation_flow(context) is not None:
        await update.effective_message.reply_text(_ACTIVE_FLOW_HINT)
        return

    uid = update.effective_user.id
    if not phase1_enabled_for(uid):
        await _remove_keyboard(update, _MENU_GUIDANCE)
        return
    await _sync_keyboard(
        update, "🎤 Voice logging isn't enabled yet.", uid
    )


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
