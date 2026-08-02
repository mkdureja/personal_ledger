"""Tests for /start, /help, /menu commands and menu callback."""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.constants import ChatType
from telegram.ext import ContextTypes

from bot.handlers.start import start_command, help_command, menu_command, menu_callback

def create_update(text="", user_id=1, username="", first_name=""):
    # One shared message object: Home replies through effective_message, while the
    # older command handlers used update.message. They must be the same mock for
    # assertions to see either path.
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id, username=username, first_name=first_name),
        message=message,
    )

def create_callback_query(data, user_id=1):
    return SimpleNamespace(
        callback_query=SimpleNamespace(
            data=data,
            answer=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
            edit_message_reply_markup=AsyncMock()
        ),
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
    )


pytestmark = pytest.mark.asyncio


async def test_start_command_onboards_then_shows_home(db, user_id):
    """A first-ever /start onboards, prepends the welcome, and shows the buttons."""
    update = create_update("/start", user_id=user_id, username="testuser", first_name="Test")
    context = SimpleNamespace(bot_data={"db": db}, user_data={})

    await start_command(update, context)

    args, kwargs = update.message.reply_text.call_args_list[0]
    assert "Welcome to <b>Ledger</b>" in args[0]
    # Home itself, on the same message, so the actions arrive immediately.
    assert "here's today" in args[0]
    assert kwargs.get("reply_markup") is not None
    assert kwargs.get("parse_mode") == "HTML"

    # Both onboarding calls are preserved: the user row and an opted-out
    # settings row.
    cursor = await db.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = await cursor.fetchone()
    assert row is not None
    assert row["first_name"] == "Test"
    settings = await db.get_user_settings(user_id)
    assert settings is not None
    assert not settings["reminders_enabled"]


async def test_start_command_returning_user_skips_the_welcome(db, user_id):
    """The welcome is one-time; a returning /start is plain Home."""
    update = create_update("/start", user_id=user_id, first_name="Test")
    context = SimpleNamespace(bot_data={"db": db}, user_data={})
    await start_command(update, context)

    second = create_update("/start", user_id=user_id, first_name="Test")
    await start_command(second, SimpleNamespace(bot_data={"db": db}, user_data={}))

    text = second.message.reply_text.call_args_list[0].args[0]
    assert "Welcome to <b>Ledger</b>" not in text
    assert "here's today" in text


async def test_start_command_escapes_a_live_flow(db, user_id):
    """/start mid-flow opens Home and ends the flow, naming what it dropped.

    This asserted the opposite until live testing showed the cost: every way
    back to Home refused while a flow was active, so the user's only escape was
    a command they had to already know.
    """
    from bot.handlers.common import activate_conversation, active_conversation_flow

    update = create_update("/start", user_id=user_id, first_name="Test")
    update.effective_chat = SimpleNamespace(id=user_id, type=ChatType.PRIVATE)
    context = SimpleNamespace(bot_data={"db": db}, user_data={})
    activate_conversation(update, context, "diet")
    context.user_data["diet_food_items"] = "oats"

    await start_command(update, context)

    said = " ".join(
        str(call.args[0])
        for call in update.message.reply_text.call_args_list
        if call.args
    )
    assert "here's today" in said
    assert "Dropped your unsaved meal" in said
    assert active_conversation_flow(context) is None


async def test_help_command(user_id):
    """Test /help sends command reference."""
    update = create_update("/help", user_id=user_id)
    context = SimpleNamespace()
    
    await help_command(update, context)
    
    update.message.reply_text.assert_called_once()
    args, kwargs = update.message.reply_text.call_args
    assert "<b>All Commands</b>" in args[0]
    assert "/study" in args[0]
    assert "/gym" in args[0]
    assert kwargs.get("parse_mode") == "HTML"


async def test_menu_command_is_home(db, user_id):
    """/menu and Home are one surface, so both habits lead to the same place."""
    update = create_update("/menu", user_id=user_id, first_name="Test")
    context = SimpleNamespace(bot_data={"db": db}, user_data={})

    await menu_command(update, context)

    args, kwargs = update.message.reply_text.call_args_list[0]
    assert "here's today" in args[0]
    assert kwargs.get("reply_markup") is not None
    assert kwargs.get("parse_mode") == "HTML"


async def test_home_command_is_the_same_surface(db, user_id):
    from bot.handlers.home import home_command

    update = create_update("/home", user_id=user_id, first_name="Test")
    context = SimpleNamespace(bot_data={"db": db}, user_data={})

    await home_command(update, context)

    assert "here's today" in update.message.reply_text.call_args_list[0].args[0]


async def test_menu_callback_recent(db, user_id, monkeypatch):
    """The Home 🗒️ Recent tap renders recent entries without an update.message."""
    update = create_callback_query("menu_recent", user_id=user_id)
    context = SimpleNamespace(bot_data={"db": db}, user_data={})

    await menu_callback(update, context)

    update.callback_query.answer.assert_called_once()
    text = update.callback_query.message.reply_text.call_args.args[0]
    assert "No recent entries yet" in text


async def test_menu_callback_analytics(db, user_id):
    """The Analytics tap opens the analytics report sub-menu."""
    update = create_callback_query("menu_analytics", user_id=user_id)
    context = SimpleNamespace(bot_data={"db": db}, user_data={})

    await menu_callback(update, context)

    update.callback_query.answer.assert_called_once()
    update.callback_query.message.reply_text.assert_called_once()
    args, _ = update.callback_query.message.reply_text.call_args
    assert "Choose a report" in args[0]


async def test_menu_callback_habits(db, user_id, monkeypatch):
    """The Habits tap shows the habit checklist."""
    mock_checklist = AsyncMock()
    monkeypatch.setattr("bot.handlers.habits.show_habits_checklist", mock_checklist)

    update = create_callback_query("menu_habits", user_id=user_id)
    context = SimpleNamespace(bot_data={"db": db}, user_data={})

    await menu_callback(update, context)

    update.callback_query.answer.assert_called_once()
    mock_checklist.assert_awaited_once()


async def test_menu_callback_conversation_categories_not_served_here(user_id):
    """Study/Gym/Diet taps are consumed by their ConversationHandler entry
    points (registered before menu_callback), so if one ever reaches this
    handler it is treated as an expired button rather than served here."""
    for action in ("menu_study", "menu_gym", "menu_diet"):
        update = create_callback_query(action, user_id=user_id)
        context = SimpleNamespace(user_data={})

        await menu_callback(update, context)

        update.callback_query.answer.assert_called_once_with(
            "This menu is no longer valid.", show_alert=True
        )


async def test_menu_callback_invalid_action(user_id):
    """Test invalid menu action alerts user and removes keyboard."""
    update = create_callback_query("invalid_menu", user_id=user_id)
    context = SimpleNamespace(user_data={})

    await menu_callback(update, context)
    
    update.callback_query.answer.assert_called_once_with("This menu is no longer valid.", show_alert=True)
    update.callback_query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)
