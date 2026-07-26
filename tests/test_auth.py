"""Tests for authorization: allowlist + private-chat restriction."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType

from bot.handlers.common import AUTH_FILTER, authorized_callback

_ALLOWED_ID = 123456789  # matches conftest ALLOWED_USER_IDS


def test_auth_filter_includes_private_chat_constraint():
    assert "private" in repr(AUTH_FILTER).lower()


def _callback_update(chat_type, user_id=_ALLOWED_ID):
    query = SimpleNamespace(answer=AsyncMock(), data="x")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        callback_query=query,
        effective_chat=SimpleNamespace(type=chat_type),
    )
    return update, query


@pytest.mark.asyncio
async def test_authorized_callback_allows_private_chat():
    calls = []

    @authorized_callback
    async def handler(update, context):
        calls.append(True)

    update, _ = _callback_update(ChatType.PRIVATE)
    await handler(update, SimpleNamespace())
    assert calls == [True]


@pytest.mark.asyncio
async def test_authorized_callback_denies_group_chat():
    calls = []

    @authorized_callback
    async def handler(update, context):
        calls.append(True)

    update, query = _callback_update(ChatType.GROUP)
    await handler(update, SimpleNamespace())
    assert calls == []  # handler never ran
    query.answer.assert_awaited()  # spinner acknowledged, silent denial


@pytest.mark.asyncio
async def test_authorized_callback_denies_non_allowlisted_user():
    calls = []

    @authorized_callback
    async def handler(update, context):
        calls.append(True)

    update, query = _callback_update(ChatType.PRIVATE, user_id=_ALLOWED_ID + 1)
    await handler(update, SimpleNamespace())
    assert calls == []
    query.answer.assert_awaited()


@pytest.mark.asyncio
async def test_authorized_callback_fails_closed_on_missing_chat_context():
    """A callback without a resolvable chat/type is denied, not allowed."""
    calls = []

    @authorized_callback
    async def handler(update, context):
        calls.append(True)

    query = SimpleNamespace(answer=AsyncMock(), data="x")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=_ALLOWED_ID),
        callback_query=query,
        effective_chat=None,  # missing chat context must fail closed
    )
    await handler(update, SimpleNamespace())
    assert calls == []  # handler never ran
    query.answer.assert_awaited()  # spinner acknowledged, silent denial
