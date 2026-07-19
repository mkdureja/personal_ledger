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
