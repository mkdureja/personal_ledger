"""Weekly habit adherence counts only days each habit has existed."""

from __future__ import annotations

from datetime import date

from bot.handlers.analytics import _eligible_habit_days

_WEEK_START = date(2026, 7, 12)
_TODAY = date(2026, 7, 18)  # a full 7-day window


def _habit(created_at):
    return {"id": 1, "habit_name": "x", "created_at": created_at}


def test_habit_existing_all_week_counts_seven():
    assert _eligible_habit_days(_habit("2026-07-01 00:00:00"), _WEEK_START, _TODAY) == 7


def test_habit_created_midweek_counts_from_creation():
    # 2026-07-16 12:00 UTC -> local date 2026-07-16 -> days 16,17,18 = 3
    assert _eligible_habit_days(_habit("2026-07-16 12:00:00"), _WEEK_START, _TODAY) == 3


def test_habit_created_today_counts_one():
    assert _eligible_habit_days(_habit("2026-07-18 12:00:00"), _WEEK_START, _TODAY) == 1


def test_missing_created_at_counts_full_week():
    assert _eligible_habit_days({"created_at": None}, _WEEK_START, _TODAY) == 7
