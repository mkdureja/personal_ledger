"""Startup integration tests."""

from __future__ import annotations

import pytest
from telegram.ext import ApplicationBuilder

from bot import main as main_module


@pytest.mark.asyncio
async def test_post_init_sets_up_database_and_reminder(tmp_path, monkeypatch):
    """A requirements-only install must provide a working JobQueue."""
    monkeypatch.setattr(main_module, "DB_PATH", str(tmp_path / "ledger-test.db"))
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
