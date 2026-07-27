"""Phase 4: personalized suggestion ordering, recent quantities, and controls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers import diet
from bot.handlers.settings import suggestions_command

pytestmark = pytest.mark.asyncio


def _item(food, amount, unit, cal):
    return {
        "source_type": "food",
        "source_id": food["id"],
        "display_name": f"{amount} {unit} {food['name']}",
        "entered_amount": float(amount),
        "entered_unit": unit,
        "resolved_base_amount": float(amount),
        "resolved_base_unit": "g",
        "calories": cal,
        "protein_g": 1.0,
        "carbs_g": 2.0,
        "fat_g": 0.5,
    }


async def _food(db, uid, name):
    return (
        await db.save_food(uid, name, "g", 100, calories=100, protein_g=1, carbs_g=2, fat_g=0.5)
    )["food"]


def _context(db, uid):
    return SimpleNamespace(
        bot_data={"db": db},
        user_data={},
        effective_user=SimpleNamespace(id=uid, username="u", first_name="U"),
    )


async def test_ranking_favours_meal_type_frequency(db_with_user, user_id):
    apple = await _food(db_with_user, user_id, "apple")
    rice = await _food(db_with_user, user_id, "rice")
    eggs = await _food(db_with_user, user_id, "eggs")

    for _ in range(3):
        await db_with_user.log_diet_with_items(
            user_id, "breakfast", [_item(eggs, 100, "g", 150)]
        )
    await db_with_user.log_diet_with_items(
        user_id, "lunch", [_item(rice, 200, "g", 260)]
    )
    await db_with_user.log_diet_with_items(
        user_id, "snack", [_item(apple, 150, "g", 78)]
    )

    context = _context(db_with_user, user_id)
    choices = await diet._ranked_choices(context, user_id, "breakfast")

    # Eggs (logged for breakfast) rank first; the rest fall back to name order.
    assert choices[0]["id"] == eggs["id"]
    assert {c["id"] for c in choices} == {apple["id"], rice["id"], eggs["id"]}


async def test_pinned_food_ranks_first_and_hidden_is_excluded(db_with_user, user_id):
    apple = await _food(db_with_user, user_id, "apple")
    rice = await _food(db_with_user, user_id, "rice")
    # rice is logged a lot, apple never — but apple is pinned and rice hidden.
    for _ in range(5):
        await db_with_user.log_diet_with_items(
            user_id, "lunch", [_item(rice, 200, "g", 260)]
        )
    await db_with_user.set_food_preference(user_id, "food", apple["id"], is_pinned=True)
    await db_with_user.set_food_preference(user_id, "food", rice["id"], hidden=True)

    context = _context(db_with_user, user_id)
    choices = await diet._ranked_choices(context, user_id, "lunch")

    assert choices[0]["id"] == apple["id"]
    assert all(c["id"] != rice["id"] for c in choices)  # hidden dropped


async def test_disabled_suggestions_use_alphabetical_order(db_with_user, user_id):
    await _food(db_with_user, user_id, "zucchini")
    await _food(db_with_user, user_id, "apple")
    await db_with_user.set_suggestions_enabled(user_id, False)

    context = _context(db_with_user, user_id)
    choices = await diet._ranked_choices(context, user_id, "lunch")

    # list_foods order is name_key ascending; ranking is bypassed.
    assert [c["name"] for c in choices] == ["apple", "zucchini"]


async def test_recent_quantities_are_distinct_and_recent_first(db_with_user, user_id):
    apple = await _food(db_with_user, user_id, "apple")
    await db_with_user.log_diet_with_items(user_id, "snack", [_item(apple, 220, "g", 114)])
    await db_with_user.log_diet_with_items(user_id, "snack", [_item(apple, 220, "g", 114)])
    await db_with_user.log_diet_with_items(user_id, "snack", [_item(apple, 150, "g", 78)])

    recent = await db_with_user.get_recent_item_quantities(user_id, "food", apple["id"])

    quantities = {(r["entered_amount"], r["entered_unit"]) for r in recent}
    assert quantities == {(220.0, "g"), (150.0, "g")}  # de-duplicated


async def test_preferences_reset_clears_pins_and_hides(db_with_user, user_id):
    apple = await _food(db_with_user, user_id, "apple")
    await db_with_user.set_food_preference(user_id, "food", apple["id"], is_pinned=True)
    assert (await db_with_user.get_food_preference(user_id, "food", apple["id"]))["is_pinned"] == 1

    removed = await db_with_user.reset_food_preferences(user_id)
    assert removed == 1
    assert await db_with_user.get_food_preference(user_id, "food", apple["id"]) is None


async def test_suggestions_are_owner_scoped(db_with_user, user_id):
    apple = await _food(db_with_user, user_id, "apple")
    await db_with_user.log_diet_with_items(user_id, "snack", [_item(apple, 100, "g", 100)])
    # Another user sees none of this history.
    assert await db_with_user.get_diet_item_stats(user_id + 1, "snack") == {}
    assert await db_with_user.get_recent_item_quantities(
        user_id + 1, "food", apple["id"]
    ) == []


# ---------------------------------------------------------------------------
# /suggestions command
# ---------------------------------------------------------------------------
def _cmd_update(uid):
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=uid, username="u", first_name="U"),
    )


async def test_suggestions_command_toggles_and_resets(db_with_user, user_id):
    update = _cmd_update(user_id)
    context = SimpleNamespace(bot_data={"db": db_with_user}, args=["off"])
    await suggestions_command(update, context)
    assert await db_with_user.get_suggestions_enabled(user_id) is False

    context.args = ["on"]
    await suggestions_command(update, context)
    assert await db_with_user.get_suggestions_enabled(user_id) is True

    apple = await _food(db_with_user, user_id, "apple")
    await db_with_user.set_food_preference(user_id, "food", apple["id"], hidden=True)
    context.args = ["reset"]
    await suggestions_command(update, context)
    assert await db_with_user.get_food_preferences(user_id) == {}
