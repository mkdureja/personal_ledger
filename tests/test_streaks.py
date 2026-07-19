"""
Tests for streak calculation and day-bucketing.

These are the highest-value tests — streak logic and timezone
boundaries are where subtle bugs live.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.asyncio


class TestStreaks:
    """Streak calculation."""

    async def test_streak_of_five(self, db_with_user, user_id):
        """5 consecutive days → streak of 5."""
        habit_id, _ = await db_with_user.add_habit(user_id, "Meditate")
        today = date.today()

        for i in range(5):
            d = today - timedelta(days=i)
            await db_with_user.check_habit(user_id, habit_id, d)

        streak = await db_with_user.get_streak(user_id, habit_id, today)
        assert streak == 5

    async def test_streak_with_gap(self, db_with_user, user_id):
        """Gap in the middle breaks the streak."""
        habit_id, _ = await db_with_user.add_habit(user_id, "Read")
        today = date.today()

        # Check today, yesterday, skip day-before, then 2 more days
        await db_with_user.check_habit(user_id, habit_id, today)
        await db_with_user.check_habit(user_id, habit_id, today - timedelta(days=1))
        # Skip day -2
        await db_with_user.check_habit(user_id, habit_id, today - timedelta(days=3))
        await db_with_user.check_habit(user_id, habit_id, today - timedelta(days=4))

        streak = await db_with_user.get_streak(user_id, habit_id, today)
        assert streak == 2, "Streak should count only consecutive days from today"

    async def test_streak_today_unchecked(self, db_with_user, user_id):
        """If today is not checked, streak is 0."""
        habit_id, _ = await db_with_user.add_habit(user_id, "Run")
        today = date.today()

        # Check yesterday and day before
        await db_with_user.check_habit(user_id, habit_id, today - timedelta(days=1))
        await db_with_user.check_habit(user_id, habit_id, today - timedelta(days=2))

        streak = await db_with_user.get_streak(user_id, habit_id, today)
        assert streak == 0, "Today unchecked means streak is 0"

    async def test_streak_single_day(self, db_with_user, user_id):
        """Only today checked → streak of 1."""
        habit_id, _ = await db_with_user.add_habit(user_id, "Stretch")
        today = date.today()

        await db_with_user.check_habit(user_id, habit_id, today)
        streak = await db_with_user.get_streak(user_id, habit_id, today)
        assert streak == 1

    async def test_streak_no_data(self, db_with_user, user_id):
        """No check-offs at all → streak of 0."""
        habit_id, _ = await db_with_user.add_habit(user_id, "Yoga")
        today = date.today()

        streak = await db_with_user.get_streak(user_id, habit_id, today)
        assert streak == 0

    async def test_streak_across_month_boundary(self, db_with_user, user_id):
        """Streak spanning month boundary (e.g., Jan 29-Feb 2)."""
        habit_id, _ = await db_with_user.add_habit(user_id, "Journal")
        # Use Feb 2 as today
        feb_2 = date(2026, 2, 2)

        for i in range(5):  # Feb 2, Feb 1, Jan 31, Jan 30, Jan 29
            d = feb_2 - timedelta(days=i)
            await db_with_user.check_habit(user_id, habit_id, d)

        streak = await db_with_user.get_streak(user_id, habit_id, feb_2)
        assert streak == 5

    async def test_streak_after_reactivation(self, db_with_user, user_id):
        """Re-added habit: old logs exist but streak should still work."""
        habit_id, _ = await db_with_user.add_habit(user_id, "Floss")
        today = date.today()

        # Log a few days
        for i in range(3):
            await db_with_user.check_habit(user_id, habit_id, today - timedelta(days=i))

        # Deactivate and reactivate
        await db_with_user.deactivate_habit(user_id, habit_id)
        reactivated_id, _ = await db_with_user.add_habit(user_id, "Floss")
        assert reactivated_id == habit_id

        # Old logs still count (same habit_id)
        streak = await db_with_user.get_streak(user_id, habit_id, today)
        assert streak == 3


class TestDayBucketing:
    """Test that local-date computation works correctly.

    These tests verify the config.local_date_from_utc function,
    which is critical for IST (UTC+5:30) day-bucketing.
    """

    def test_utc_midnight_to_ist(self):
        """UTC midnight → IST 5:30 AM same day."""
        from datetime import datetime, timezone
        from bot.config import local_date_from_utc

        utc_dt = datetime(2026, 7, 17, 0, 0, 0, tzinfo=timezone.utc)
        local = local_date_from_utc(utc_dt)
        # UTC midnight = IST 5:30 AM Jul 17
        assert local == date(2026, 7, 17)

    def test_ist_midnight_via_utc(self):
        """IST midnight (UTC 18:30 previous day) → local date is today."""
        from datetime import datetime, timezone
        from bot.config import local_date_from_utc

        # 12:30 AM IST Jul 18 = 7:00 PM UTC Jul 17
        utc_dt = datetime(2026, 7, 17, 19, 0, 0, tzinfo=timezone.utc)
        local = local_date_from_utc(utc_dt)
        # This is 00:30 IST on Jul 18
        assert local == date(2026, 7, 18), (
            "A log at 12:30 AM IST should be attributed to Jul 18, not Jul 17"
        )

    def test_late_night_ist(self):
        """1:00 AM IST → should be the current IST date, not previous."""
        from datetime import datetime, timezone
        from bot.config import local_date_from_utc

        # 1:00 AM IST Jul 18 = 7:30 PM UTC Jul 17
        utc_dt = datetime(2026, 7, 17, 19, 30, 0, tzinfo=timezone.utc)
        local = local_date_from_utc(utc_dt)
        assert local == date(2026, 7, 18)

    def test_5am_ist_still_same_day(self):
        """5:00 AM IST → same day (UTC 23:30 previous day)."""
        from datetime import datetime, timezone
        from bot.config import local_date_from_utc

        # 5:00 AM IST Jul 18 = 11:30 PM UTC Jul 17
        utc_dt = datetime(2026, 7, 17, 23, 30, 0, tzinfo=timezone.utc)
        local = local_date_from_utc(utc_dt)
        assert local == date(2026, 7, 18)

    def test_afternoon_ist(self):
        """3:00 PM IST → same day as UTC date."""
        from datetime import datetime, timezone
        from bot.config import local_date_from_utc

        # 3:00 PM IST Jul 18 = 9:30 AM UTC Jul 18
        utc_dt = datetime(2026, 7, 18, 9, 30, 0, tzinfo=timezone.utc)
        local = local_date_from_utc(utc_dt)
        assert local == date(2026, 7, 18)
