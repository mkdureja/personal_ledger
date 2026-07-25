"""Case-insensitive habit keys: reactivation, dedup, and migration backfill."""

from __future__ import annotations

import pytest

from bot.config import today_local
from bot.database import _habit_key


def test_habit_key_normalizes_case_and_whitespace():
    assert _habit_key("  Read  ") == "read"
    assert _habit_key("READ") == _habit_key("read")


@pytest.mark.asyncio
async def test_add_habit_stores_name_key(db_with_user, user_id):
    hid, status = await db_with_user.add_habit(user_id, "Read Books")
    assert status == "added"
    row = await db_with_user._query_one(
        "SELECT name_key FROM habits WHERE id = ?", (hid,)
    )
    assert row["name_key"] == "read books"


@pytest.mark.asyncio
async def test_case_variant_is_already_active(db_with_user, user_id):
    hid, _ = await db_with_user.add_habit(user_id, "Read")
    same, status = await db_with_user.add_habit(user_id, "read")
    assert same == hid
    assert status == "already_active"
    assert len(await db_with_user.get_active_habits(user_id)) == 1


@pytest.mark.asyncio
async def test_reactivation_is_case_insensitive_and_keeps_streak(
    db_with_user, user_id
):
    today = today_local()
    hid, _ = await db_with_user.add_habit(user_id, "Read")
    await db_with_user.check_habit(user_id, hid, today)
    assert await db_with_user.get_streak(user_id, hid, today) == 1

    await db_with_user.deactivate_habit(user_id, hid)
    same, status = await db_with_user.add_habit(user_id, "READ")
    assert same == hid  # same id -> history preserved
    assert status == "reactivated"
    assert await db_with_user.get_streak(user_id, hid, today) == 1


@pytest.mark.asyncio
async def test_migration_backfills_null_name_key(db_with_user, user_id):
    # Simulate a legacy row inserted without a key, then re-run the migration.
    async with db_with_user._write_operation():
        await db_with_user.conn.execute(
            "INSERT INTO habits (user_id, habit_name, name_key) VALUES (?, ?, NULL)",
            (user_id, "Legacy Habit"),
        )
        await db_with_user._migrate_habit_name_keys()

    row = await db_with_user._query_one(
        "SELECT name_key FROM habits WHERE habit_name = ?", ("Legacy Habit",)
    )
    assert row["name_key"] == "legacy habit"
