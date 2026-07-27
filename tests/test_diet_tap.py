"""Tap-first diet flow (P1): saved-food/recipe selection, quantity, preview,
save, and the keep-logging loop — plus owner/stale callback safety.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType
from telegram.ext import ConversationHandler

from bot.handlers import diet
from bot.handlers.catalog import resolve_catalog_diet_entry
from bot.handlers.common import activate_conversation, active_conversation_flow

USER = 123456789  # matches conftest ALLOWED_USER_IDS / user_id fixture

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fake update / context helpers
# ---------------------------------------------------------------------------
def _query(data: str, *, message_id: int = 100, sent_id: int = 777):
    """A callback query whose message.reply_text yields a known new id."""
    return SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=SimpleNamespace(
            message_id=message_id,
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=sent_id)),
        ),
        edit_message_reply_markup=AsyncMock(),
    )


def _cb_update(query, *, user_id: int = USER):
    return SimpleNamespace(
        callback_query=query,
        effective_message=query.message,
        effective_user=SimpleNamespace(id=user_id, username="t", first_name="T"),
        effective_chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
    )


def _msg_update(text: str, *, user_id: int = USER):
    message = SimpleNamespace(
        text=text,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=777)),
    )
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id, username="t", first_name="T"),
        effective_chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
    )


def _context(db, user_data=None):
    return SimpleNamespace(bot_data={"db": db}, user_data=user_data or {})


async def _save_apple(db, uid):
    food = (
        await db.save_food(
            uid, "apple", "g", 100, calories=52, protein_g=0.3, carbs_g=14, fat_g=0.2
        )
    )["food"]
    await db.save_food_portion(uid, food["id"], "medium", 182, "g")
    return food


async def _save_recipe(db, uid):
    apple = await _save_apple(db, uid)
    recipe = (await db.save_recipe(uid, "apple-bowl", 1, "serving"))["recipe"]
    await db.save_recipe_ingredient(
        uid, recipe["id"], apple["id"], 182, "g", 1, "medium"
    )
    return recipe


# ---------------------------------------------------------------------------
# P0: direct menu entry
# ---------------------------------------------------------------------------
async def test_diet_menu_entry_starts_the_flow():
    db = SimpleNamespace(ensure_user=AsyncMock())
    query = _query("menu_diet")
    update = _cb_update(query)
    context = _context(db)

    result = await diet.diet_menu_entry(update, context)

    assert result == diet.MEAL_TYPE
    query.answer.assert_awaited()  # spinner stopped
    query.message.reply_text.assert_awaited_once()
    assert "reply_markup" in query.message.reply_text.await_args.kwargs
    assert active_conversation_flow(context) == "diet"
    assert context.user_data["diet_meal_message_id"] == 777


async def test_diet_menu_entry_declines_when_another_flow_active():
    db = SimpleNamespace(ensure_user=AsyncMock())
    query = _query("menu_diet")
    update = _cb_update(query)
    context = _context(db)
    activate_conversation(update, context, "gym")

    result = await diet.diet_menu_entry(update, context)

    assert result == ConversationHandler.END
    assert active_conversation_flow(context) == "gym"  # untouched
    assert "diet_meal_message_id" not in context.user_data
    assert "already in" in query.message.reply_text.await_args.args[0]


# ---------------------------------------------------------------------------
# Meal selection branches into taps or free text
# ---------------------------------------------------------------------------
async def test_meal_selection_offers_saved_items_when_present():
    db = SimpleNamespace(
        list_foods=AsyncMock(return_value=[{"id": 5, "name": "Apple"}]),
        list_recipes=AsyncMock(return_value=[]),
    )
    query = _query(f"meal_{USER}_breakfast", message_id=100)
    context = _context(db, {"diet_meal_message_id": 100})

    result = await diet.receive_meal_type(_cb_update(query), context)

    assert result == diet.FOOD_CHOICE
    assert context.user_data["diet_meal_type"] == "breakfast"
    assert context.user_data["diet_ui_message_id"] == 777
    assert "reply_markup" in query.message.reply_text.await_args.kwargs


async def test_meal_selection_falls_back_to_free_text_when_no_saved_items():
    db = SimpleNamespace(
        list_foods=AsyncMock(return_value=[]),
        list_recipes=AsyncMock(return_value=[]),
    )
    query = _query(f"meal_{USER}_lunch", message_id=100)
    context = _context(db, {"diet_meal_message_id": 100})

    result = await diet.receive_meal_type(_cb_update(query), context)

    assert result == diet.FOOD_ITEMS
    assert "reply_markup" not in query.message.reply_text.await_args.kwargs
    assert "diet_ui_message_id" not in context.user_data


# ---------------------------------------------------------------------------
# Selecting a food
# ---------------------------------------------------------------------------
async def test_choose_food_with_portions_asks_how_much():
    db = SimpleNamespace(
        get_food_by_id=AsyncMock(
            return_value={"id": 5, "name": "Apple", "base_unit": "g"}
        ),
        get_food_portions=AsyncMock(
            return_value=[
                {"id": 9, "name": "medium", "name_key": "medium", "base_amount": 182}
            ]
        ),
    )
    query = _query(f"dfood_{USER}_5", message_id=100)
    context = _context(db, {"diet_ui_message_id": 100, "diet_meal_type": "snack"})

    result = await diet.choose_food(_cb_update(query), context)

    assert result == diet.PORTION_CHOICE
    assert context.user_data["diet_sel_kind"] == "food"
    assert context.user_data["diet_sel_id"] == 5
    assert "reply_markup" in query.message.reply_text.await_args.kwargs


async def test_choose_food_without_portions_prompts_custom_amount():
    db = SimpleNamespace(
        get_food_by_id=AsyncMock(
            return_value={"id": 5, "name": "Apple", "base_unit": "g"}
        ),
        get_food_portions=AsyncMock(return_value=[]),
    )
    query = _query(f"dfood_{USER}_5", message_id=100)
    context = _context(db, {"diet_ui_message_id": 100, "diet_meal_type": "snack"})

    result = await diet.choose_food(_cb_update(query), context)

    assert result == diet.CUSTOM_AMOUNT
    assert context.user_data["diet_sel_kind"] == "food"
    assert "diet_ui_message_id" not in context.user_data  # text state, no keyboard


async def test_tampered_owner_id_fails_closed():
    db = SimpleNamespace(get_food_by_id=AsyncMock())
    # Callback claims a different owner than the acting user.
    query = _query(f"dfood_{USER + 1}_5", message_id=100)
    context = _context(db, {"diet_ui_message_id": 100})

    result = await diet.choose_food(_cb_update(query, user_id=USER), context)

    assert result == diet.FOOD_CHOICE
    db.get_food_by_id.assert_not_awaited()
    assert query.answer.await_args.kwargs.get("show_alert") is True


async def test_stale_keyboard_message_id_is_rejected():
    db = SimpleNamespace(get_food_by_id=AsyncMock())
    query = _query(f"dfood_{USER}_5", message_id=999)  # not the live UI message
    context = _context(db, {"diet_ui_message_id": 100})

    result = await diet.choose_food(_cb_update(query), context)

    assert result == diet.FOOD_CHOICE
    db.get_food_by_id.assert_not_awaited()
    query.edit_message_reply_markup.assert_awaited()  # stale keyboard retired


# ---------------------------------------------------------------------------
# Quantity resolution matches the shared resolver (real DB)
# ---------------------------------------------------------------------------
async def test_choose_portion_matches_catalog_resolver(db_with_user, user_id):
    food = await _save_apple(db_with_user, user_id)
    portions = await db_with_user.get_food_portions(user_id, food["id"])
    medium = next(p for p in portions if p["name"] == "medium")
    context = _context(
        db_with_user,
        {
            "diet_meal_type": "snack",
            "diet_sel_kind": "food",
            "diet_sel_id": food["id"],
            "diet_ui_message_id": 100,
        },
    )
    query = _query(f"dport_{user_id}_{medium['id']}", message_id=100)

    result = await diet.choose_portion(_cb_update(query, user_id=user_id), context)

    assert result == diet.CONFIRM_ITEM
    expected = await resolve_catalog_diet_entry(
        db_with_user, user_id, "food:apple", ["1", "medium"]
    )
    pending = context.user_data["diet_pending"]
    assert pending["calories"] == expected.calories
    assert pending["protein_g"] == expected.protein_g
    assert pending["carbs_g"] == expected.carbs_g
    assert pending["display_text"] == expected.display_text


async def test_custom_amount_resolves_and_previews(db_with_user, user_id):
    food = await _save_apple(db_with_user, user_id)
    context = _context(
        db_with_user,
        {"diet_meal_type": "snack", "diet_sel_kind": "food", "diet_sel_id": food["id"]},
    )

    result = await diet.receive_custom_amount(
        _msg_update("220 g", user_id=user_id), context
    )

    assert result == diet.CONFIRM_ITEM
    assert context.user_data["diet_pending"]["calories"] == 114


async def test_custom_amount_invalid_stays_in_state(db_with_user, user_id):
    food = await _save_apple(db_with_user, user_id)
    context = _context(
        db_with_user,
        {"diet_meal_type": "snack", "diet_sel_kind": "food", "diet_sel_id": food["id"]},
    )
    update = _msg_update("banana", user_id=user_id)

    result = await diet.receive_custom_amount(update, context)

    assert result == diet.CUSTOM_AMOUNT
    assert "diet_pending" not in context.user_data
    update.message.reply_text.assert_awaited_once()


async def test_recipe_quick_amount_matches_catalog_resolver(db_with_user, user_id):
    recipe = await _save_recipe(db_with_user, user_id)
    context = _context(
        db_with_user,
        {
            "diet_meal_type": "breakfast",
            "diet_sel_kind": "recipe",
            "diet_sel_id": recipe["id"],
            "diet_ui_message_id": 100,
        },
    )
    query = _query(f"drq_{user_id}", message_id=100)

    result = await diet.recipe_quick_amount(_cb_update(query, user_id=user_id), context)

    assert result == diet.CONFIRM_ITEM
    expected = await resolve_catalog_diet_entry(
        db_with_user, user_id, "recipe:apple-bowl", ["1", "serving"]
    )
    assert context.user_data["diet_pending"]["calories"] == expected.calories
    assert context.user_data["diet_pending"]["display_text"] == expected.display_text


# ---------------------------------------------------------------------------
# Save is the mutation boundary and is idempotent on replay
# ---------------------------------------------------------------------------
async def test_save_item_writes_once_and_offers_loop():
    db = SimpleNamespace(log_diet=AsyncMock())
    pending = {
        "display_text": "220 g apple",
        "calories": 114,
        "protein_g": 0.66,
        "carbs_g": 30.8,
        "fat_g": 0.44,
    }
    context = _context(
        db,
        {
            "diet_meal_type": "snack",
            "diet_pending": pending,
            "diet_ui_message_id": 100,
        },
    )
    update = _cb_update(_query(f"dsave_{USER}", message_id=100))
    activate_conversation(update, context, "diet")

    result = await diet.save_item(update, context)

    assert result == diet.LOG_ANOTHER
    db.log_diet.assert_awaited_once()
    assert "diet_pending" not in context.user_data
    assert active_conversation_flow(context) == "diet"  # loop stays open

    # A replayed Save lands on the retired keyboard (id 100 != new 777) and must
    # not write a second row.
    replay = _cb_update(_query(f"dsave_{USER}", message_id=100))
    result2 = await diet.save_item(replay, context)
    assert result2 == diet.CONFIRM_ITEM
    db.log_diet.assert_awaited_once()


async def test_log_another_yes_reopens_meal_picker():
    db = SimpleNamespace()
    context = _context(db)
    update = _cb_update(_query(f"dmore_{USER}_yes", message_id=100))
    activate_conversation(update, context, "diet")
    context.user_data["diet_ui_message_id"] = 100

    result = await diet.log_another(update, context)

    assert result == diet.MEAL_TYPE
    assert context.user_data["diet_meal_message_id"] == 777
    assert active_conversation_flow(context) == "diet"


async def test_log_another_no_ends_the_flow():
    db = SimpleNamespace()
    context = _context(db)
    update = _cb_update(_query(f"dmore_{USER}_no", message_id=100))
    activate_conversation(update, context, "diet")
    context.user_data["diet_ui_message_id"] = 100

    result = await diet.log_another(update, context)

    assert result == ConversationHandler.END
    assert active_conversation_flow(context) is None


async def test_type_it_instead_preserves_free_text_path():
    db = SimpleNamespace()
    query = _query(f"dtype_{USER}", message_id=100)
    context = _context(db, {"diet_meal_type": "lunch", "diet_ui_message_id": 100})

    result = await diet.type_food_instead(_cb_update(query), context)

    assert result == diet.FOOD_ITEMS
    assert "diet_ui_message_id" not in context.user_data


# ---------------------------------------------------------------------------
# Stale taps after the conversation ended
# ---------------------------------------------------------------------------
async def test_stale_diet_callback_answers_expired():
    query = _query(f"dfood_{USER}_5")
    context = _context(SimpleNamespace())

    await diet.stale_diet_callback(_cb_update(query), context)

    assert query.answer.await_args.kwargs.get("show_alert") is True
    query.edit_message_reply_markup.assert_awaited()


async def test_stale_diet_callback_rejects_other_users_button():
    query = _query(f"dsave_{USER + 1}")
    context = _context(SimpleNamespace())

    await diet.stale_diet_callback(_cb_update(query, user_id=USER), context)

    assert "another user" in query.answer.await_args.args[0]
    query.edit_message_reply_markup.assert_not_awaited()
