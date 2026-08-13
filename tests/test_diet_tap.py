"""Tap-first diet flow (P1): saved-food/recipe selection, quantity, preview,
save, and the keep-logging loop â€” plus owner/stale callback safety.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType
from telegram.ext import ConversationHandler

from bot.callback_data import to_base36
from bot.config import today_local
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
        get_suggestions_enabled=AsyncMock(return_value=False),
        get_food_preferences=AsyncMock(return_value={}),
    )
    query = _query(f"meal_{USER}_breakfast", message_id=100)
    context = _context(db, {"diet_meal_message_id": 100})

    result = await diet.receive_meal_type(_cb_update(query), context)

    assert result == diet.FOOD_CHOICE
    assert context.user_data["diet_meal_type"] == "breakfast"
    assert context.user_data["diet_ui_message_id"] == 777
    assert "reply_markup" in query.message.reply_text.await_args.kwargs


async def test_meal_selection_still_offers_search_when_no_saved_items():
    """Even with no personal foods, the choice keyboard (with ðŸ”Ž Search / âœï¸ Type)
    is shown so the shared catalog is reachable by tapping."""
    db = SimpleNamespace(
        list_foods=AsyncMock(return_value=[]),
        list_recipes=AsyncMock(return_value=[]),
        get_suggestions_enabled=AsyncMock(return_value=False),
        get_food_preferences=AsyncMock(return_value={}),
    )
    query = _query(f"meal_{USER}_lunch", message_id=100)
    context = _context(db, {"diet_meal_message_id": 100})

    result = await diet.receive_meal_type(_cb_update(query), context)

    assert result == diet.FOOD_CHOICE
    kwargs = query.message.reply_text.await_args.kwargs
    assert "reply_markup" in kwargs
    # The Search-catalog button must be present.
    buttons = [
        b.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any(cb.startswith("dsearch_") for cb in buttons)
    assert context.user_data["diet_ui_message_id"] == 777


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
        get_recent_item_quantities=AsyncMock(return_value=[]),
        get_food_preference=AsyncMock(return_value=None),
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
        get_recent_item_quantities=AsyncMock(return_value=[]),
        get_food_preference=AsyncMock(return_value=None),
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
    item = context.user_data["diet_items"][-1]
    assert item["calories"] == expected.calories
    assert item["protein_g"] == expected.protein_g
    assert item["carbs_g"] == expected.carbs_g
    assert item["display_name"] == expected.display_text
    assert item["source_type"] == "food"
    assert item["source_id"] == food["id"]


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
    assert context.user_data["diet_items"][-1]["calories"] == 114


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
    item = context.user_data["diet_items"][-1]
    assert item["calories"] == expected.calories
    assert item["display_name"] == expected.display_text
    assert item["source_type"] == "recipe"
    assert item["source_id"] == recipe["id"]


# ---------------------------------------------------------------------------
# Save is the mutation boundary and is idempotent on replay
# ---------------------------------------------------------------------------
async def test_save_item_writes_once_and_offers_loop():
    db = SimpleNamespace(log_diet_with_items=AsyncMock())
    item = {
        "source_type": "food",
        "source_id": 5,
        "display_name": "220 g apple",
        "entered_amount": 220.0,
        "entered_unit": "g",
        "resolved_base_amount": 220.0,
        "resolved_base_unit": "g",
        "calories": 114,
        "protein_g": 0.66,
        "carbs_g": 30.8,
        "fat_g": 0.44,
    }
    context = _context(
        db,
        {
            "diet_meal_type": "snack",
            "diet_items": [item],
            "diet_ui_message_id": 100,
        },
    )
    update = _cb_update(_query(f"dsave_{USER}", message_id=100))
    activate_conversation(update, context, "diet")

    result = await diet.save_item(update, context)

    assert result == diet.LOG_ANOTHER
    db.log_diet_with_items.assert_awaited_once()
    # The meal was written with exactly the drafted items.
    assert db.log_diet_with_items.await_args.args[2] == [item]
    assert "diet_items" not in context.user_data
    assert active_conversation_flow(context) == "diet"  # loop stays open

    # A replayed Save lands on the retired keyboard (id 100 != new 777) and must
    # not write a second meal.
    replay = _cb_update(_query(f"dsave_{USER}", message_id=100))
    result2 = await diet.save_item(replay, context)
    assert result2 == diet.CONFIRM_ITEM
    db.log_diet_with_items.assert_awaited_once()


async def test_add_another_item_keeps_draft_and_returns_to_list():
    db = SimpleNamespace(
        list_foods=AsyncMock(return_value=[{"id": 5, "name": "Apple"}]),
        list_recipes=AsyncMock(return_value=[]),
        get_suggestions_enabled=AsyncMock(return_value=False),
        get_food_preferences=AsyncMock(return_value={}),
    )
    existing = [{"display_name": "1 medium apple", "calories": 95}]
    query = _query(f"dadd_{USER}", message_id=100)
    context = _context(
        db,
        {
            "diet_meal_type": "lunch",
            "diet_items": existing,
            "diet_ui_message_id": 100,
        },
    )

    result = await diet.add_another_item(_cb_update(query), context)

    assert result == diet.FOOD_CHOICE
    assert context.user_data["diet_items"] == existing  # draft preserved


async def test_two_tapped_items_become_one_meal_with_two_children(
    db_with_user, user_id
):
    await _save_apple(db_with_user, user_id)
    rice = (
        await db_with_user.save_food(
            user_id, "rice", "g", 100, calories=130, protein_g=2.4, carbs_g=28, fat_g=0.3
        )
    )["food"]
    context = _context(
        db_with_user,
        {
            "diet_meal_type": "lunch",
            "diet_sel_kind": "food",
            "diet_sel_id": rice["id"],
        },
    )
    # First item via custom amount.
    await diet.receive_custom_amount(_msg_update("100 g", user_id=user_id), context)
    assert len(context.user_data["diet_items"]) == 1

    # Second item.
    context.user_data["diet_sel_kind"] = "food"
    context.user_data["diet_sel_id"] = rice["id"]
    await diet.receive_custom_amount(_msg_update("50 g", user_id=user_id), context)
    assert len(context.user_data["diet_items"]) == 2

    # Save the meal.
    context.user_data["diet_ui_message_id"] = 100
    save = _cb_update(_query(f"dsave_{USER}", message_id=100), user_id=user_id)
    activate_conversation(save, context, "diet")
    result = await diet.save_item(save, context)

    assert result == diet.LOG_ANOTHER
    from bot.config import today_local

    rows = await db_with_user.get_diet_logs(user_id, today_local(), today_local())
    assert len(rows) == 1  # one meal, not two
    items = await db_with_user.get_diet_log_items(user_id, rows[-1]["id"])
    assert len(items) == 2


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


# ---------------------------------------------------------------------------
# Finishing a meal from the picker (the "cancelled my whole meal" defect)
# ---------------------------------------------------------------------------
# Reported from real use: two items entered, the third could not be found, and
# the only visible exit from the picker was ✖️ Cancel — which discards the
# draft. The picker now carries Save whenever there is something to save, and
# Cancel says what it is about to throw away.
def _picker_db(foods=()):
    return SimpleNamespace(
        list_foods=AsyncMock(return_value=list(foods)),
        list_recipes=AsyncMock(return_value=[]),
        get_suggestions_enabled=AsyncMock(return_value=False),
        get_food_preferences=AsyncMock(return_value={}),
    )


def _item(name, cal, p, c, f):
    """One drafted item, as the builder assembles it."""
    return {
        "source_type": "freetext",
        "source_id": None,
        "display_name": name,
        "entered_amount": None,
        "entered_unit": None,
        "resolved_base_amount": None,
        "resolved_base_unit": None,
        "calories": cal,
        "protein_g": p,
        "carbs_g": c,
        "fat_g": f,
    }


def _button_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


async def test_the_picker_offers_save_once_the_draft_has_something(monkeypatch):
    monkeypatch.setattr("bot.config.PHASE1_ENABLED_USER_IDS", frozenset({USER}))
    db = _picker_db([{"id": 5, "name": "Apple"}])
    query = _query(f"dadd_{USER}", message_id=100)
    context = _context(
        db,
        {
            "diet_meal_type": "lunch",
            "diet_items": [_item("dal", 200, 9, 30, 1), _item("rice", 150, 3, 33, 0)],
            "diet_ui_message_id": 100,
            "diet_ui_revision": 0,
        },
    )

    state = await diet._prompt_food_choice(
        _cb_update(query), context, query.message, "lunch"
    )

    assert state == diet.FOOD_CHOICE
    markup = query.message.reply_text.await_args.kwargs["reply_markup"]
    assert "✅ Save meal (2 items)" in _button_texts(markup)


async def test_an_empty_draft_gets_no_save_button(monkeypatch):
    """Nothing to save yet: the button would be a dead end, not a way out."""
    monkeypatch.setattr("bot.config.PHASE1_ENABLED_USER_IDS", frozenset({USER}))
    db = _picker_db([{"id": 5, "name": "Apple"}])
    query = _query(f"meal_{USER}_lunch", message_id=100)
    context = _context(db, {"diet_meal_type": "lunch", "diet_ui_revision": 0})

    await diet._prompt_food_choice(_cb_update(query), context, query.message, "lunch")

    markup = query.message.reply_text.await_args.kwargs["reply_markup"]
    assert not any(text.startswith("✅ Save") for text in _button_texts(markup))


async def test_saving_from_the_picker_writes_the_meal(db_with_user, user_id):
    """The two entered items reach the ledger without passing through Cancel."""
    items = [_item("dal", 200, 9, 30, 1), _item("rice", 150, 3, 33, 0)]
    query = _query(f"dsave_{to_base36(user_id)}_{to_base36(0)}", message_id=100)
    context = _context(
        db_with_user,
        {
            "diet_meal_type": "lunch",
            "diet_items": items,
            "diet_ui_message_id": 100,
            "diet_ui_revision": 0,
        },
    )
    update = _cb_update(query)
    activate_conversation(update, context, "diet")

    state = await diet.save_from_picker_p1(update, context)

    assert state == diet.LOG_ANOTHER
    logs = await db_with_user.get_diet_logs(
        user_id, today_local() - timedelta(days=1), today_local()
    )
    assert [row["calories"] for row in logs] == [350]
    assert "diet_items" not in context.user_data


async def test_a_stale_picker_save_stays_on_the_picker(db_with_user, user_id):
    """A stale tap must not silently move the user to the draft screen's state."""
    query = _query(f"dsave_{to_base36(user_id)}_{to_base36(0)}", message_id=55)
    context = _context(
        db_with_user,
        {
            "diet_meal_type": "lunch",
            "diet_items": [_item("dal", 200, 9, 30, 1)],
            "diet_ui_message_id": 100,  # a newer message owns the flow
            "diet_ui_revision": 0,
        },
    )
    update = _cb_update(query)
    activate_conversation(update, context, "diet")

    state = await diet.save_from_picker_p1(update, context)

    assert state == diet.FOOD_CHOICE
    assert len(context.user_data["diet_items"]) == 1  # nothing written or lost


async def test_cancel_names_what_it_discards():
    context = _context(
        SimpleNamespace(),
        {
            "diet_meal_type": "lunch",
            "diet_items": [_item("dal", 200, 9, 30, 1), _item("rice", 150, 3, 33, 0)],
            "diet_ui_message_id": 100,
        },
    )
    query = _query(f"dcancel_{USER}", message_id=100)
    update = _cb_update(query)
    activate_conversation(update, context, "diet")

    await diet.cancel_diet_callback(update, context)

    assert "2 items discarded" in query.message.reply_text.await_args.args[0]


async def test_cancel_with_nothing_drafted_stays_terse():
    context = _context(SimpleNamespace(), {"diet_ui_message_id": 100})
    query = _query(f"dcancel_{USER}", message_id=100)
    update = _cb_update(query)
    activate_conversation(update, context, "diet")

    await diet.cancel_diet_callback(update, context)

    assert query.message.reply_text.await_args.args[0] == "✖️ Cancelled."
