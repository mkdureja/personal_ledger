"""/settings and /reminders — per-user reminder opt-in (implementation_plan Phase 4).

Reminders are opt-in: a new authorized user receives no scheduled messages until
they run ``/reminders on``. Settings are strictly per-user and never grant access
(authentication stays in ``.env``).
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from .common import reply_html

logger = logging.getLogger(__name__)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the acting user's current settings."""
    db = context.bot_data["db"]
    user = update.effective_user
    settings = await db.get_user_settings(user.id)
    reminders_on = bool(settings and settings["reminders_enabled"])
    profile = (settings or {}).get("routine_profile") or "default"

    status = "🔔 on" if reminders_on else "🔕 off"
    await reply_html(
        update.message,
        "⚙️ <b>Your settings</b>\n"
        f"Reminders: <b>{status}</b>\n"
        f"Routine profile: <b>{profile}</b>\n\n"
        "Change reminders with <code>/reminders on</code> or "
        "<code>/reminders off</code>.",
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
