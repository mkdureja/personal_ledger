"""Durable, resumable reminder chunk delivery (implementation_plan Phase 7).

Transient outages and restarts must neither lose nor duplicate a successfully
delivered chunk, and delivery state stays tenant-scoped.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio
from telegram.error import Forbidden, NetworkError

from bot.database import DatabaseManager
from bot.handlers import reminders as reminders_module
from bot.handlers.reminders import _deliver_chunks, daily_reminder

MANOJ = 111
RATIKA = 222
DATE = "2026-07-18"


class _Bot:
    """A fake bot; fails sends to `fail_users` or of `fail_texts`."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail_users: set[int] = set()
        self.fail_texts: set[str] = set()

    async def send_message(self, chat_id, text, parse_mode=None):
        if chat_id in self.fail_users or text in self.fail_texts:
            raise NetworkError("transient outage")
        self.sent.append((chat_id, text))

    def texts_for(self, user_id: int) -> list[str]:
        return [text for (cid, text) in self.sent if cid == user_id]


@pytest_asyncio.fixture
async def db():
    mgr = DatabaseManager(":memory:")
    await mgr.connect()
    await mgr.init_db()
    await mgr.ensure_user(MANOJ, "manoj", "Manoj")
    await mgr.ensure_user(RATIKA, "ratika", "Ratika")
    yield mgr
    await mgr.close()


async def test_duplicate_run_does_not_resend_delivered_chunks(db):
    bot = _Bot()
    ctx = SimpleNamespace(bot=bot)
    messages = ["a", "b", "c"]

    first = await _deliver_chunks(ctx, db, MANOJ, "job", DATE, messages)
    assert first == 3 and bot.texts_for(MANOJ) == ["a", "b", "c"]

    bot.sent.clear()
    second = await _deliver_chunks(ctx, db, MANOJ, "job", DATE, messages)
    assert second == 0 and bot.sent == []  # nothing resent on a duplicate run


async def test_middle_chunk_failure_resumes_next_run(db, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    bot = _Bot()
    bot.fail_texts = {"b"}
    ctx = SimpleNamespace(bot=bot)
    messages = ["a", "b", "c"]

    await _deliver_chunks(ctx, db, MANOJ, "job", DATE, messages)
    assert bot.texts_for(MANOJ) == ["a"]  # stopped at the failing middle chunk

    # "Restart": the outage clears; the next run resumes at chunk 1.
    bot.sent.clear()
    bot.fail_texts = set()
    await _deliver_chunks(ctx, db, MANOJ, "job", DATE, messages)
    assert bot.texts_for(MANOJ) == ["b", "c"]


async def test_transient_failure_then_success_within_a_chunk(db, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    calls = {"n": 0}

    class OnceFailBot:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []

        async def send_message(self, chat_id, text, parse_mode=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise NetworkError("first attempt down")
            self.sent.append((chat_id, text))

    bot = OnceFailBot()
    delivered = await _deliver_chunks(SimpleNamespace(bot=bot), db, MANOJ, "j", DATE, ["a"])
    assert delivered == 1 and bot.sent == [(MANOJ, "a")]


async def test_delivery_state_is_owner_scoped(db, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    bot = _Bot()
    bot.fail_users = {MANOJ}  # Manoj blocked the bot
    ctx = SimpleNamespace(bot=bot)
    messages = ["x", "y"]

    await _deliver_chunks(ctx, db, MANOJ, "job", DATE, messages)
    await _deliver_chunks(ctx, db, RATIKA, "job", DATE, messages)

    assert bot.texts_for(RATIKA) == ["x", "y"]  # unaffected by Manoj's failure
    assert bot.texts_for(MANOJ) == []
    assert await db.get_delivered_chunk_indices(MANOJ, "job", DATE) == set()
    assert await db.get_delivered_chunk_indices(RATIKA, "job", DATE) == {0, 1}


async def test_failed_chunk_records_sanitized_category(db, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    bot = _Bot()
    bot.fail_texts = {"a"}
    await _deliver_chunks(SimpleNamespace(bot=bot), db, MANOJ, "job", DATE, ["a"])

    row = await db._query_one(
        "SELECT status, error_category FROM reminder_deliveries WHERE user_id = ?",
        (MANOJ,),
    )
    assert row["status"] == "failed"
    assert row["error_category"] == "retry_exhausted"  # sanitized label, no URL/token


async def test_permanent_error_records_permanent_category(db):
    class PermBot:
        async def send_message(self, chat_id, text, parse_mode=None):
            raise Forbidden("bot was blocked by the user")

    await _deliver_chunks(SimpleNamespace(bot=PermBot()), db, MANOJ, "job", DATE, ["a"])
    row = await db._query_one(
        "SELECT error_category FROM reminder_deliveries WHERE user_id = ?", (MANOJ,)
    )
    assert row["error_category"] == "permanent"


async def test_daily_reminder_one_user_blocked_other_delivered(db, monkeypatch):
    monkeypatch.setattr(reminders_module, "ALLOWED_USER_IDS", frozenset({MANOJ, RATIKA}))
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    await db.set_reminders_enabled(MANOJ, True)
    await db.set_reminders_enabled(RATIKA, True)
    await db.add_habit(MANOJ, "ManojRead")
    await db.add_habit(RATIKA, "RatikaWrite")

    bot = _Bot()
    bot.fail_users = {MANOJ}  # Manoj blocked the bot
    ctx = SimpleNamespace(bot_data={"db": db}, bot=bot)

    await daily_reminder(ctx)

    ratika_text = "\n".join(bot.texts_for(RATIKA))
    assert "RatikaWrite" in ratika_text  # the other user still received their nudge
    assert "ManojRead" not in ratika_text  # each message carries only its owner's data
    assert bot.texts_for(MANOJ) == []
