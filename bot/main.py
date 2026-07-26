"""
Ledger Bot — Entry point.

Builds the Application, registers all handlers, initializes DB,
schedules the daily reminder, and runs polling.

Usage:
    python -m bot.main
"""

from __future__ import annotations

import logging

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
)

from .config import BOT_TOKEN, DB_PATH, REMINDER_TIME, ROUTINE_PATH, LOCAL_TZ, LOG_FORMAT
from .database import DatabaseManager
from .routine import load_routine
from .handlers.common import (
    AUTH_FILTER,
    cancel_command,
    error_handler,
    undo_cancel_callback,
    undo_command,
    undo_confirm_callback,
)
from .handlers.start import start_command, help_command, menu_command, menu_callback
from .handlers.study import study_conv_handler
from .handlers.gym import gym_conv_handler, stale_gym_callback
from .handlers.diet import diet_conv_handler, stale_meal_callback
from .handlers.catalog import food_command, recipe_command
from .handlers.habits import (
    habits_setup_conv_handler,
    habit_check_callback,
    habit_uncheck_callback,
    habit_toggle_day_callback,
    habit_page_callback,
    habit_noop_callback,
    remove_habit_callback,
    habit_setup_done_callback,
    habit_setup_page_callback,
)
from .handlers.analytics import (
    summary_command,
    chart_command,
    streak_command,
    analytics_callback,
)
from .handlers.reminders import anchor_job, daily_reminder

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)

# PTB's HTTPX transport logs the full Bot API request URL — which embeds the
# bot token — at INFO. Silence that layer, and redact the token from any
# remaining log output as defense in depth (e.g. exceptions, other libraries).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class _TokenRedactingFilter(logging.Filter):
    """Scrub the bot token from formatted log records before they are emitted."""

    def __init__(self, secret: str) -> None:
        super().__init__()
        self._secret = secret

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if self._secret:
            message = record.getMessage()
            if self._secret in message:
                record.msg = message.replace(self._secret, "***")
                record.args = None
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_TokenRedactingFilter(BOT_TOKEN))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Post-init: connect DB, schedule reminders
# ---------------------------------------------------------------------------
async def post_init(application) -> None:
    """Called after Application.initialize() — set up DB and jobs."""
    if "db" in application.bot_data:
        logger.warning("post_init called again — skipping (already initialised)")
        return

    db = DatabaseManager(DB_PATH)
    await db.connect()
    try:
        await db.init_db()
    except BaseException:
        # Close the connection we just opened; it is not yet registered in
        # bot_data, so post_shutdown would not otherwise clean it up.
        await db.close()
        raise
    application.bot_data["db"] = db
    logger.info("Database ready")

    # Schedule routine anchors when a routine file is present; otherwise fall
    # back to the single legacy habit reminder (backward compatible).
    routine = load_routine(ROUTINE_PATH)
    if routine is not None:
        application.bot_data["routine_targets"] = routine.targets
        application.bot_data["routine_quotes"] = routine.quotes
        for anchor in routine.anchors:
            application.job_queue.run_daily(
                anchor_job,
                time=anchor.at.replace(tzinfo=LOCAL_TZ),
                name=f"anchor_{anchor.id}",
                data=anchor,
            )
        logger.info(
            "Scheduled %d routine anchor(s): %s",
            len(routine.anchors),
            ", ".join(f"{a.id}@{a.at:%H:%M}" for a in routine.anchors),
        )
    else:
        application.job_queue.run_daily(
            daily_reminder,
            time=REMINDER_TIME,
            name="daily_habit_reminder",
        )
        logger.info("Daily reminder scheduled at %s (no routine file)", REMINDER_TIME)


async def post_shutdown(application) -> None:
    """Called on shutdown — close DB."""
    db = application.bot_data.get("db")
    if db:
        await db.close()
        logger.info("Database closed")


# ---------------------------------------------------------------------------
# Build and run
# ---------------------------------------------------------------------------
def build_application() -> Application:
    """Construct the Application with all handlers registered, without polling.

    Separated from :func:`main` so tests can register the real handlers and drive
    ``Application.process_update(...)`` through genuine handler order, filters,
    and error routing without starting the network loop.
    """
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # --- Conversation handlers (must be added before simple handlers) ---
    application.add_handler(study_conv_handler)
    application.add_handler(gym_conv_handler)
    application.add_handler(diet_conv_handler)
    application.add_handler(habits_setup_conv_handler)

    # --- Simple command handlers ---
    application.add_handler(CommandHandler("start", start_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("help", help_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("menu", menu_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("food", food_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("recipe", recipe_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("summary", summary_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("chart", chart_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("streak", streak_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("undo", undo_command, filters=AUTH_FILTER))
    # Conversation fallbacks consume /cancel while active; this catches a
    # stale marker or a cancel command sent outside an active conversation.
    application.add_handler(CommandHandler("cancel", cancel_command, filters=AUTH_FILTER))

    # --- Callback query handlers ---
    # Guided-flow callbacks are consumed by their ConversationHandlers while
    # active. These handlers safely retire the same buttons after timeout.
    application.add_handler(
        CallbackQueryHandler(stale_gym_callback, pattern=r"^gym_")
    )
    application.add_handler(
        CallbackQueryHandler(stale_meal_callback, pattern=r"^meal_")
    )
    # Menu callbacks
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu_"))
    # Habit callbacks
    application.add_handler(
        CallbackQueryHandler(habit_check_callback, pattern=r"^habit_(check|c)_")
    )
    application.add_handler(
        CallbackQueryHandler(habit_uncheck_callback, pattern=r"^habit_(uncheck|u)_")
    )
    application.add_handler(CallbackQueryHandler(habit_toggle_day_callback, pattern=r"^habit_toggle_"))
    application.add_handler(CallbackQueryHandler(habit_page_callback, pattern=r"^habit_page_"))
    application.add_handler(CallbackQueryHandler(habit_noop_callback, pattern=r"^habit_noop_"))
    application.add_handler(CallbackQueryHandler(remove_habit_callback, pattern=r"^habit_remove_"))
    application.add_handler(
        CallbackQueryHandler(habit_setup_page_callback, pattern=r"^habit_setup_page_")
    )
    # Also handle a setup button after its conversation has timed out.
    application.add_handler(
        CallbackQueryHandler(habit_setup_done_callback, pattern=r"^habit_setup_done_")
    )
    # Undo confirm/cancel callbacks
    application.add_handler(
        CallbackQueryHandler(undo_confirm_callback, pattern=r"^undo_do_")
    )
    application.add_handler(
        CallbackQueryHandler(undo_cancel_callback, pattern=r"^undo_keep_")
    )
    # Analytics callbacks
    application.add_handler(CallbackQueryHandler(analytics_callback, pattern=r"^(analytics_|chart_)"))

    # --- Error handler ---
    application.add_error_handler(error_handler)

    return application


def main() -> None:
    """Build the application and start polling."""
    application = build_application()

    # Preserve updates accumulated during downtime so a study/gym/meal command
    # sent while the process was restarting is not silently dropped.
    logger.info("Starting Ledger bot in polling mode...")
    application.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
