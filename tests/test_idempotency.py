"""Replay-safe mutations and /recent reconciliation (implementation_plan Phase 3).

At-least-once Telegram delivery (drop_pending_updates=False) must produce
exactly-once ledger mutations: replaying the *same* update returns the original
row, while genuinely separate activities (new updates) still each log. Idempotency
never crosses a user or chat boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from bot.database import DatabaseManager, MutationSource
from bot.handlers.recent import recent_command
from bot.handlers.study import study_command

MANOJ = 111
RATIKA = 222
TODAY = datetime.now(timezone.utc).date()


@pytest_asyncio.fixture
async def two_user_db():
    mgr = DatabaseManager(":memory:")
    await mgr.connect()
    await mgr.init_db()
    await mgr.ensure_user(MANOJ, "manoj", "Manoj")
    await mgr.ensure_user(RATIKA, "ratika", "Ratika")
    yield mgr
    await mgr.close()


async def _receipt_count(db) -> int:
    rows = await db._query_all("SELECT * FROM mutation_receipts")
    return len(rows)


# ---------------------------------------------------------------------------
# DB-level idempotency
# ---------------------------------------------------------------------------
async def test_same_update_processed_twice_inserts_one_row_and_one_receipt(two_user_db):
    source = MutationSource(update_id=5001, chat_id=MANOJ, message_id=7)
    first = await two_user_db.log_study(MANOJ, "Math", 60, source=source)
    second = await two_user_db.log_study(MANOJ, "Math", 60, source=source)

    assert first == second  # replay returned the original row
    assert len(await two_user_db.get_study_logs(MANOJ, TODAY, TODAY)) == 1
    assert await _receipt_count(two_user_db) == 1


async def test_same_message_id_different_users_both_persist(two_user_db):
    # Message IDs are only unique per chat; both must persist under distinct,
    # globally-unique update_ids.
    m = await two_user_db.log_diet(
        MANOJ, "lunch", "Manoj-meal", 100, protein_g=1, carbs_g=2, fat_g=3,
        source=MutationSource(update_id=10, chat_id=MANOJ, message_id=42),
    )
    r = await two_user_db.log_diet(
        RATIKA, "lunch", "Ratika-meal", 100, protein_g=1, carbs_g=2, fat_g=3,
        source=MutationSource(update_id=11, chat_id=RATIKA, message_id=42),
    )

    assert m != r
    assert len(await two_user_db.get_diet_logs(MANOJ, TODAY, TODAY)) == 1
    assert len(await two_user_db.get_diet_logs(RATIKA, TODAY, TODAY)) == 1


async def test_replay_cannot_return_another_users_receipt(two_user_db):
    source = MutationSource(update_id=777, chat_id=MANOJ, message_id=1)
    await two_user_db.log_study(MANOJ, "Owned", 30, source=source)

    # A replay of the same update_id claiming a different owner must be refused,
    # never silently returning Manoj's row to Ratika.
    with pytest.raises(RuntimeError, match="owner mismatch"):
        await two_user_db.log_study(RATIKA, "Owned", 30, source=source)

    assert len(await two_user_db.get_study_logs(MANOJ, TODAY, TODAY)) == 1
    assert len(await two_user_db.get_study_logs(RATIKA, TODAY, TODAY)) == 0


async def test_separate_identical_activities_are_not_deduplicated(two_user_db):
    # Same content, two distinct updates (the user really did it twice).
    a = await two_user_db.log_gym(
        MANOJ, "Pushups", 3, 10, source=MutationSource(update_id=1)
    )
    b = await two_user_db.log_gym(
        MANOJ, "Pushups", 3, 10, source=MutationSource(update_id=2)
    )
    assert a != b
    assert len(await two_user_db.get_gym_logs(MANOJ, TODAY, TODAY)) == 2


async def test_missing_source_falls_back_to_plain_insert(two_user_db):
    a = await two_user_db.log_study(MANOJ, "X", 10)
    b = await two_user_db.log_study(MANOJ, "X", 10)
    assert a != b
    assert len(await two_user_db.get_study_logs(MANOJ, TODAY, TODAY)) == 2
    assert await _receipt_count(two_user_db) == 0


# ---------------------------------------------------------------------------
# Handler-level replay (the study shortcut wires source through)
# ---------------------------------------------------------------------------
async def test_study_command_replay_is_idempotent(two_user_db):
    def _make_update(update_id: int):
        message = SimpleNamespace(message_id=9, reply_text=AsyncMock(), text="/study maths 45")
        return SimpleNamespace(
            update_id=update_id,
            message=message,
            effective_message=message,
            effective_user=SimpleNamespace(id=MANOJ, username="manoj", first_name="Manoj"),
            effective_chat=SimpleNamespace(id=MANOJ, type="private"),
        )

    def _ctx():
        return SimpleNamespace(bot_data={"db": two_user_db}, user_data={}, args=["maths", "45"])

    await study_command(_make_update(8080), _ctx())
    await study_command(_make_update(8080), _ctx())  # Telegram replay of same update

    rows = await two_user_db.get_study_logs(MANOJ, TODAY, TODAY)
    assert len(rows) == 1
    assert await _receipt_count(two_user_db) == 1


# ---------------------------------------------------------------------------
# /recent reconciliation
# ---------------------------------------------------------------------------
async def test_get_recent_entries_is_owner_scoped_and_newest_first(two_user_db):
    await two_user_db.log_study(MANOJ, "Manoj-Study", 30)
    await two_user_db.log_gym(MANOJ, "Manoj-Gym", 3, 8, 50.0)
    await two_user_db.log_diet(MANOJ, "lunch", "Manoj-Food", 500, protein_g=1, carbs_g=2, fat_g=3)
    await two_user_db.log_study(RATIKA, "Ratika-Study", 20)

    entries = await two_user_db.get_recent_entries(MANOJ, limit=10)
    kinds = {e["kind"] for e in entries}
    summaries = {e["summary"] for e in entries}
    assert kinds == {"study", "gym", "diet"}
    assert "Ratika-Study" not in summaries  # strictly owner-scoped
    assert len(entries) == 3
    # Newest first: the diet entry was logged last.
    assert entries[0]["summary"] == "Manoj-Food"


async def test_recent_command_lists_entries(two_user_db):
    await two_user_db.log_study(MANOJ, "Calculus", 45)
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=MANOJ),
    )
    context = SimpleNamespace(bot_data={"db": two_user_db})

    await recent_command(update, context)

    sent = message.reply_text.await_args.args[0]
    assert "Recent entries" in sent
    assert "Calculus" in sent


async def test_recent_command_handles_empty(two_user_db):
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=RATIKA),
    )
    context = SimpleNamespace(bot_data={"db": two_user_db})

    await recent_command(update, context)

    assert "No recent entries" in message.reply_text.await_args.args[0]
