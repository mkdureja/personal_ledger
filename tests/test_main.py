"""Startup integration tests."""

from __future__ import annotations

import pytest
from telegram.ext import ApplicationBuilder

from bot import main as main_module


@pytest.mark.asyncio
async def test_post_init_sets_up_database_and_reminder(tmp_path, monkeypatch):
    """A requirements-only install must provide a working JobQueue."""
    monkeypatch.setattr(main_module, "DB_PATH", str(tmp_path / "ledger-test.db"))
    # Isolate from any real routine.yaml so the legacy reminder path is tested.
    monkeypatch.setattr(main_module, "ROUTINE_PATH", str(tmp_path / "no-routine.yaml"))
    application = ApplicationBuilder().token("123456:TEST_TOKEN").build()

    assert application.job_queue is not None

    await main_module.post_init(application)
    db = application.bot_data["db"]
    try:
        assert db.conn is not None
        assert [job.name for job in application.job_queue.jobs()] == [
            "daily_habit_reminder"
        ]
    finally:
        await main_module.post_shutdown(application)

    assert db._conn is None


def test_build_application_registers_handlers_without_polling():
    """build_application wires up the real handlers and returns an Application.

    This is the seam the two-user isolation matrix drives via
    Application.process_update(...) without starting the network loop.
    """
    application = main_module.build_application()

    total_handlers = sum(len(group) for group in application.handlers.values())
    assert total_handlers > 10  # conversations + commands + callbacks
    assert application.error_handlers  # error handler registered
    # post_init/post_shutdown are registered but not invoked (no DB/network yet).
    assert application.bot_data.get("db") is None


@pytest.mark.asyncio
async def test_post_shutdown_safe_without_init():
    """post_shutdown should not crash if post_init was never called."""
    application = ApplicationBuilder().token("123456:TEST_TOKEN").build()
    await main_module.post_shutdown(application)
    # Should complete without raising exceptions


@pytest.mark.asyncio
async def test_post_init_is_idempotent(tmp_path, monkeypatch):
    """Calling post_init twice should not create duplicate connections or jobs."""
    monkeypatch.setattr(main_module, "DB_PATH", str(tmp_path / "ledger-test.db"))
    monkeypatch.setattr(main_module, "ROUTINE_PATH", str(tmp_path / "no-routine.yaml"))
    application = ApplicationBuilder().token("123456:TEST_TOKEN").build()

    await main_module.post_init(application)
    db_first = application.bot_data["db"]
    jobs_first = len(application.job_queue.jobs())
    
    # Call again
    await main_module.post_init(application)
    db_second = application.bot_data["db"]
    jobs_second = len(application.job_queue.jobs())
    
    assert db_first is db_second  # Same instance
    assert jobs_first == jobs_second  # No new jobs added

    await main_module.post_shutdown(application)


@pytest.mark.asyncio
async def test_post_init_closes_db_when_init_fails(tmp_path, monkeypatch):
    """If init_db raises, the just-opened connection must be closed, not leaked."""
    from bot import database as db_module

    closed = {"value": False}
    original_close = db_module.DatabaseManager.close

    async def failing_init(self):
        raise RuntimeError("schema init failed")

    async def spy_close(self):
        closed["value"] = True
        await original_close(self)

    monkeypatch.setattr(db_module.DatabaseManager, "init_db", failing_init)
    monkeypatch.setattr(db_module.DatabaseManager, "close", spy_close)
    monkeypatch.setattr(main_module, "DB_PATH", str(tmp_path / "ledger-test.db"))
    monkeypatch.setattr(main_module, "ROUTINE_PATH", str(tmp_path / "no-routine.yaml"))
    application = ApplicationBuilder().token("123456:TEST_TOKEN").build()

    with pytest.raises(RuntimeError):
        await main_module.post_init(application)

    assert closed["value"] is True
    assert "db" not in application.bot_data
