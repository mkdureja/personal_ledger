"""Lifecycle-aware weekly habit adherence (implementation_plan Phase 5).

Adherence counts a habit-day only when an activity period covered that local day,
so deactivating a habit mid-week keeps the days it was live (and its completions
on them) instead of erasing them from both numerator and denominator.
"""

from __future__ import annotations

from datetime import date

import pytest_asyncio

from bot.database import DatabaseManager

WEEK_START = date(2026, 7, 12)
TODAY = date(2026, 7, 18)  # a full 7-day window
MANOJ = 111
RATIKA = 222


@pytest_asyncio.fixture
async def db():
    mgr = DatabaseManager(":memory:")
    await mgr.connect()
    await mgr.init_db()
    await mgr.ensure_user(MANOJ, "manoj", "Manoj")
    await mgr.ensure_user(RATIKA, "ratika", "Ratika")
    yield mgr
    await mgr.close()


async def _check_range(db, user_id, habit_id, start: date, end: date):
    day = start
    from datetime import timedelta

    while day <= end:
        await db.check_habit(user_id, habit_id, day)
        day += timedelta(days=1)


async def test_no_habits_is_zero_over_zero(db):
    assert await db.get_habit_adherence(MANOJ, WEEK_START, TODAY) == (0, 0)


async def test_habit_active_all_week_counts_seven(db):
    hid, _ = await db.add_habit(MANOJ, "Read", today=date(2026, 7, 1))
    await _check_range(db, MANOJ, hid, WEEK_START, TODAY)
    assert await db.get_habit_adherence(MANOJ, WEEK_START, TODAY) == (7, 7)


async def test_habit_created_midweek_counts_from_creation(db):
    hid, _ = await db.add_habit(MANOJ, "Read", today=date(2026, 7, 16))
    await _check_range(db, MANOJ, hid, date(2026, 7, 16), TODAY)
    assert await db.get_habit_adherence(MANOJ, WEEK_START, TODAY) == (3, 3)


async def test_habit_created_today_and_done_today_is_one_over_one(db):
    hid, _ = await db.add_habit(MANOJ, "Read", today=TODAY)
    await db.check_habit(MANOJ, hid, TODAY)
    assert await db.get_habit_adherence(MANOJ, WEEK_START, TODAY) == (1, 1)


async def test_deactivation_midweek_keeps_earlier_completions(db):
    # Active 12-15, completed 12-14, then deactivated on the 15th.
    hid, _ = await db.add_habit(MANOJ, "Read", today=date(2026, 7, 12))
    await _check_range(db, MANOJ, hid, date(2026, 7, 12), date(2026, 7, 14))
    await db.deactivate_habit(MANOJ, hid, today=date(2026, 7, 15))

    # The habit is now inactive, but the 4 days it was live (12-15) still count,
    # and the 3 completions are preserved — not erased to 0/0.
    assert await db.get_habit_adherence(MANOJ, WEEK_START, TODAY) == (3, 4)


async def test_reactivation_opens_a_new_period_with_a_gap(db):
    hid, _ = await db.add_habit(MANOJ, "Read", today=date(2026, 7, 12))
    await db.deactivate_habit(MANOJ, hid, today=date(2026, 7, 13))  # period 12-13
    await db.add_habit(MANOJ, "Read", today=date(2026, 7, 16))  # new period 16-open
    await db.check_habit(MANOJ, hid, date(2026, 7, 12))
    await db.check_habit(MANOJ, hid, date(2026, 7, 16))

    # Eligible: 12,13 + 16,17,18 = 5 (14 and 15 fall in the gap). Done: 12,16 = 2.
    assert await db.get_habit_adherence(MANOJ, WEEK_START, TODAY) == (2, 5)


async def test_adherence_is_owner_scoped(db):
    m_hid, _ = await db.add_habit(MANOJ, "Yoga", today=date(2026, 7, 12))
    await db.add_habit(RATIKA, "Yoga", today=date(2026, 7, 12))
    await db.deactivate_habit(MANOJ, m_hid, today=date(2026, 7, 13))

    # Manoj deactivated his Yoga; Ratika's identically-named habit is unaffected.
    assert await db.get_habit_adherence(MANOJ, WEEK_START, TODAY) == (0, 2)
    assert await db.get_habit_adherence(RATIKA, WEEK_START, TODAY) == (0, 7)


async def test_migration_backfill_does_not_invent_inactive_history():
    """A habit inactive at migration time gets no period (unknowable history)."""
    mgr = DatabaseManager(":memory:")
    await mgr.connect()
    try:
        await mgr.init_db()
        await mgr.ensure_user(MANOJ, "manoj", "Manoj")
        # Create then deactivate before the periods table would be backfilled:
        # emulate a legacy inactive habit by clearing its periods and resetting
        # the schema version so the v4 backfill re-runs.
        hid, _ = await mgr.add_habit(MANOJ, "Legacy", today=date(2026, 7, 1))
        await mgr.deactivate_habit(MANOJ, hid, today=date(2026, 7, 2))
        await mgr.conn.execute("DELETE FROM habit_activity_periods")
        await mgr.conn.execute("PRAGMA user_version = 3")
        await mgr.conn.commit()

        await mgr.init_db()  # re-runs v4 backfill

        rows = await mgr._query_all(
            "SELECT * FROM habit_activity_periods WHERE habit_id = ?", (hid,)
        )
        assert rows == []  # inactive habit was not given an invented period
    finally:
        await mgr.close()
