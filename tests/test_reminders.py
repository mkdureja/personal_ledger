"""Reminder chunking tests for Telegram's message-size limit."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers.common import escape_html
from bot.handlers.reminders import (
    _REMINDER_MESSAGE_LIMIT,
    _build_reminder_messages,
    _telegram_text_units,
    daily_reminder,
)


def test_many_habits_are_escaped_and_preserved_across_messages():
    habit_names = [f"Habit <{index}> & {'x' * 50}" for index in range(200)]

    messages = _build_reminder_messages(habit_names)

    combined = "\n".join(messages)
    assert len(messages) > 1
    assert all(len(message) < 4096 for message in messages)
    assert all(
        _telegram_text_units(message) <= _REMINDER_MESSAGE_LIMIT
        for message in messages
    )
    assert all(escape_html(name) in combined for name in habit_names)
    assert combined.count("Tap /habits to check them off!") == 1


def test_one_legacy_long_name_is_split_without_data_or_entity_loss():
    habit_name = "😀<&x" * 3000

    messages = _build_reminder_messages([habit_name])

    assert len(messages) > 1
    assert all(len(message) < 4096 for message in messages)
    assert all(
        _telegram_text_units(message) <= _REMINDER_MESSAGE_LIMIT
        for message in messages
    )
    assert sum(message.count("😀") for message in messages) == 3000
    assert sum(message.count("&lt;") for message in messages) == 3000
    assert sum(message.count("&amp;") for message in messages) == 3000
    assert sum(message.count("x") for message in messages) == 3000


@pytest.mark.asyncio
async def test_daily_reminder_sends_every_chunk_as_html(user_id):
    habit_names = [f"Habit <{index}> & {'z' * 60}" for index in range(180)]
    db = SimpleNamespace(
        get_users_with_unchecked_habits=AsyncMock(
            return_value={user_id: habit_names}
        ),
        get_active_habits=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(bot_data={"db": db}, bot=bot)

    await daily_reminder(context)

    calls = bot.send_message.await_args_list
    assert len(calls) > 1
    combined = "\n".join(call.kwargs["text"] for call in calls)
    assert all(call.kwargs["chat_id"] == user_id for call in calls)
    assert all(call.kwargs["parse_mode"] == "HTML" for call in calls)
    assert all(len(call.kwargs["text"]) < 4096 for call in calls)
    assert all(escape_html(name) in combined for name in habit_names)
