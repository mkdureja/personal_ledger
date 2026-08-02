"""Regression tests for logging-handler validation and conversation safety."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.error import NetworkError
from telegram.ext import CommandHandler, ConversationHandler, TypeHandler

from bot.config import ALLOWED_USER_IDS
from bot.handlers import common, diet, gym, habits, study
from bot.handlers.common import (

    activate_conversation,
    authorized_callback,
    cancel_command,
    undo_command,
    escape_html,
    parse_float,
    parse_int,
    reply_html,
    timeout_handler,
)

def assert_log_diet(db_mock, user_id, meal_type, food_items, calories, protein_g=None, carbs_g=None, fat_g=None, source=None):
    db_mock.log_diet_with_items.assert_awaited_once()
    args, kwargs = db_mock.log_diet_with_items.call_args
    assert args[0] == user_id
    assert args[1] == meal_type
    assert kwargs.get("source") == source
    assert len(args[2]) == 1
    child = args[2][0]
    assert child["source_type"] == "freetext"
    assert child["display_name"] == food_items
    assert child["calories"] == calories
    assert child.get("protein_g") == protein_g
    assert child.get("carbs_g") == carbs_g
    assert child.get("fat_g") == fat_g


def _allowed_user() -> SimpleNamespace:
    return SimpleNamespace(id=next(iter(ALLOWED_USER_IDS)))


def _context(db: object, user_data: dict | None = None, args: list[str] | None = None):
    return SimpleNamespace(
        bot_data={"db": db},
        user_data=user_data if user_data is not None else {},
        args=args or [],
    )


def _message(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(text=text, reply_text=AsyncMock())


def test_integer_validator_rejects_invalid_ranges_and_unicode_numeric() -> None:
    assert parse_int("12", "Count", max_value=12) == (12, None)

    for raw in ("0", "-1", "13", "²"):
        value, error = parse_int(raw, "Count", max_value=12)
        assert value is None
        assert error


def test_float_validator_rejects_non_finite_and_maximum_overflow() -> None:
    assert parse_float("12.5", "Weight", max_value=20) == (12.5, None)

    for raw in ("nan", "inf", "-inf", "1e9999", "20.1"):
        value, error = parse_float(raw, "Weight", max_value=20)
        assert value is None
        assert error


@pytest.mark.parametrize(
    ("conversation", "command"),
    [
        (study.study_conv_handler, "study"),
        (gym.gym_conv_handler, "gym"),
        (diet.diet_conv_handler, "diet"),
        (habits.habits_setup_conv_handler, "habits"),
    ],
)
def test_guided_flows_handle_same_command_reentry(
    conversation: ConversationHandler, command: str
) -> None:
    """An active conversation must not silently drop its own command."""
    assert any(
        isinstance(handler, CommandHandler) and command in handler.commands
        for handler in conversation.fallbacks
    )


@pytest.mark.parametrize(
    "conversation",
    [
        study.study_conv_handler,
        gym.gym_conv_handler,
        diet.diet_conv_handler,
        habits.habits_setup_conv_handler,
    ],
)
def test_conversation_timeout_accepts_callback_updates(
    conversation: ConversationHandler,
) -> None:
    """Callback-driven flows must clear their marker when the timer expires."""
    callback_update = Update(update_id=1, callback_query=SimpleNamespace())
    timeout_handlers = conversation.states[ConversationHandler.TIMEOUT]

    assert any(
        isinstance(handler, TypeHandler) and handler.check_update(callback_update)
        for handler in timeout_handlers
    )


def test_html_builders_escape_dynamic_values() -> None:
    assert escape_html("<b>&\"'") == "&lt;b&gt;&amp;&quot;&#x27;"

    study_text = study._confirmation("Math <&>", 30, "read <i>this</i> & that")
    gym_text = gym._exercise_summary("Rows <heavy> & slow", 3, 8, 42.5)
    diet_text = diet._confirmation("lunch", "dal < rice & veg", 650)

    assert "<b>Math &lt;&amp;&gt;</b>" in study_text
    assert "&lt;i&gt;this&lt;/i&gt; &amp; that" in study_text
    assert "Rows &lt;heavy&gt; &amp; slow" in gym_text
    assert "dal &lt; rice &amp; veg" in diet_text


@pytest.mark.asyncio
async def test_reply_html_sets_explicit_parse_mode() -> None:
    message = _message()

    await reply_html(message, "<b>Safe</b>", disable_notification=True)

    message.reply_text.assert_awaited_once_with(
        "<b>Safe</b>",
        disable_notification=True,
        parse_mode=ParseMode.HTML,
    )


@pytest.mark.asyncio
async def test_authorized_callback_rejects_disallowed_user() -> None:
    handler = AsyncMock(return_value=123)
    wrapped = authorized_callback(handler)
    query = SimpleNamespace(answer=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=max(ALLOWED_USER_IDS) + 1),
        callback_query=query,
    )

    result = await wrapped(update, SimpleNamespace())

    assert result is None
    handler.assert_not_awaited()
    query.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_cancel_preserves_other_conversation_state() -> None:
    message = _message()
    state = {"study_subject": "Math", "diet_food_items": "dal"}
    context = _context(SimpleNamespace(), state)
    update = SimpleNamespace(
        message=message, effective_message=message,
        effective_chat=SimpleNamespace(id=10),
    )
    activate_conversation(update, context, "study")

    result = await cancel_command(update, context)

    assert result == ConversationHandler.END
    assert context.user_data == {"diet_food_items": "dal"}


@pytest.mark.asyncio
async def test_second_guided_flow_is_rejected_while_study_is_active() -> None:
    db = SimpleNamespace(ensure_user=AsyncMock(), log_gym=AsyncMock())
    context = _context(db)
    user = SimpleNamespace(id=_allowed_user().id, username="tester", first_name="Test")
    chat = SimpleNamespace(id=10)

    study_message = _message()
    study_update = SimpleNamespace(
        message=study_message,
        effective_message=study_message,
        effective_user=user,
        effective_chat=chat,
    )
    assert await study.study_command(study_update, context) == study.SUBJECT

    gym_message = _message()
    gym_update = SimpleNamespace(
        message=gym_message,
        effective_message=gym_message,
        effective_user=user,
        effective_chat=chat,
    )
    result = await gym.gym_command(gym_update, context)

    assert result == ConversationHandler.END
    db.log_gym.assert_not_awaited()
    assert "already in a study session" in gym_message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_cancel_in_another_chat_does_not_release_active_flow() -> None:
    state = {"study_subject": "Math"}
    context = _context(SimpleNamespace(), state)
    owner_update = SimpleNamespace(effective_chat=SimpleNamespace(id=10))
    activate_conversation(owner_update, context, "study")

    other_message = _message()
    other_update = SimpleNamespace(
        message=other_message,
        effective_chat=SimpleNamespace(id=20),
    )
    await cancel_command(other_update, context)

    assert context.user_data["study_subject"] == "Math"
    assert "_ledger_active_conversation" in context.user_data
    assert "No active guided log" in other_message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_undo_is_blocked_during_active_guided_flow() -> None:
    db = SimpleNamespace(peek_last=AsyncMock())
    context = _context(db)
    message = _message()
    update = SimpleNamespace(
        message=message, effective_message=message,
        effective_user=_allowed_user(),
        effective_chat=SimpleNamespace(id=10),
    )
    activate_conversation(update, context, "gym")

    await undo_command(update, context)

    db.peek_last.assert_not_awaited()
    assert "before /undo" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_undo_is_blocked_when_flow_is_active_in_another_chat() -> None:
    db = SimpleNamespace(peek_last=AsyncMock())
    context = _context(db)
    owner_update = SimpleNamespace(effective_chat=SimpleNamespace(id=10))
    activate_conversation(owner_update, context, "gym")
    message = _message()
    other_chat_update = SimpleNamespace(
        message=message, effective_message=message,
        effective_user=_allowed_user(),
        effective_chat=SimpleNamespace(id=20),
    )

    await undo_command(other_chat_update, context)

    db.peek_last.assert_not_awaited()
    assert "where it started" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receiver", "text", "expected_state"),
    [
        (study.receive_subject, "S" * (study.MAX_SUBJECT_LENGTH + 1), study.SUBJECT),
        (
            # The gym flow no longer types an exercise name to pick one, but a
            # user's *own* exercise is still typed, and the same cap applies.
            gym.receive_new_exercise_name,
            "E" * (gym.MAX_EXERCISE_NAME_LENGTH + 1),
            gym.NEW_NAME,
        ),
        (
            diet.receive_food_items,
            "F" * (diet.MAX_FOOD_ITEMS_LENGTH + 1),
            diet.FOOD_ITEMS,
        ),
    ],
)
async def test_guided_text_fields_enforce_message_safe_limits(
    receiver, text: str, expected_state: int
) -> None:
    message = _message(text)
    update = SimpleNamespace(message=message)
    context = _context(SimpleNamespace())

    result = await receiver(update, context)

    assert result == expected_state
    assert context.user_data == {}
    assert "too long" in message.reply_text.await_args.args[0]




@pytest.mark.asyncio
async def test_study_state_survives_database_failure() -> None:
    db = SimpleNamespace(log_study=AsyncMock(side_effect=RuntimeError("db down")))
    state = {"study_subject": "Math", "study_duration": 45, "diet_food_items": "dal"}
    context = _context(db, state)
    message = _message("notes")
    update = SimpleNamespace(message=message, effective_message=message, effective_user=_allowed_user())

    with pytest.raises(RuntimeError, match="db down"):
        await study.receive_notes(update, context)

    assert context.user_data == state
    message.reply_text.assert_not_awaited()




@pytest.mark.asyncio
async def test_diet_state_survives_database_failure() -> None:
    db = SimpleNamespace(log_diet_with_items=AsyncMock(side_effect=RuntimeError("db down")))
    state = {
        "diet_meal_type": "lunch",
        "diet_food_items": "dal",
        "study_subject": "Math",
    }
    context = _context(db, state)
    message = _message()
    update = SimpleNamespace(message=message, effective_message=message, effective_user=_allowed_user())

    with pytest.raises(RuntimeError, match="db down"):
        await diet._save_diet(update, context, 650)

    assert context.user_data == state
    message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_study_confirmation_failure_still_ends_persisted_flow() -> None:
    db = SimpleNamespace(log_study=AsyncMock())
    state = {"study_subject": "Math", "study_duration": 45}
    context = _context(db, state)
    message = _message("notes")
    message.reply_text.side_effect = NetworkError("offline")
    update = SimpleNamespace(
        message=message, effective_message=message,
        effective_user=_allowed_user(),
        effective_chat=SimpleNamespace(id=10),
    )
    activate_conversation(update, context, "study")

    result = await study.receive_notes(update, context)

    assert result == ConversationHandler.END
    db.log_study.assert_awaited_once()
    assert context.user_data == {}




@pytest.mark.asyncio
async def test_diet_confirmation_failure_still_ends_persisted_flow() -> None:
    db = SimpleNamespace(log_diet_with_items=AsyncMock())
    state = {"diet_meal_type": "lunch", "diet_food_items": "dal"}
    context = _context(db, state)
    message = _message()
    message.reply_text.side_effect = NetworkError("offline")
    update = SimpleNamespace(
        message=message, effective_message=message,
        effective_user=_allowed_user(),
        effective_chat=SimpleNamespace(id=10),
    )
    activate_conversation(update, context, "diet")

    result = await diet._save_diet(update, context, 650)

    assert result == ConversationHandler.END
    db.log_diet_with_items.assert_awaited_once()
    assert context.user_data == {}


@pytest.mark.asyncio
async def test_timeout_delivery_failure_still_clears_flow() -> None:
    context = _context(SimpleNamespace(), {"study_subject": "Math"})
    message = _message()
    message.reply_text.side_effect = NetworkError("offline")
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=10),
    )
    activate_conversation(update, context, "study")

    result = await timeout_handler(update, context)

    assert result == ConversationHandler.END
    assert context.user_data == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("calorie_token", ["0", "²"])
async def test_diet_shortcut_rejects_invalid_numeric_calorie_tokens(
    calorie_token: str,
) -> None:
    db = SimpleNamespace(ensure_user=AsyncMock(), log_diet_with_items=AsyncMock())
    context = _context(db, args=["lunch", "dal", calorie_token])
    message = _message()
    user = SimpleNamespace(id=_allowed_user().id, username="tester", first_name="Test")
    update = SimpleNamespace(message=message, effective_message=message, effective_user=user)

    result = await diet.diet_command(update, context)

    assert result == ConversationHandler.END
    db.log_diet_with_items.assert_not_awaited()
    message.reply_text.assert_awaited_once()




@pytest.mark.asyncio
async def test_stale_diet_callback_does_not_set_meal_type() -> None:
    query_message = SimpleNamespace(message_id=19, reply_text=AsyncMock())
    query = SimpleNamespace(
        data=f"meal_{_allowed_user().id}_lunch",
        message=query_message,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=_allowed_user(),
        callback_query=query,
        effective_chat=SimpleNamespace(id=10, type=ChatType.PRIVATE),
    )
    state = {"diet_meal_message_id": 20, "study_subject": "Math"}
    context = _context(SimpleNamespace(), state)

    result = await diet.receive_meal_type(update, context)

    assert result == diet.MEAL_TYPE
    assert context.user_data == state
    query.answer.assert_awaited_once_with("This meal menu has expired.", show_alert=True)
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    query_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_stale_gym_callback_only_retires_button() -> None:
    query_message = SimpleNamespace(message_id=9, reply_text=AsyncMock())
    query = SimpleNamespace(
        data=f"gym_{_allowed_user().id}_yes",
        message=query_message,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=_allowed_user(),
        callback_query=query,
        effective_chat=SimpleNamespace(id=10, type=ChatType.PRIVATE),
    )
    state = {"study_subject": "Math", "gym_exercises": ["existing"]}
    expected_state = {"study_subject": "Math", "gym_exercises": ["existing"]}
    context = _context(SimpleNamespace(), state)

    result = await gym.stale_gym_callback(update, context)

    assert result is None
    assert context.user_data == expected_state
    query.answer.assert_awaited_once_with(
        "This workout prompt has expired.", show_alert=True
    )
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    query_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_stale_meal_callback_only_retires_button() -> None:
    query_message = SimpleNamespace(message_id=19, reply_text=AsyncMock())
    query = SimpleNamespace(
        data=f"meal_{_allowed_user().id}_lunch",
        message=query_message,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=_allowed_user(),
        callback_query=query,
        effective_chat=SimpleNamespace(id=10, type=ChatType.PRIVATE),
    )
    state = {"study_subject": "Math", "diet_meal_type": "breakfast"}
    expected_state = {"study_subject": "Math", "diet_meal_type": "breakfast"}
    context = _context(SimpleNamespace(), state)

    result = await diet.stale_meal_callback(update, context)

    assert result is None
    assert context.user_data == expected_state
    query.answer.assert_awaited_once_with(
        "This meal menu has expired.", show_alert=True
    )
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    query_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "callback_data"),
    [
        (gym.stale_gym_callback, "gym_{owner}_yes"),
        (diet.stale_meal_callback, "meal_{owner}_lunch"),
    ],
)
async def test_allowed_user_cannot_retire_another_users_prompt(
    handler, callback_data: str, monkeypatch
) -> None:
    owner_id = _allowed_user().id
    clicker_id = owner_id + 1
    monkeypatch.setattr(
        common, "ALLOWED_USER_IDS", frozenset({owner_id, clicker_id})
    )
    query = SimpleNamespace(
        data=callback_data.format(owner=owner_id),
        message=SimpleNamespace(message_id=9, reply_text=AsyncMock()),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=clicker_id),
        callback_query=query,
        effective_chat=SimpleNamespace(id=clicker_id, type=ChatType.PRIVATE),
    )

    await handler(update, _context(SimpleNamespace()))

    assert "another user" in query.answer.await_args.args[0]
    query.edit_message_reply_markup.assert_not_awaited()
