"""Focused tests for callback safety and shared analytics UI paths."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ParseMode
from telegram.error import NetworkError
from telegram.ext import ConversationHandler

from bot import charts
from bot.config import today_local
from bot.handlers import analytics
from bot.handlers.common import activate_conversation
from bot.handlers.analytics import _send_streaks, _weekly_summary
from bot.handlers.diet import stale_meal_callback
from bot.handlers.gym import stale_gym_callback
from bot.handlers.habits import (
    ADDING_HABIT,
    add_habit_text,
    habit_check_callback,
    habit_noop_callback,
    habit_page_callback,
    habit_setup_done_callback,
    habit_setup_page_callback,
    habit_toggle_day_callback,
    habit_uncheck_callback,
    remove_habit_callback,
)
from bot.handlers.start import menu_callback
from bot.keyboards import (
    MAX_ACTIVE_HABITS,
    habit_checklist_keyboard,
    habit_setup_keyboard,
)


def _query(data: str, *, chat_id: int = 1, message_id: int = 1):
    message = SimpleNamespace(
        chat_id=chat_id,
        message_id=message_id,
        reply_text=AsyncMock(),
        reply_photo=AsyncMock(),
    )
    return SimpleNamespace(
        data=data,
        message=message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


def _update(user_id: int, query):
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=query.message.chat_id),
    )


def test_all_global_callbacks_are_authorized():
    """Every callback registered directly by main is wrapped by auth."""
    callbacks = [
        menu_callback,
        habit_check_callback,
        habit_uncheck_callback,
        habit_toggle_day_callback,
        habit_page_callback,
        habit_noop_callback,
        remove_habit_callback,
        habit_setup_done_callback,
        habit_setup_page_callback,
        stale_gym_callback,
        stale_meal_callback,
        analytics.analytics_callback,
    ]
    assert all(hasattr(callback, "__wrapped__") for callback in callbacks)


def test_habit_keyboards_never_exceed_telegram_button_limit(user_id):
    habits = [
        {"id": index, "habit_name": f"Habit {index}"}
        for index in range(1, 61)
    ]

    checklist = habit_checklist_keyboard(
        habits, set(), today_local(), user_id, is_today=True
    )
    setup = habit_setup_keyboard(habits, user_id)

    assert sum(len(row) for row in checklist.inline_keyboard) <= 100
    assert sum(len(row) for row in setup.inline_keyboard) <= 100
    assert all(
        str(user_id) in button.callback_data
        for keyboard in (checklist, setup)
        for row in keyboard.inline_keyboard
        for button in row
    )

    second_checklist = habit_checklist_keyboard(
        habits, set(), today_local(), user_id, is_today=True, page=1
    )
    second_setup = habit_setup_keyboard(habits, user_id, page=1)
    assert any(
        "Habit 49" in button.text
        for row in second_checklist.inline_keyboard
        for button in row
    )
    assert any(
        "Habit 49" in button.text
        for row in second_setup.inline_keyboard
        for button in row
    )


def test_habit_callback_payloads_fit_telegram_limit():
    user_id = (1 << 52) - 1
    maximum_row_id = (1 << 63) - 1
    habits = [
        {"id": maximum_row_id - index, "habit_name": f"Habit {index}"}
        for index in range(60)
    ]

    keyboards = (
        habit_checklist_keyboard(
            habits, set(), today_local(), user_id, is_today=True, page=1
        ),
        habit_setup_keyboard(habits, user_id, page=1),
    )

    assert all(
        len(button.callback_data.encode("utf-8")) <= 64
        for keyboard in keyboards
        for row in keyboard.inline_keyboard
        for button in row
    )


@pytest.mark.asyncio
async def test_stale_habit_date_is_rejected_without_database_write(user_id):
    stale_date = today_local() - timedelta(days=2)
    query = _query(f"habit_check_{user_id}_42_{stale_date.isoformat()}")
    db = SimpleNamespace(check_habit=AsyncMock())
    context = SimpleNamespace(bot_data={"db": db})

    await habit_check_callback(_update(user_id, query), context)

    db.check_habit.assert_not_awaited()
    query.answer.assert_awaited_once()
    assert query.answer.await_args.kwargs["show_alert"] is True
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_habit_button_owner_mismatch_does_not_clear_owners_keyboard(user_id):
    query = _query(
        f"habit_check_{user_id + 1}_42_{today_local().isoformat()}"
    )
    db = SimpleNamespace(check_habit=AsyncMock(), get_active_habits=AsyncMock())
    context = SimpleNamespace(bot_data={"db": db})

    await habit_check_callback(_update(user_id, query), context)

    db.get_active_habits.assert_not_awaited()
    db.check_habit.assert_not_awaited()
    query.answer.assert_awaited_once()
    query.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_back_to_habits_ends_setup_and_retires_keyboard(user_id):
    query = _query(f"habit_setup_done_{user_id}")
    db = SimpleNamespace(get_active_habits=AsyncMock(return_value=[]))
    context = SimpleNamespace(
        bot_data={"db": db},
        user_data={"habit_setup_prompt": (query.message.chat_id, query.message.message_id)},
    )
    update = _update(user_id, query)
    activate_conversation(update, context, "habits")

    result = await habit_setup_done_callback(update, context)

    assert result == ConversationHandler.END
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    query.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_back_to_habits_delivery_failure_still_ends_setup(user_id):
    query = _query(f"habit_setup_done_{user_id}")
    query.message.reply_text.side_effect = NetworkError("offline")
    db = SimpleNamespace(get_active_habits=AsyncMock(return_value=[]))
    context = SimpleNamespace(
        bot_data={"db": db},
        user_data={"habit_setup_prompt": (query.message.chat_id, query.message.message_id)},
    )
    update = _update(user_id, query)
    activate_conversation(update, context, "habits")

    result = await habit_setup_done_callback(update, context)

    assert result == ConversationHandler.END
    assert context.user_data == {}


@pytest.mark.asyncio
async def test_stale_back_keeps_active_setup_state(user_id):
    query = _query(f"habit_setup_done_{user_id}", message_id=1)
    context = SimpleNamespace(
        bot_data={"db": SimpleNamespace()},
        user_data={"habit_setup_prompt": (query.message.chat_id, 2)},
    )
    update = _update(user_id, query)
    activate_conversation(update, context, "habits")

    result = await habit_setup_done_callback(update, context)

    assert result == ADDING_HABIT
    assert "_ledger_active_conversation" in context.user_data
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_legacy_habit_checklist_can_open_second_page(user_id):
    habits = [
        {"id": index, "habit_name": f"Habit {index}"}
        for index in range(1, 61)
    ]
    db = SimpleNamespace(
        get_active_habits=AsyncMock(return_value=habits),
        get_checked_habits=AsyncMock(return_value=set()),
    )
    query = _query(
        f"habit_page_{user_id}_{today_local().isoformat()}_1"
    )
    context = SimpleNamespace(bot_data={"db": db})

    await habit_page_callback(_update(user_id, query), context)

    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert "0/60" in query.edit_message_text.await_args.args[0]
    assert "page 2/2" in query.edit_message_text.await_args.args[0]
    assert any(
        "Habit 49" in button.text
        for row in markup.inline_keyboard
        for button in row
    )


@pytest.mark.asyncio
async def test_stale_remove_button_cannot_mutate_habits(user_id):
    query = _query(f"habit_remove_{user_id}_42")
    db = SimpleNamespace(
        get_active_habits=AsyncMock(),
        deactivate_habit=AsyncMock(),
    )
    context = SimpleNamespace(bot_data={"db": db}, user_data={})

    await remove_habit_callback(_update(user_id, query), context)

    db.get_active_habits.assert_not_awaited()
    db.deactivate_habit.assert_not_awaited()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_active_habit_cap_blocks_database_insert(user_id):
    habits = [
        {"id": index, "habit_name": f"Habit {index}"}
        for index in range(MAX_ACTIVE_HABITS)
    ]
    db = SimpleNamespace(
        get_active_habits=AsyncMock(return_value=habits),
        add_habit=AsyncMock(),
    )
    message = SimpleNamespace(text="One more", reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=user_id),
    )
    context = SimpleNamespace(bot_data={"db": db})

    result = await add_habit_text(update, context)

    assert result == ADDING_HABIT
    db.add_habit.assert_not_awaited()
    assert "at most 49" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_add_habit_text_rejects_case_insensitive_duplicates(user_id):
    """EDGE-1: Adding 'meditate' when 'Meditate' is active should be rejected."""
    habits = [{"id": 1, "habit_name": "Meditate"}]
    db = SimpleNamespace(
        get_active_habits=AsyncMock(return_value=habits),
        add_habit=AsyncMock(),
    )
    message = SimpleNamespace(text="meditate", reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=user_id),
    )
    context = SimpleNamespace(bot_data={"db": db})

    result = await add_habit_text(update, context)

    assert result == ADDING_HABIT
    db.add_habit.assert_not_awaited()
    assert "Already active: <b>meditate</b>" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_weekly_habit_percentage_counts_only_active_ids(user_id):
    db = SimpleNamespace(
        get_study_logs=AsyncMock(return_value=[]),
        get_gym_logs=AsyncMock(return_value=[]),
        get_diet_logs=AsyncMock(return_value=[]),
        get_active_habits=AsyncMock(
            return_value=[{"id": 1, "habit_name": "Read"}]
        ),
        get_habit_logs_range=AsyncMock(
            return_value=[{"habit_id": 1}, {"habit_id": 999}]
        ),
    )
    message = SimpleNamespace(reply_text=AsyncMock())

    await _weekly_summary(message, db, user_id)

    text = message.reply_text.await_args.args[0]
    assert "1/7 (14%)" in text
    assert "2/7" not in text
    assert message.reply_text.await_args.kwargs["parse_mode"] == ParseMode.HTML


@pytest.mark.asyncio
async def test_streak_names_are_html_escaped(user_id):
    db = SimpleNamespace(
        get_active_habits=AsyncMock(
            return_value=[{"id": 1, "habit_name": "Read <b>& notes"}]
        ),
        get_streak=AsyncMock(return_value=2),
    )
    message = SimpleNamespace(reply_text=AsyncMock())

    await _send_streaks(message, db, user_id)

    text = message.reply_text.await_args.args[0]
    assert "Read &lt;b&gt;&amp; notes" in text
    assert "Read <b>& notes" not in text


@pytest.mark.asyncio
async def test_analytics_callback_reuses_message_helper_without_update_mutation(
    user_id, monkeypatch
):
    class FrozenUpdate:
        __slots__ = ("callback_query", "effective_user")

        def __init__(self, callback_query, effective_user):
            self.callback_query = callback_query
            self.effective_user = effective_user

    query = _query("analytics_summary")
    update = FrozenUpdate(query, SimpleNamespace(id=user_id))
    context = SimpleNamespace(bot_data={"db": object()})
    daily_summary = AsyncMock()
    monkeypatch.setattr(analytics, "_daily_summary", daily_summary)

    await analytics.analytics_callback(update, context)

    daily_summary.assert_awaited_once_with(query.message, context.bot_data["db"], user_id)
    query.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_chart_rendering_runs_in_worker_thread(user_id, monkeypatch):
    rendered = object()
    to_thread = AsyncMock(return_value=rendered)
    monkeypatch.setattr(analytics.asyncio, "to_thread", to_thread)
    db = SimpleNamespace(get_study_logs=AsyncMock(return_value=[]))
    message = SimpleNamespace(reply_photo=AsyncMock())
    end = today_local()
    start = end - timedelta(days=6)

    await analytics._send_study_chart(message, db, user_id, start, end)

    assert to_thread.await_args.args[0] is charts.study_chart
    assert to_thread.await_args.kwargs == {"days": 7, "end_date": end}
    message.reply_photo.assert_awaited_once_with(
        rendered, caption="📖 Study — Last 7 Days"
    )
