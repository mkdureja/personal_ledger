"""Tests for /start, /help, /menu commands and menu callback."""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.ext import ContextTypes

from bot.handlers.start import start_command, help_command, menu_command, menu_callback

def create_update(text="", user_id=1, username="", first_name=""):
    return SimpleNamespace(
        effective_message=SimpleNamespace(reply_text=AsyncMock()),
        effective_user=SimpleNamespace(id=user_id, username=username, first_name=first_name),
        message=SimpleNamespace(reply_text=AsyncMock())
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
    )


pytestmark = pytest.mark.asyncio


async def test_start_command(db, user_id):
    """Test /start creates user and sends welcome."""
    update = create_update("/start", user_id=user_id, username="testuser", first_name="Test")
    context = SimpleNamespace(bot_data={"db": db})
    
    await start_command(update, context)
    
    update.message.reply_text.assert_called_once()
    args, kwargs = update.message.reply_text.call_args
    assert "Hey Test!" in args[0]
    assert "Welcome to <b>Ledger</b>" in args[0]
    assert kwargs.get("parse_mode") == "HTML"
    
    # Verify user was created in DB
    cursor = await db.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = await cursor.fetchone()
    assert row is not None
    assert row["first_name"] == "Test"


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


async def test_menu_command(user_id):
    """Test /menu sends keyboard."""
    update = create_update("/menu", user_id=user_id)
    context = SimpleNamespace()
    
    await menu_command(update, context)
    
    update.message.reply_text.assert_called_once()
    args, kwargs = update.message.reply_text.call_args
    assert "<b>Main Menu</b>" in args[0]
    assert "reply_markup" in kwargs
    assert kwargs.get("parse_mode") == "HTML"


async def test_menu_callback_valid_actions(db, user_id, monkeypatch):
    """Test valid menu actions send corresponding help or checklists."""
    # Mock habits handler to avoid needing a full checklist setup
    from unittest.mock import AsyncMock
    monkeypatch.setattr("bot.handlers.habits.show_habits_checklist", AsyncMock())
    
    actions = {
        "menu_study": "Send /study to start logging",
        "menu_gym": "Send /gym to start logging",
        "menu_diet": "Send /diet to start logging",
        "menu_analytics": "Choose a report",
    }
    
    for action, expected_text in actions.items():
        update = create_callback_query(action, user_id=user_id)
        context = SimpleNamespace(bot_data={"db": db})
        
        await menu_callback(update, context)
        
        update.callback_query.answer.assert_called_once()
        update.callback_query.message.reply_text.assert_called_once()
        args, _ = update.callback_query.message.reply_text.call_args
        assert expected_text in args[0]


async def test_menu_callback_invalid_action(user_id):
    """Test invalid menu action alerts user and removes keyboard."""
    update = create_callback_query("invalid_menu", user_id=user_id)
    context = SimpleNamespace()
    
    await menu_callback(update, context)
    
    update.callback_query.answer.assert_called_once_with("This menu is no longer valid.", show_alert=True)
    update.callback_query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)
