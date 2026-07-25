"""Tests that the bot token never reaches log output."""

from __future__ import annotations

import logging

from bot.main import _TokenRedactingFilter

_SECRET = "123456:SUPERSECRETTOKEN"


def _record(msg, args=None):
    return logging.LogRecord("test", logging.INFO, __file__, 1, msg, args, None)


def test_filter_redacts_token_in_message():
    filt = _TokenRedactingFilter(_SECRET)
    record = _record(f"HTTP Request: GET https://api.telegram.org/bot{_SECRET}/getMe")
    assert filt.filter(record) is True
    assert _SECRET not in record.getMessage()
    assert "***" in record.getMessage()


def test_filter_redacts_token_in_args():
    filt = _TokenRedactingFilter(_SECRET)
    record = _record("HTTP Request: %s", (f"https://api.telegram.org/bot{_SECRET}/x",))
    assert filt.filter(record) is True
    assert _SECRET not in record.getMessage()
    assert "***" in record.getMessage()


def test_filter_is_noop_without_secret():
    filt = _TokenRedactingFilter("")
    record = _record("nothing to redact")
    assert filt.filter(record) is True
    assert record.getMessage() == "nothing to redact"


def test_http_transport_loggers_are_quieted():
    # Importing bot.main (above) applies the level changes at module load.
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING
