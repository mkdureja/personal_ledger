"""/recent — show a user's latest study/gym/diet entries for reconciliation.

Lets a user confirm a save actually landed when Telegram could not deliver the
confirmation (e.g. a send failure after a committed write), without needing to
inspect the database. Strictly owner-scoped.
"""

from __future__ import annotations

import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from ..config import localize
from .common import escape_html, reply_html

logger = logging.getLogger(__name__)

_RECENT_LIMIT = 10


def _parse_utc(value: str) -> datetime | None:
    """Parse a stored timestamp (with or without microseconds) as naive UTC."""
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _format_entry(entry: dict) -> str:
    kind = entry["kind"]
    summary = escape_html(str(entry["summary"]))
    if kind == "study":
        body = f"📖 <b>{summary}</b> — {entry['n1']} min"
    elif kind == "gym":
        body = f"🏋️ <b>{summary}</b> — {entry['n1']}×{entry['n2']}"
    else:  # diet
        calories = f" — {entry['n1']} cal" if entry["n1"] is not None else ""
        body = f"🍽️ <b>{summary}</b>{calories}"

    parsed = _parse_utc(entry["logged_at"])
    when = localize(parsed).strftime("%d %b %H:%M") if parsed else "?"
    return f"{body}\n   <i>{when}</i>"


async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the acting user's most recent entries across study, gym, and diet."""
    db = context.bot_data["db"]
    user = update.effective_user
    entries = await db.get_recent_entries(user.id, _RECENT_LIMIT)

    if not entries:
        await reply_html(
            update.message,
            "🗒️ No recent entries yet. Log something with /study, /gym, or /diet.",
        )
        return

    lines = ["🗒️ <b>Recent entries</b>\n"]
    lines.extend(_format_entry(entry) for entry in entries)
    await reply_html(update.message, "\n".join(lines))
