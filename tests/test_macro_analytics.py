"""Macro aggregation coverage for daily and weekly diet summaries."""

from __future__ import annotations

from datetime import datetime, time, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ParseMode

from bot.config import LOCAL_TZ, today_local
from bot.handlers.analytics import (
    _daily_summary,
    _macro_summary_lines,
    _weekly_summary,
)


def _today_timestamp() -> str:
    local_noon = datetime.combine(today_local(), time(hour=12), tzinfo=LOCAL_TZ)
    return local_noon.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _summary_db(diet_logs: list[dict[str, object]]) -> SimpleNamespace:
    return SimpleNamespace(
        get_study_logs=AsyncMock(return_value=[]),
        get_gym_logs=AsyncMock(return_value=[]),
        get_diet_logs=AsyncMock(return_value=diet_logs),
        get_active_habits=AsyncMock(return_value=[]),
    )


@pytest.mark.asyncio
async def test_daily_summary_aggregates_complete_decimal_macros(user_id: int) -> None:
    logged_at = _today_timestamp()
    diet_logs = [
        {
            "logged_at": logged_at,
            "calories": 600,
            "protein_g": 25.5,
            "carbs_g": 80.0,
            "fat_g": 15.25,
        },
        {
            "logged_at": logged_at,
            "calories": 250,
            "protein_g": 5.0,
            "carbs_g": 20.0,
            "fat_g": 10.0,
        },
    ]
    message = SimpleNamespace(reply_text=AsyncMock())

    await _daily_summary(message, _summary_db(diet_logs), user_id)

    text = message.reply_text.await_args.args[0]
    assert "<b>Macros</b>: P 30.5g · C 100g · F 25.25g" in text
    assert "known values" not in text
    assert "Missing macro values" not in text
    assert message.reply_text.await_args.kwargs["parse_mode"] == ParseMode.HTML


@pytest.mark.asyncio
async def test_weekly_summary_labels_partial_macro_totals(user_id: int) -> None:
    logged_at = _today_timestamp()
    diet_logs = [
        {
            "logged_at": logged_at,
            "calories": 600,
            "protein_g": 25.0,
            "carbs_g": 80.0,
            "fat_g": 15.0,
        },
        {
            "logged_at": logged_at,
            "calories": None,
            "protein_g": 5.0,
            "carbs_g": None,
            "fat_g": None,
        },
    ]
    message = SimpleNamespace(reply_text=AsyncMock())

    await _weekly_summary(message, _summary_db(diet_logs), user_id)

    text = message.reply_text.await_args.args[0]
    # In the mock we have 2 logs on the SAME day, so it divides by 1 logged day.
    # 600 total / 1 day = 600 avg.
    assert "600 known cal total, ~600 known cal/day avg (1 day)" in text
    assert "1/2 meal(s) missing calories" in text
    assert (
        "<b>Macros (known values)</b>: "
        "P 30g (2/2) · C 80g (1/2) · F 15g (1/2)" in text
    )
    assert "Missing macro values are excluded from these totals." in text

@pytest.mark.asyncio
async def test_weekly_summary_divides_by_logged_days(user_id: int) -> None:
    """EDGE-6: Calorie average uses unique logged days, not hardcoded 7."""
    day1 = _today_timestamp()
    # Subtract exactly 1 day
    day2 = (datetime.fromisoformat(day1) - timedelta(days=1)).isoformat()
    
    diet_logs = [
        {"logged_at": day1, "calories": 1000},
        {"logged_at": day2, "calories": 500},
    ]
    message = SimpleNamespace(reply_text=AsyncMock())

    await _weekly_summary(message, _summary_db(diet_logs), user_id)

    text = message.reply_text.await_args.args[0]
    # Total = 1500, logged_days = 2, avg = 750
    assert "1500 cal total, ~750 cal/day avg (2 days)" in text


@pytest.mark.asyncio
async def test_reply_html_line_chunks(user_id: int) -> None:
    """TEST-10: Output is properly chunked at message limits."""
    from bot.handlers.analytics import _reply_html_line_chunks
    
    message = SimpleNamespace(reply_text=AsyncMock())
    
    # Telegram limit is 4096.
    # First chunk will have line 1 (3000 chars + header).
    # Second chunk will have line 2 (3000 chars + header continued).
    # Third chunk will have line 3 (3000 chars + header continued).
    long_line = "A" * 3000
    lines = [long_line, long_line, long_line]
    
    await _reply_html_line_chunks(message, "Header", lines)
    
    assert message.reply_text.call_count == 3
    first_call_args = message.reply_text.call_args_list[0].args[0]
    second_call_args = message.reply_text.call_args_list[1].args[0]
    third_call_args = message.reply_text.call_args_list[2].args[0]
    
    assert len(first_call_args) == len("Header\n") + 3000
    assert len(second_call_args) == len("Header (continued)\n") + 3000
    assert len(third_call_args) == len("Header (continued)\n") + 3000


def test_legacy_rows_do_not_render_missing_macros_as_zero() -> None:
    lines = _macro_summary_lines([{"calories": 500}, {"calories": None}])

    assert lines == ["⚖️ <b>Macros</b>: not recorded for these meals"]
    assert "0g" not in lines[0]


@pytest.mark.asyncio
async def test_weekly_summary_does_not_count_unknown_calories_as_zero(
    user_id: int,
) -> None:
    logged_at = _today_timestamp()
    diet_logs = [
        {"logged_at": logged_at, "calories": None},
        {"logged_at": logged_at, "calories": None},
    ]
    message = SimpleNamespace(reply_text=AsyncMock())

    await _weekly_summary(message, _summary_db(diet_logs), user_id)

    text = message.reply_text.await_args.args[0]
    assert "<b>Diet</b>: calories not recorded" in text
    assert "0 cal total" not in text


def test_macro_output_is_bounded_for_extreme_legacy_totals() -> None:
    rows = [
        {"protein_g": 1e308, "carbs_g": 1e308, "fat_g": 1e308},
        {"protein_g": 1e308, "carbs_g": 1e308, "fat_g": 1e308},
    ]

    lines = _macro_summary_lines(rows)

    assert len("\n".join(lines)) < 250
    assert "P ≥1e308g" in lines[0]


def test_tiny_positive_macro_totals_are_not_rounded_to_zero() -> None:
    lines = _macro_summary_lines(
        [{"protein_g": 0.001, "carbs_g": 0.004, "fat_g": 0.00001}]
    )

    assert "P 0.001g" in lines[0]
    assert "C 0.004g" in lines[0]
    assert "F 1e-05g" in lines[0]
    assert " 0g" not in lines[0]
