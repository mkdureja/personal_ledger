"""Per-user reminder opt-in and settings (implementation_plan Phase 4).

A new authorized user receives no scheduled messages until they opt in; toggling
one user never changes another; and a settings row never grants access.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from bot.database import DatabaseManager
from bot.handlers import reminders as reminders_module
from bot.handlers.reminders import daily_reminder
from bot.handlers.settings import reminders_command, settings_command
from bot.handlers.start import start_command
from bot import migrations

MANOJ = 111
RATIKA = 222


@pytest_asyncio.fixture
async def two_user_db():
    mgr = DatabaseManager(":memory:")
    await mgr.connect()
    await mgr.init_db()
    await mgr.ensure_user(MANOJ, "manoj", "Manoj")
    await mgr.ensure_user(RATIKA, "ratika", "Ratika")
    yield mgr
    await mgr.close()


# ---------------------------------------------------------------------------
# Settings storage
# ---------------------------------------------------------------------------
async def test_new_user_has_no_settings_row(two_user_db):
    assert await two_user_db.get_user_settings(MANOJ) is None


async def test_ensure_user_settings_defaults_disabled_and_never_overrides(two_user_db):
    await two_user_db.ensure_user_settings(MANOJ, default_enabled=False)
    assert (await two_user_db.get_user_settings(MANOJ))["reminders_enabled"] == 0

    # Turn on, then re-ensure: the existing choice must be preserved.
    await two_user_db.set_reminders_enabled(MANOJ, True)
    await two_user_db.ensure_user_settings(MANOJ, default_enabled=False)
    assert (await two_user_db.get_user_settings(MANOJ))["reminders_enabled"] == 1


async def test_toggling_one_user_does_not_affect_the_other(two_user_db):
    await two_user_db.set_reminders_enabled(MANOJ, True)
    await two_user_db.set_reminders_enabled(RATIKA, False)

    assert (await two_user_db.get_user_settings(MANOJ))["reminders_enabled"] == 1
    assert (await two_user_db.get_user_settings(RATIKA))["reminders_enabled"] == 0

    await two_user_db.set_reminders_enabled(RATIKA, True)
    assert (await two_user_db.get_user_settings(MANOJ))["reminders_enabled"] == 1
    assert (await two_user_db.get_user_settings(RATIKA))["reminders_enabled"] == 1


async def test_get_reminder_enabled_users_filters(two_user_db):
    await two_user_db.set_reminders_enabled(MANOJ, True)
    # RATIKA left with no settings row (opted out by default).
    enabled = await two_user_db.get_reminder_enabled_users({MANOJ, RATIKA})
    assert enabled == {MANOJ}


async def test_migration_backfills_existing_users_as_enabled():
    mgr = DatabaseManager(":memory:")
    await mgr.connect()
    try:
        await mgr.init_db()
        await mgr.ensure_user(MANOJ, "manoj", "Manoj")
        # Simulate a database that predates the user_settings migration.
        await mgr.conn.execute("PRAGMA user_version = 2")
        await mgr.conn.commit()

        await mgr.init_db()  # re-runs the v3 backfill

        assert await migrations.get_user_version(mgr.conn) == migrations.LATEST_VERSION
        assert (await mgr.get_user_settings(MANOJ))["reminders_enabled"] == 1
    finally:
        await mgr.close()


# ---------------------------------------------------------------------------
# Reminder delivery respects opt-in
# ---------------------------------------------------------------------------
async def test_daily_reminder_skips_opted_out_user(two_user_db, monkeypatch):
    monkeypatch.setattr(reminders_module, "ALLOWED_USER_IDS", frozenset({MANOJ, RATIKA}))
    await two_user_db.set_reminders_enabled(MANOJ, True)
    # RATIKA never opted in.
    await two_user_db.add_habit(MANOJ, "Read")
    await two_user_db.add_habit(RATIKA, "Read")

    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(bot_data={"db": two_user_db}, bot=bot)

    await daily_reminder(context)

    chat_ids = {call.kwargs["chat_id"] for call in bot.send_message.await_args_list}
    assert chat_ids == {MANOJ}  # RATIKA received nothing


async def test_daily_reminder_sends_nothing_when_all_opted_out(two_user_db, monkeypatch):
    monkeypatch.setattr(reminders_module, "ALLOWED_USER_IDS", frozenset({MANOJ, RATIKA}))
    await two_user_db.add_habit(MANOJ, "Read")

    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(bot_data={"db": two_user_db}, bot=bot)

    await daily_reminder(context)

    bot.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def test_reminders_command_on_then_off(two_user_db):
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=MANOJ, username="manoj", first_name="Manoj"),
    )

    on_ctx = SimpleNamespace(bot_data={"db": two_user_db}, args=["on"])
    await reminders_command(update, on_ctx)
    assert (await two_user_db.get_user_settings(MANOJ))["reminders_enabled"] == 1

    off_ctx = SimpleNamespace(bot_data={"db": two_user_db}, args=["off"])
    await reminders_command(update, off_ctx)
    assert (await two_user_db.get_user_settings(MANOJ))["reminders_enabled"] == 0


async def test_reminders_command_rejects_bad_arg(two_user_db):
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=MANOJ))
    context = SimpleNamespace(bot_data={"db": two_user_db}, args=["maybe"])

    await reminders_command(update, context)

    assert await two_user_db.get_user_settings(MANOJ) is None  # unchanged
    assert "Usage" in message.reply_text.await_args.args[0]


async def test_settings_command_reports_state(two_user_db):
    await two_user_db.set_reminders_enabled(MANOJ, True)
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=MANOJ, username="manoj", first_name="Manoj"),
    )
    context = SimpleNamespace(bot_data={"db": two_user_db})

    await settings_command(update, context)

    text = message.reply_text.await_args.args[0]
    assert "Your settings" in text
    assert "on" in text.lower()


async def test_reminders_on_as_first_ever_command(two_user_db):
    """A never-/start-ed user running /reminders on must not hit a FK error.

    user_settings has a FK to users, so the command must create the users row
    before upserting settings (codex #10).
    """
    unstarted_id = 444  # no users row, no /start
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(
            id=unstarted_id, username="new", first_name="New"
        ),
    )
    context = SimpleNamespace(bot_data={"db": two_user_db}, args=["on"])

    await reminders_command(update, context)

    settings = await two_user_db.get_user_settings(unstarted_id)
    assert settings is not None and settings["reminders_enabled"] == 1


async def test_start_defaults_new_user_to_opt_out(two_user_db):
    message = SimpleNamespace(reply_text=AsyncMock())
    new_user_id = 333
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=new_user_id, username="new", first_name="New"),
    )
    context = SimpleNamespace(bot_data={"db": two_user_db})

    await start_command(update, context)

    settings = await two_user_db.get_user_settings(new_user_id)
    assert settings is not None and settings["reminders_enabled"] == 0
