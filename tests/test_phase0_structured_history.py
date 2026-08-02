"""Phase 0 Gate: Ensure all Diet writes use structured history."""

from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.ext import ConversationHandler
from telegram.error import NetworkError

from bot.handlers import diet
from bot.database import MutationSource
from ledger_schema import LATEST_SCHEMA_VERSION

pytestmark = pytest.mark.asyncio

def _user(user_id=123456789):
    return SimpleNamespace(id=user_id, username="tester", first_name="Test")

def _message(text: str = ""):
    return SimpleNamespace(text=text, reply_text=AsyncMock(), chat_id=123, message_id=456)

def _update(message=None, user_id=123456789):
    msg = message or _message()
    return SimpleNamespace(
        message=msg,
        effective_message=msg,
        effective_user=_user(user_id),
        effective_chat=SimpleNamespace(id=42),
        update_id=999,
    )

def _context(db: object, args: list[str] | None = None, user_data: dict | None = None):
    return SimpleNamespace(
        bot_data={"db": db},
        args=args or [],
        user_data=user_data if user_data is not None else {},
    )

async def test_a_fresh_database_is_stamped_at_the_current_schema_version(db):
    """A new database migrates all the way to this checkout's version.

    Asserted against ``LATEST_SCHEMA_VERSION`` rather than a literal: pinning the
    number here meant every migration broke this test for no product reason,
    while still not checking the thing that matters — that setup reaches the
    version the rest of the code assumes.
    """
    row = await db._query_one("PRAGMA user_version")
    assert row["user_version"] == LATEST_SCHEMA_VERSION

async def test_food_and_recipe_reference_write_structured_child(db_with_user, user_id):
    await db_with_user.save_food(user_id, "apple", "g", 100.0, calories=52)
    
    ctx = _context(db_with_user, args=["snack", "food:apple", "200", "g"])
    res = await diet.diet_command(_update(), ctx)
    assert res == ConversationHandler.END

    rows = await db_with_user._query_all("SELECT * FROM diet_logs WHERE user_id = ?", (user_id,))
    assert len(rows) == 1
    meal_id = rows[0]["id"]
    children = await db_with_user.get_diet_log_items(user_id, meal_id)
    assert len(children) == 1
    child = children[0]
    assert child["source_type"] == "food"
    assert child["display_name"] == "200 g apple"
    assert child["entered_amount"] == 200.0
    assert child["entered_unit"] == "g"
    assert child["resolved_base_amount"] == 200.0
    assert child["resolved_base_unit"] == "g"

    # Recipe
    await db_with_user.save_recipe(user_id, "curry", 4.0, "serving")
    await db_with_user.save_recipe_ingredient(user_id, 1, 1, 100.0, "g", 100.0, "g")
    
    ctx2 = _context(db_with_user, args=["dinner", "recipe:curry", "1", "serving"])
    upd2 = _update()
    upd2.update_id = 1000
    res2 = await diet.diet_command(upd2, ctx2)
    assert res2 == ConversationHandler.END

    rows2 = await db_with_user._query_all("SELECT * FROM diet_logs WHERE user_id = ? ORDER BY id", (user_id,))
    assert len(rows2) == 2
    meal_id2 = rows2[1]["id"]
    children2 = await db_with_user.get_diet_log_items(user_id, meal_id2)
    assert len(children2) == 1
    child2 = children2[0]
    assert child2["source_type"] == "recipe"
    assert child2["display_name"] == "1 serving curry (recipe)"
    assert child2["entered_amount"] == 1.0
    assert child2["entered_unit"] == "serving"

async def test_manual_command_writes_freetext_child(db_with_user, user_id):
    ctx = _context(db_with_user, args=["lunch", "pizza", "slice", "300", "p=15"])
    res = await diet.diet_command(_update(), ctx)
    assert res == ConversationHandler.END

    rows = await db_with_user._query_all("SELECT * FROM diet_logs WHERE user_id = ?", (user_id,))
    assert len(rows) == 1
    meal_id = rows[0]["id"]
    children = await db_with_user.get_diet_log_items(user_id, meal_id)
    assert len(children) == 1
    child = children[0]
    assert child["source_type"] == "freetext"
    assert child["display_name"] == "pizza slice"
    assert child["calories"] == 300
    assert child["protein_g"] == 15.0
    assert child["source_id"] is None
    assert child["entered_amount"] is None

async def test_guided_input_writes_freetext_child(db_with_user, user_id):
    state = {
        "diet_meal_type": "lunch",
        "diet_food_items": "burger",
        "diet_calories": 500,
    }
    ctx = _context(db_with_user, user_data=state)
    msg = _message("20 40 10")
    upd = _update(msg)
    
    res = await diet.receive_macros(upd, ctx)
    assert res == diet.LOG_ANOTHER

    rows = await db_with_user._query_all("SELECT * FROM diet_logs WHERE user_id = ?", (user_id,))
    assert len(rows) == 1
    meal_id = rows[0]["id"]
    children = await db_with_user.get_diet_log_items(user_id, meal_id)
    assert len(children) == 1
    child = children[0]
    assert child["source_type"] == "freetext"
    assert child["display_name"] == "burger"
    assert child["calories"] == 500
    assert child["protein_g"] == 20.0
    assert child["carbs_g"] == 40.0
    assert child["fat_g"] == 10.0

async def test_same_update_replay_creates_one_header_and_child(db_with_user, user_id):
    ctx = _context(db_with_user, args=["snack", "cookie", "150"])
    upd = _update()
    
    # First delivery
    await diet.diet_command(upd, ctx)
    # Replay
    await diet.diet_command(upd, ctx)

    rows = await db_with_user._query_all("SELECT * FROM diet_logs WHERE user_id = ?", (user_id,))
    assert len(rows) == 1
    children = await db_with_user.get_diet_log_items(user_id, rows[0]["id"])
    assert len(children) == 1

async def test_cross_owner_private_reference_writes_nothing(db_with_user, user_id):
    # Other user creates food
    other_user = 999
    await db_with_user.ensure_user(other_user, "other", "Other")
    await db_with_user.save_food(other_user, "apple", "g", 100.0, calories=52)

    ctx = _context(db_with_user, args=["snack", "food:apple", "1", "g"])
    upd = _update(user_id=user_id)
    
    res = await diet.diet_command(upd, ctx)
    assert res == ConversationHandler.END

    rows = await db_with_user._query_all("SELECT * FROM diet_logs WHERE user_id = ?", (user_id,))
    assert len(rows) == 0

async def test_confirmation_delivery_failure_followed_by_replay_creates_no_duplicate(db_with_user, user_id):
    ctx = _context(db_with_user, args=["snack", "cookie", "150"])
    msg = _message()
    msg.reply_text.side_effect = [NetworkError("timeout"), AsyncMock()]
    upd = _update(msg)
    
    # First delivery fails network
    await diet.diet_command(upd, ctx)
    
    # Client replays automatically
    await diet.diet_command(upd, ctx)

    rows = await db_with_user._query_all("SELECT * FROM diet_logs WHERE user_id = ?", (user_id,))
    assert len(rows) == 1
    children = await db_with_user.get_diet_log_items(user_id, rows[0]["id"])
    assert len(children) == 1
