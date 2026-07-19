"""Tests for bot configuration."""

import importlib
import os
from pathlib import Path
from unittest import mock

import pytest


def test_db_path_default():
    """Test that DB_PATH defaults to a path relative to the project root."""
    mock_env = {"BOT_TOKEN": "test", "ALLOWED_USER_IDS": "1"}
    with mock.patch.dict(os.environ, mock_env, clear=True), mock.patch("dotenv.load_dotenv"):
        # Reload the config module to pick up the mocked environment
        import bot.config
        importlib.reload(bot.config)
        
        expected_path = Path(bot.config.__file__).resolve().parent.parent / "ledger.db"
        assert Path(bot.config.DB_PATH) == expected_path


def test_reminder_hour_valid():
    """Test that a valid REMINDER_HOUR is accepted."""
    mock_env = {"BOT_TOKEN": "test", "ALLOWED_USER_IDS": "1", "REMINDER_HOUR": "10"}
    with mock.patch.dict(os.environ, mock_env, clear=True), mock.patch("dotenv.load_dotenv"):
        import bot.config
        importlib.reload(bot.config)
        assert bot.config.REMINDER_HOUR == 10
        assert bot.config.REMINDER_TIME.hour == 10


@pytest.mark.parametrize("invalid_hour", ["24", "-1", "abc", ""])
def test_reminder_hour_invalid(invalid_hour):
    """Test that invalid REMINDER_HOUR raises RuntimeError."""
    mock_env = {"BOT_TOKEN": "test", "ALLOWED_USER_IDS": "1", "REMINDER_HOUR": invalid_hour}
    with mock.patch.dict(os.environ, mock_env, clear=True), mock.patch("dotenv.load_dotenv"):
        import bot.config
        with pytest.raises(RuntimeError):
            importlib.reload(bot.config)
