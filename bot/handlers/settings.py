"""/settings and /reminders — per-user reminder opt-in (implementation_plan Phase 4).

Reminders are opt-in: a new authorized user receives no scheduled messages until
they run ``/reminders on``. Settings are strictly per-user and never grant access
(authentication stays in ``.env``).
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from ..config import GEMINI_AVAILABLE, home_keyboard_action_for, phase1_enabled_for
from .common import reply_html

logger = logging.getLogger(__name__)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the acting user's current settings."""
    db = context.bot_data["db"]
    user = update.effective_user
    # A newly authorized user may run this before /start, so their users row may
    # not exist yet; user_settings has a FK to users, so ensure it first.
    await db.ensure_user(user.id, user.username, user.first_name)
    settings = await db.get_user_settings(user.id)
    reminders_on = bool(settings and settings["reminders_enabled"])
    profile = (settings or {}).get("routine_profile") or "default"

    status = "🔔 on" if reminders_on else "🔕 off"
    suggestions_on = await db.get_suggestions_enabled(user.id)
    suggestions_status = "✨ on" if suggestions_on else "off"

    # Only shown when the deployment actually has a key, so a user is never
    # offered a privacy choice that has no effect.
    ai_line = ""
    if GEMINI_AVAILABLE:
        ai_on = await db.get_ai_parsing_enabled(user.id)
        ai_line = (
            f"AI meal parsing: <b>{'🤖 on' if ai_on else 'off'}</b> "
            "(<code>/aiparse on|off</code>)\n"
        )

    # Fast logging and how many "usual" amounts are saved — the two Phase 1
    # facts a user cannot otherwise see without hunting through the picker.
    fast_line = ""
    if phase1_enabled_for(user.id):
        usual_count = await db.count_default_quantities(user.id)
        bar = "shown" if home_keyboard_action_for(user.id) == "send" else "hidden"
        fast_line = (
            "Fast logging: <b>⚡ on</b>\n"
            f"Quick-action bar: <b>{bar}</b> "
            "(<code>/keyboard hide|show</code>)\n"
            f"Saved “usual” amounts: <b>{usual_count}</b>\n"
        )

    await reply_html(
        update.message,
        "⚙️ <b>Your settings</b>\n"
        f"Reminders: <b>{status}</b>\n"
        f"Food suggestions: <b>{suggestions_status}</b>\n"
        f"{ai_line}"
        f"{fast_line}"
        f"Routine profile: <b>{profile}</b>\n\n"
        "Change reminders with <code>/reminders on</code> or "
        "<code>/reminders off</code>.\n"
        "Personalized food ordering: <code>/suggestions on|off|reset</code>."
        + (
            "\nManage a “usual” amount with ⚙️ beside an item while logging."
            if fast_line
            else ""
        ),
    )


async def suggestions_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Control personalized food ordering: /suggestions on|off|reset.

    History is always recorded; this only controls whether it reorders the tap
    list. ``reset`` clears the user's pins and hides — deliberately *not* their
    saved default quantities, which are separate per-item settings.
    """
    db = context.bot_data["db"]
    user = update.effective_user
    args = context.args or []
    choice = args[0].lower() if args else ""

    await db.ensure_user(user.id, user.username, user.first_name)
    await db.ensure_user_settings(user.id, default_enabled=False)

    if choice in ("on", "off"):
        enabled = choice == "on"
        await db.set_suggestions_enabled(user.id, enabled)
        state = "✨ on" if enabled else "off"
        detail = (
            "Your saved foods are ordered by what you log most for each meal."
            if enabled
            else "Your saved foods are listed alphabetically."
        )
        await reply_html(
            update.message,
            f"Food suggestions are <b>{state}</b>. {detail}",
        )
        return

    if choice == "reset":
        removed = await db.reset_food_preferences(user.id)
        await reply_html(
            update.message,
            f"🧹 Cleared <b>{removed}</b> pin/hide preference(s). "
            "Ranking now uses only your logged history.\n"
            "Your saved “usual” amounts are kept — manage those with ⚙️ "
            "beside an item.",
        )
        return

    enabled = await db.get_suggestions_enabled(user.id)
    state = "✨ on" if enabled else "off"
    await reply_html(
        update.message,
        f"Food suggestions: <b>{state}</b>.\n"
        "Use <code>/suggestions on</code>, <code>/suggestions off</code>, or "
        "<code>/suggestions reset</code> (clears pins/hides).",
    )


async def aiparse_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Control external meal-text parsing: /aiparse on|off.

    The "on" branch is the consent moment, so it states plainly what leaves the
    host and what does not, rather than hiding it behind a settings label.
    """
    db = context.bot_data["db"]
    user = update.effective_user
    args = context.args or []
    choice = args[0].lower() if args else ""

    await db.ensure_user(user.id, user.username, user.first_name)

    if not GEMINI_AVAILABLE:
        # Nothing to consent to. Say so instead of storing a preference that
        # cannot take effect.
        await reply_html(
            update.message,
            "🤖 AI meal parsing isn't configured on this bot, so "
            "<code>/describe</code> uses local parsing only.\n"
            "Nothing you type is sent anywhere.",
        )
        return

    if choice == "on":
        await db.set_ai_parsing_enabled(user.id, True)
        await reply_html(
            update.message,
            "🤖 AI meal parsing is <b>on</b>.\n\n"
            "<b>What this means</b>\n"
            "• When <code>/describe</code> can't understand part of what you "
            "typed, that leftover text is sent to Google Gemini to be split "
            "into items.\n"
            "• Only that text is sent — never your logs, totals, name, or "
            "Telegram ID.\n"
            "• Calories and macros still come only from your saved foods and "
            "the shared catalog. The AI never supplies a nutrition number.\n"
            "• You still confirm every meal before it's saved.\n\n"
            "Turn it off any time with <code>/aiparse off</code>.",
        )
        return

    if choice == "off":
        await db.set_ai_parsing_enabled(user.id, False)
        await reply_html(
            update.message,
            "🤖 AI meal parsing is <b>off</b>. Nothing you type is sent "
            "anywhere; <code>/describe</code> uses local parsing only.",
        )
        return

    enabled = await db.get_ai_parsing_enabled(user.id)
    state = "🤖 on" if enabled else "off"
    await reply_html(
        update.message,
        f"AI meal parsing: <b>{state}</b>.\n\n"
        "When on, text <code>/describe</code> can't parse locally is sent to "
        "Google Gemini to be split into items. Nutrition always comes from your "
        "saved foods — never from the AI.\n"
        "Use <code>/aiparse on</code> or <code>/aiparse off</code>.",
    )


async def reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle the acting user's reminders: /reminders on|off."""
    db = context.bot_data["db"]
    user = update.effective_user
    args = context.args or []
    choice = args[0].lower() if args else ""

    if choice not in ("on", "off"):
        await reply_html(
            update.message,
            "Usage: <code>/reminders on</code> or <code>/reminders off</code>.",
        )
        return

    # May be this user's first-ever command (before /start); user_settings has a
    # FK to users, so ensure the users row exists before upserting settings.
    await db.ensure_user(user.id, user.username, user.first_name)
    enabled = choice == "on"
    await db.set_reminders_enabled(user.id, enabled)
    if enabled:
        await reply_html(
            update.message,
            "🔔 Reminders are <b>on</b>. You'll get your daily nudges and routine "
            "anchors.",
        )
    else:
        await reply_html(
            update.message,
            "🔕 Reminders are <b>off</b>. You won't receive scheduled messages until "
            "you run <code>/reminders on</code>.",
        )
