"""Tests for the global error handler."""

import logging
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from telegram.ext import ContextTypes

from bot.handlers.common import error_handler

def create_update(text="", user_id=1):
    return SimpleNamespace(
        effective_message=SimpleNamespace(reply_text=AsyncMock()),
        effective_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(reply_text=AsyncMock())
    )


pytestmark = pytest.mark.asyncio


async def test_error_handler_logs_error_and_notifies_user(user_id, caplog):
    """Test error handler logs exception and replies to user."""
    update = create_update("Some message", user_id=user_id)
    context = SimpleNamespace(error=ValueError("Something broke!"))
    
    with caplog.at_level(logging.ERROR), patch("bot.handlers.common.Update", type(update)):
        await error_handler(update, context)
    
    assert "Exception while handling an update" in caplog.text
    assert "ValueError" in caplog.text
    
    update.effective_message.reply_text.assert_called_once()
    args, _ = update.effective_message.reply_text.call_args
    assert "Something went wrong" in args[0]


async def test_error_handler_handles_non_update_objects(caplog):
    """Test error handler doesn't crash when update is not an Update object."""
    update = object()  # Not a telegram Update
    context = SimpleNamespace(error=KeyError("Missing key"))
    
    with caplog.at_level(logging.ERROR):
        await error_handler(update, context)
        
    assert "Exception while handling an update" in caplog.text
    assert "KeyError" in caplog.text
