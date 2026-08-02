"""
Ledger Bot — Entry point.

Builds the Application, registers all handlers, initializes DB,
schedules the daily reminder, and runs polling.

Usage:
    python -m bot.main
"""

from __future__ import annotations

import logging
import sqlite3
from functools import partial

from telegram import BotCommand
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .config import (
    BACKUP_DEST_DIR,
    BOT_TOKEN,
    DB_PATH,
    REMINDER_TIME,
    ROUTINE_PATH,
    LOCAL_TZ,
    LOG_FORMAT,
)
from .database import DatabaseManager
from .instance_lock import AlreadyRunningError, SingleInstanceLock, lock_path_for
from .migration_preflight import (
    MigrationPreflightError,
    prepare_database_for_startup,
)
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
from .handlers.home import (
    home_command,
    home_text_router,
    home_voice_router,
    keyboard_command,
)
from .handlers.study import study_conv_handler
from .handlers.gym import gym_conv_handler, stale_gym_callback
from .handlers.diet import (
    _DIET_PHASE1_CALLBACK_RE,
    _RECEIPT_CALLBACK_RE,
    diet_conv_handler,
    stale_diet_callback,
    stale_meal_callback,
    stale_phase1_diet_callback,
    stale_receipt_callback,
)
from .handlers.receipts import RECEIPT_UNDO_PATTERN, undo_from_receipt
from .handlers.catalog import food_command, recipe_command
from .handlers.describe import (
    describe_cancel_callback,
    describe_command,
    describe_save_callback,
)
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
from .handlers.supplements import (
    supplements_setup_conv_handler,
    supplement_take_callback,
    supplement_untake_callback,
    supplement_toggle_day_callback,
    supplement_page_callback,
    supplement_noop_callback,
    remove_supplement_callback,
    supplement_setup_done_callback,
    supplement_setup_page_callback,
)
from .handlers.analytics import (
    summary_command,
    chart_command,
    streak_command,
    analytics_callback,
)
from .handlers.reminders import anchor_job, daily_reminder
from .handlers.recent import recent_command
from .handlers.settings import (
    aiparse_command,
    reminders_command,
    settings_command,
    suggestions_command,
)

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
    """Scrub the bot token from log records before they are emitted.

    Redacts the token from the rendered message, its positional args, and — the
    part the message-only approach missed — the formatted exception traceback and
    stack info, which the handler's formatter renders *after* the message from
    ``exc_info``. A Bot API request URL surfacing inside an exception would
    otherwise leak the token into service logs.
    """

    _REDACTION = "***"
    # Reused only to render a record's exception into text so it can be redacted.
    _exc_formatter = logging.Formatter()

    def __init__(self, secret: str) -> None:
        super().__init__()
        self._secret = secret

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not self._secret:
            return True

        message = record.getMessage()
        if self._secret in message:
            record.msg = message.replace(self._secret, self._REDACTION)
            record.args = None

        # The traceback is formatted from exc_info by the handler's formatter,
        # bypassing the message redaction above. Pre-render it here (cached in
        # exc_text so the formatter reuses it), then redact both it and any
        # stack info in place.
        if record.exc_info and not record.exc_text:
            record.exc_text = self._exc_formatter.formatException(record.exc_info)
        if record.exc_text and self._secret in record.exc_text:
            record.exc_text = record.exc_text.replace(self._secret, self._REDACTION)
        if record.stack_info and self._secret in record.stack_info:
            record.stack_info = record.stack_info.replace(self._secret, self._REDACTION)

        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_TokenRedactingFilter(BOT_TOKEN))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Post-init: connect DB, schedule reminders
# ---------------------------------------------------------------------------
#: The short everyday set shown in Telegram's command picker. Every other command
#: keeps working; this is the discoverable subset, not the whole reference.
COMMAND_MENU: tuple[tuple[str, str], ...] = (
    ("home", "Today and actions"),
    ("recent", "Recent entries"),
    ("undo", "Recover the latest supported entry"),
    ("help", "Full reference"),
)

async def register_command_menu(application) -> None:
    """Publish :data:`COMMAND_MENU` to Telegram's command picker.

    Best-effort: a failed registration degrades discoverability, never startup.
    """
    try:
        await application.bot.set_my_commands(
            [BotCommand(command, description) for command, description in COMMAND_MENU]
        )
        logger.info("Registered %d everyday command(s)", len(COMMAND_MENU))
    except Exception:
        logger.warning("Could not register the command menu", exc_info=True)


async def post_init(application, *, register_commands: bool = False) -> None:
    """Called after Application.initialize() — set up DB and jobs.

    ``register_commands`` publishes the Telegram command picker, which is a Bot
    API call and therefore a real deployment side effect. Only ``main()`` turns it
    on (via :func:`build_application`), so a test driving this function directly
    never reaches the network.
    """
    if "db" in application.bot_data:
        logger.warning("post_init called again — skipping (already initialised)")
        return

    db = DatabaseManager(DB_PATH)
    try:
        await db.connect()
    except sqlite3.Error as exc:
        # A connection handle can be created before the first PRAGMA discovers
        # that the configured file is corrupt or not SQLite. DatabaseManager
        # closes that partial handle; turn the driver traceback into the same
        # controlled, actionable startup refusal used by migration preflight.
        error = MigrationPreflightError(
            f"Refusing to start: the configured database at {DB_PATH} could not "
            "be opened and validated as SQLite. Check DB_PATH or restore a "
            "verified backup before retrying. No migration or schema change was "
            "attempted."
        )
        logger.error("%s", error)
        raise error from exc
    try:
        # No production migration runs without a freshly verified backup of the
        # exact source it is about to change. This is the only approved
        # executable migration path; init_db()/run_migrations() below stay the
        # low-level primitive that migration tests drive directly.
        outcome = await prepare_database_for_startup(
            db.conn, db_path=DB_PATH, backup_dest_dir=BACKUP_DEST_DIR
        )
        if outcome.backup_path is not None:
            logger.info("Pre-migration backup verified before migrating")
        await db.init_db()
    except MigrationPreflightError as exc:
        # Refuse to start, having changed no schema. Log the actionable message
        # itself so the operator does not have to read a traceback for it.
        logger.error("%s", exc)
        await db.close()
        raise
    except BaseException:
        # Close the connection we just opened; it is not yet registered in
        # bot_data, so post_shutdown would not otherwise clean it up.
        await db.close()
        raise
    # Register the connection before any optional startup step, so a step that
    # aborts (including via cancellation) still leaves a connection post_shutdown
    # can close instead of leaking one.
    application.bot_data["db"] = db
    logger.info("Database ready")

    if register_commands:
        await register_command_menu(application)

    # Seed the shared curated catalog (idempotent upsert by provider id).
    try:
        from .catalog_seed import CATALOG_FOODS

        await db.seed_catalog(CATALOG_FOODS)
        logger.info("Seeded %d curated catalog food(s)", len(CATALOG_FOODS))
    except Exception:
        # Optional-feature degradation only. Catching ``BaseException`` here also
        # swallowed ``CancelledError`` and ``KeyboardInterrupt``, so a Ctrl-C or a
        # cancelled startup task would have been logged as "catalog seeding
        # failed" and then continued into polling. Those must propagate.
        logger.warning("Catalog seeding failed; search may be empty", exc_info=True)

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
def build_application(*, register_commands: bool = False) -> Application:
    """Construct the Application with all handlers registered, without polling.

    Separated from :func:`main` so tests can register the real handlers and drive
    ``Application.process_update(...)`` through genuine handler order, filters,
    and error routing without starting the network loop. ``register_commands``
    defaults to off for the same reason: only a real run should publish the
    Telegram command picker.
    """
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        # ConversationHandler relies on one-update-at-a-time processing to keep
        # per-user draft state consistent; make PTB's requirement explicit
        # rather than depending on the default (plan §8.7).
        .concurrent_updates(False)
        .post_init(partial(post_init, register_commands=register_commands))
        .post_shutdown(post_shutdown)
        .build()
    )

    # --- Conversation handlers (must be added before simple handlers) ---
    application.add_handler(study_conv_handler)
    application.add_handler(gym_conv_handler)
    application.add_handler(diet_conv_handler)
    application.add_handler(habits_setup_conv_handler)
    application.add_handler(supplements_setup_conv_handler)

    # --- Simple command handlers ---
    application.add_handler(CommandHandler("start", start_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("help", help_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("menu", menu_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("home", home_command, filters=AUTH_FILTER))
    application.add_handler(
        CommandHandler("describe", describe_command, filters=AUTH_FILTER)
    )
    application.add_handler(CommandHandler("food", food_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("recipe", recipe_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("summary", summary_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("chart", chart_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("streak", streak_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("undo", undo_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("recent", recent_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("settings", settings_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("reminders", reminders_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("suggestions", suggestions_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("aiparse", aiparse_command, filters=AUTH_FILTER))
    application.add_handler(CommandHandler("keyboard", keyboard_command, filters=AUTH_FILTER))
    # Conversation fallbacks consume /cancel while active; this catches a
    # stale marker or a cancel command sent outside an active conversation.
    application.add_handler(CommandHandler("cancel", cancel_command, filters=AUTH_FILTER))

    # --- Callback query handlers ---
    # Targeted Undo is a durable receipt control, deliberately outside every
    # ConversationHandler: it removes one exact completed meal and never touches
    # conversation state, so it stays usable during another guided flow. "Log
    # another" is instead a Diet entry point (registered above) because it opens
    # a flow.
    application.add_handler(
        CallbackQueryHandler(undo_from_receipt, pattern=RECEIPT_UNDO_PATTERN)
    )
    # Remaining receipt controls (Use current values, or Log another while a Diet
    # flow owns the update) and revisioned base-36 diet families are retired
    # inertly (answer + retire markup, no DB). Registered before the legacy stale
    # handlers so base-36 dpin/dhide route here, while pre-A decimal payloads
    # still fall through to the legacy handler below.
    application.add_handler(
        CallbackQueryHandler(stale_receipt_callback, pattern=_RECEIPT_CALLBACK_RE)
    )
    application.add_handler(
        CallbackQueryHandler(
            stale_phase1_diet_callback, pattern=_DIET_PHASE1_CALLBACK_RE
        )
    )
    # Guided-flow callbacks are consumed by their ConversationHandlers while
    # active. These handlers safely retire the same buttons after timeout.
    application.add_handler(
        CallbackQueryHandler(stale_gym_callback, pattern=r"^gym_")
    )
    application.add_handler(
        CallbackQueryHandler(stale_meal_callback, pattern=r"^meal_")
    )
    application.add_handler(
        CallbackQueryHandler(
            stale_diet_callback,
            pattern=r"^d(food|recipe|type|port|custom|back|rq|save|cancel|more|add"
            r"|recent|pin|hide|search|catalog)_",
        )
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
    # Supplement callbacks — a distinct prefix family from habits, so neither
    # checklist can route into the other's writes.
    application.add_handler(
        CallbackQueryHandler(supplement_take_callback, pattern=r"^supp_c_")
    )
    application.add_handler(
        CallbackQueryHandler(supplement_untake_callback, pattern=r"^supp_u_")
    )
    application.add_handler(
        CallbackQueryHandler(supplement_toggle_day_callback, pattern=r"^supp_toggle_")
    )
    application.add_handler(
        CallbackQueryHandler(supplement_page_callback, pattern=r"^supp_page_")
    )
    application.add_handler(
        CallbackQueryHandler(supplement_noop_callback, pattern=r"^supp_noop_")
    )
    application.add_handler(
        CallbackQueryHandler(remove_supplement_callback, pattern=r"^supp_remove_")
    )
    application.add_handler(
        CallbackQueryHandler(
            supplement_setup_page_callback, pattern=r"^supp_setup_page_"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            supplement_setup_done_callback, pattern=r"^supp_setup_done_"
        )
    )
    # Typed-meal (describe) callbacks
    application.add_handler(
        CallbackQueryHandler(describe_save_callback, pattern=r"^desc_save_")
    )
    application.add_handler(
        CallbackQueryHandler(describe_cancel_callback, pattern=r"^desc_cancel_")
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

    # --- Home routers (last in group 0) ---
    # Registered after every ConversationHandler and command handler, so an
    # active flow claims its own text/voice first. These only see leftover
    # greetings/Home/Home-action text and voice, and are flag-gated internally
    # ("Meal" is a Diet entry point, not handled here). Plan §8.7.
    application.add_handler(
        MessageHandler(
            AUTH_FILTER & filters.TEXT & ~filters.COMMAND, home_text_router
        )
    )
    application.add_handler(
        MessageHandler(AUTH_FILTER & filters.VOICE, home_voice_router)
    )

    # --- Error handler ---
    application.add_error_handler(error_handler)

    return application


def main() -> None:
    """Take the single-instance lock, then build the application and poll.

    The lock is acquired *before* anything opens or migrates the database — and
    therefore before the migration preflight — so a second process on this host
    exits without touching a byte of data. This process owns the open handle for
    the whole ``run_polling()`` lifetime and releases it in ``finally``; the OS
    releases it anyway if the process dies.
    """
    lock = SingleInstanceLock(lock_path_for(DB_PATH))
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc

    try:
        application = build_application(register_commands=True)

        # Preserve updates accumulated during downtime so a study/gym/meal command
        # sent while the process was restarting is not silently dropped.
        logger.info("Starting Ledger bot in polling mode...")
        application.run_polling(drop_pending_updates=False)
    finally:
        lock.release()


if __name__ == "__main__":
    main()
