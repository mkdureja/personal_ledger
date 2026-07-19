"""
Daily habit reminder via PTB JobQueue.

Sends an evening message listing unchecked habits.
"""

from __future__ import annotations

import logging

from telegram.ext import ContextTypes

from ..config import ALLOWED_USER_IDS, today_local

logger = logging.getLogger(__name__)


async def daily_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled daily job: remind each user of unchecked habits."""
    db = context.bot_data["db"]
    today = today_local()

    unchecked_map = await db.get_users_with_unchecked_habits(ALLOWED_USER_IDS, today)

    for user_id, habit_names in unchecked_map.items():
        if not habit_names:
            continue

        habits_list = "\n".join(f"⬜ {name}" for name in habit_names)
        text = (
            f"⏰ **Evening Reminder**\n\n"
            f"You still have {len(habit_names)} habit{'s' if len(habit_names) > 1 else ''} "
            f"unchecked today:\n\n"
            f"{habits_list}\n\n"
            f"Tap /habits to check them off!"
        )

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="Markdown",
            )
            logger.info("Sent reminder to user %d: %d unchecked", user_id, len(habit_names))
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
                        text="🎉 **All habits done today!** Great job! 💪",
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception("Failed to send completion msg to user %d", user_id)
