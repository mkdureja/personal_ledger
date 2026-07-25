"""Bounded retry for scheduled message delivery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter

from bot.handlers.reminders import _MAX_SEND_ATTEMPTS, _send_with_retry


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient_failures(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    send = AsyncMock(side_effect=[NetworkError("x"), RetryAfter(0), None])
    bot = SimpleNamespace(send_message=send)

    assert await _send_with_retry(bot, 1, "hi") is True
    assert send.await_count == 3


@pytest.mark.asyncio
async def test_retry_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    send = AsyncMock(side_effect=NetworkError("down"))
    bot = SimpleNamespace(send_message=send)

    assert await _send_with_retry(bot, 1, "hi") is False
    assert send.await_count == _MAX_SEND_ATTEMPTS


@pytest.mark.asyncio
async def test_permanent_error_is_not_retried(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    send = AsyncMock(side_effect=BadRequest("bad chat"))
    bot = SimpleNamespace(send_message=send)

    assert await _send_with_retry(bot, 1, "hi") is False
    assert send.await_count == 1
