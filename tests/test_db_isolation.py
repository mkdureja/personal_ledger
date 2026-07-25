"""Read isolation: a read never observes an uncommitted (rolled-back) write."""

from __future__ import annotations

import asyncio

import pytest

from bot.config import today_local
from bot.database import _utc_timestamp_now


@pytest.mark.asyncio
async def test_read_never_sees_uncommitted_write(db_with_user, user_id):
    started = asyncio.Event()
    release = asyncio.Event()

    async def writer():
        try:
            async with db_with_user._write_operation():
                await db_with_user.conn.execute(
                    "INSERT INTO study_logs "
                    "(user_id, subject, duration_min, logged_at) VALUES (?, ?, ?, ?)",
                    (user_id, "Phantom", 10, _utc_timestamp_now()),
                )
                started.set()
                await release.wait()
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass

    writer_task = asyncio.create_task(writer())
    await started.wait()

    reader_task = asyncio.create_task(
        db_with_user.get_study_logs(user_id, today_local(), today_local())
    )
    await asyncio.sleep(0.05)
    # The read is serialized behind the open write transaction.
    assert not reader_task.done()

    release.set()
    rows = await reader_task
    await writer_task

    # The uncommitted row was rolled back and never observed.
    assert all(row["subject"] != "Phantom" for row in rows)
