"""Routing tests for direct menu-tap entry (tap-first Phase 1 / P0).

These drive the *real* handler table from ``build_application()`` and assert
which handler would consume a given Update — the genuine group order,
AUTH_FILTER, and callback patterns — without any network loop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User

from bot.handlers.diet import diet_conv_handler
from bot.handlers.gym import gym_conv_handler
from bot.handlers.study import study_conv_handler
from bot.main import build_application

MANOJ = 123456789  # matches conftest ALLOWED_USER_IDS

_BOT = MagicMock()
_BOT.username = "LedgerTestBot"


def _first_handler(app, update):
    """Return the first registered handler that would consume ``update``."""
    for group in sorted(app.handlers):
        for handler in app.handlers[group]:
            if handler.check_update(update):
                return handler
    return None


def _handler_name(handler) -> str | None:
    callback = getattr(handler, "callback", None)
    return getattr(callback, "__name__", None)


def _command_update(user_id: int, text: str) -> Update:
    chat = Chat(id=user_id, type="private")
    user = User(id=user_id, is_bot=False, first_name="U")
    command = text.split()[0]
    entities = [
        MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(command))
    ]
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
        entities=entities,
    )
    message.set_bot(_BOT)
    return Update(update_id=1, message=message)


def _callback_update(user_id: int, data: str) -> Update:
    chat = Chat(id=user_id, type="private")
    user = User(id=user_id, is_bot=False, first_name="U")
    message = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=chat, from_user=user
    )
    message.set_bot(_BOT)
    query = CallbackQuery(
        id="1", from_user=user, chat_instance="ci", data=data, message=message
    )
    query.set_bot(_BOT)
    return Update(update_id=1, callback_query=query)


def test_category_taps_enter_their_conversation_handlers():
    """Study/Gym/Diet taps are consumed by their ConversationHandler (before the
    generic menu_callback), so they enter the guided flow directly."""
    app = build_application()
    assert _first_handler(app, _callback_update(MANOJ, "menu_diet")) is diet_conv_handler
    assert _first_handler(app, _callback_update(MANOJ, "menu_study")) is study_conv_handler
    assert _first_handler(app, _callback_update(MANOJ, "menu_gym")) is gym_conv_handler


def test_non_conversation_taps_still_reach_menu_callback():
    """Habits/Analytics taps are not conversations and remain served by menu_callback."""
    app = build_application()
    for data in ("menu_habits", "menu_analytics"):
        handler = _first_handler(app, _callback_update(MANOJ, data))
        assert _handler_name(handler) == "menu_callback"


def test_commands_still_enter_the_same_conversations():
    """The command entry points are unchanged by adding callback entry points."""
    app = build_application()
    assert _first_handler(app, _command_update(MANOJ, "/diet")) is diet_conv_handler
    assert _first_handler(app, _command_update(MANOJ, "/study")) is study_conv_handler
    assert _first_handler(app, _command_update(MANOJ, "/gym")) is gym_conv_handler
