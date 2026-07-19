"""
Daily habit reminder via PTB JobQueue.

Sends an evening message listing unchecked habits.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from telegram.ext import ContextTypes

from .common import escape_html
from ..config import ALLOWED_USER_IDS, today_local

logger = logging.getLogger(__name__)

# Keep a little headroom below Telegram's documented 4096-character ceiling.
# Counting UTF-16 code units is conservative for emoji-heavy names and matches
# the unit Telegram uses for message-entity offsets.
_REMINDER_MESSAGE_LIMIT = 4000
_CONTINUATION_HEADER = "⏰ <b>Evening Reminder</b> (continued)\n\n"
_REMINDER_FOOTER = "\n\nTap /habits to check them off!"


def _telegram_text_units(text: str) -> int:
    """Return Telegram's UTF-16 text length for conservative limit checks."""
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def _split_habit_name(name: object, payload_limit: int) -> list[str]:
    """Escape and split one habit without cutting an HTML entity in half."""
    first_prefix = "⬜ "
    continuation_prefix = "↳ "
    fragments: list[str] = []
    current = first_prefix
    current_units = _telegram_text_units(current)

    for character in str(name):
        escaped_character = escape_html(character)
        character_units = _telegram_text_units(escaped_character)
        if current_units + character_units > payload_limit:
            fragments.append(current)
            current = continuation_prefix
            current_units = _telegram_text_units(current)
        current += escaped_character
        current_units += character_units

    fragments.append(current)
    return fragments


def _build_reminder_messages(habit_names: Sequence[object]) -> list[str]:
    """Build complete, independently valid HTML messages below Telegram's limit."""
    if not habit_names:
        return []

    count = len(habit_names)
    first_header = (
        "⏰ <b>Evening Reminder</b>\n\n"
        f"You still have {count} habit{'s' if count > 1 else ''} unchecked today:\n\n"
    )
    maximum_overhead = max(
        _telegram_text_units(first_header),
        _telegram_text_units(_CONTINUATION_HEADER),
    ) + _telegram_text_units(_REMINDER_FOOTER)
    payload_limit = _REMINDER_MESSAGE_LIMIT - maximum_overhead

    fragments = [
        fragment
        for name in habit_names
        for fragment in _split_habit_name(name, payload_limit)
    ]

    payloads: list[str] = []
    current_lines: list[str] = []
    current_units = 0
    for fragment in fragments:
        fragment_units = _telegram_text_units(fragment)
        separator_units = 1 if current_lines else 0
        if current_lines and current_units + separator_units + fragment_units > payload_limit:
            payloads.append("\n".join(current_lines))
            current_lines = []
            current_units = 0
            separator_units = 0
        current_lines.append(fragment)
        current_units += separator_units + fragment_units

    if current_lines:
        payloads.append("\n".join(current_lines))

    messages: list[str] = []
    for index, payload in enumerate(payloads):
        header = first_header if index == 0 else _CONTINUATION_HEADER
        footer = _REMINDER_FOOTER if index == len(payloads) - 1 else ""
        message = f"{header}{payload}{footer}"
        if _telegram_text_units(message) > _REMINDER_MESSAGE_LIMIT:
            raise AssertionError("Reminder chunk exceeded the safe Telegram limit")
        messages.append(message)

    return messages


async def daily_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled daily job: remind each user of unchecked habits."""
    db = context.bot_data["db"]
    today = today_local()

    unchecked_map = await db.get_users_with_unchecked_habits(ALLOWED_USER_IDS, today)

    for user_id, habit_names in unchecked_map.items():
        if not habit_names:
            continue

        messages = _build_reminder_messages(habit_names)

        try:
            for text in messages:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode="HTML",
                )
            logger.info(
                "Sent reminder to user %d: %d unchecked across %d message(s)",
                user_id,
                len(habit_names),
                len(messages),
            )
        except Exception:
            logger.exception("Failed to send reminder to user %d", user_id)

    # Also send a "well done" message to users who completed all habits
    for user_id in ALLOWED_USER_IDS:
        if user_id not in unchecked_map:
            # Check if they have any habits at all
            habits = await db.get_active_habits(user_id)
            if habits:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="🎉 <b>All habits done today!</b> Great job! 💪",
                        parse_mode="HTML",
                    )
                except Exception:
                    logger.exception("Failed to send completion msg to user %d", user_id)
