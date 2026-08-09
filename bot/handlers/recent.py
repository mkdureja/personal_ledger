"""/recent — show a user's latest study/gym/diet entries for reconciliation.

Lets a user confirm a save actually landed when Telegram could not deliver the
confirmation (e.g. a send failure after a committed write), without needing to
inspect the database. Strictly owner-scoped.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from ..config import localize
from .common import escape_html, reply_html

logger = logging.getLogger(__name__)

_RECENT_LIMIT = 10
# Headroom below Telegram's 4096-character ceiling, counted in UTF-16 units (the
# unit Telegram uses). A single diet entry (food_items up to 500 chars, expanded
# by HTML escaping) stays well under this, so packing whole entry blocks is safe.
_MESSAGE_LIMIT = 4000


def _telegram_text_units(text: str) -> int:
    """Return Telegram's UTF-16 text length for conservative limit checks."""
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def _pack_messages(header: str, blocks: list[str], limit: int) -> list[str]:
    """Pack entry blocks into newline-joined messages under ``limit`` UTF-16 units.

    The header leads the first message; continuation messages carry only blocks.
    A block never straddles two messages, so no HTML entity is ever cut in half.
    """
    messages: list[str] = []
    lines = [header]
    units = _telegram_text_units(header)
    for block in blocks:
        block_units = _telegram_text_units(block) + 1  # + newline separator
        if units + block_units > limit and len(lines) > 1:
            messages.append("\n".join(lines))
            lines = [block]
            units = _telegram_text_units(block)
        else:
            lines.append(block)
            units += block_units
    messages.append("\n".join(lines))
    return messages


def _parse_utc(value: str) -> datetime | None:
    """Parse a stored timestamp (with or without microseconds) as naive UTC."""
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


# Bound each entry's displayed summary. /recent is a reconciliation view ("did my
# save land?"), so a truncated description still identifies the entry while keeping
# every block small enough that packing stays well under Telegram's limit.
_MAX_SUMMARY_CHARS = 120


def _format_entry(entry: dict) -> str:
    kind = entry["kind"]
    raw_summary = str(entry["summary"])
    if len(raw_summary) > _MAX_SUMMARY_CHARS:
        raw_summary = raw_summary[: _MAX_SUMMARY_CHARS - 1] + "…"
    summary = escape_html(raw_summary)
    if kind == "study":
        body = f"📖 <b>{summary}</b> — {entry['n1']} min"
    elif kind == "gym":
        # reps is NULL for a workout logged without per-set detail — either a
        # varying exercise, or a session recorded as "I trained this" and
        # nothing more. Rendering it blind produced "1×None".
        sets = entry["n1"]
        reps = entry["n2"]
        detail = f"{sets}×{reps}" if reps is not None else f"{sets} set(s)"
        body = f"🏋️ <b>{summary}</b> — {detail}"
    else:  # diet
        calories = f" — {entry['n1']} cal" if entry["n1"] is not None else ""
        body = f"🍽️ <b>{summary}</b>{calories}"

    parsed = _parse_utc(entry["logged_at"])
    when = localize(parsed).strftime("%d %b %H:%M") if parsed else "?"
    return f"{body}\n   <i>{when}</i>"


async def show_recent(
    message: Any, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    """Render one user's recent entries as replies to ``message``.

    Takes the target message rather than the update, so the Home 🗒️ Recent button
    can reuse it: a callback query has no ``update.message``.
    """
    db = context.bot_data["db"]
    entries = await db.get_recent_entries(user_id, _RECENT_LIMIT)

    if not entries:
        await reply_html(
            message,
            "🗒️ No recent entries yet. Log something with /study, /gym, or /diet.",
        )
        return

    blocks = [_format_entry(entry) for entry in entries]
    for chunk in _pack_messages("🗒️ <b>Recent entries</b>\n", blocks, _MESSAGE_LIMIT):
        await reply_html(message, chunk)


async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the acting user's most recent entries across study, gym, and diet."""
    await show_recent(update.effective_message, context, update.effective_user.id)
