"""End-to-end saved-food and recipe logging through the diet handler."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import ConversationHandler

from bot.config import today_local
from bot.handlers.common import activate_conversation
from bot.handlers.diet import diet_command, receive_food_items


def _message(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(text=text, reply_text=AsyncMock())


def _update(user_id: int, message: SimpleNamespace | None = None) -> SimpleNamespace:
    message = message or _message()
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(
            id=user_id,
            username="catalog-user",
            first_name="Catalog",
        ),
        effective_chat=SimpleNamespace(id=user_id),
    )


def _context(db, args: list[str], user_data: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        bot_data={"db": db},
        args=args,
        user_data=user_data if user_data is not None else {},
    )


async def _save_apple(db, user_id: int) -> dict:
    result = await db.save_food(
        user_id,
        "apple",
        "g",
        100,
        calories=52,
        protein_g=0.3,
        carbs_g=14,
        fat_g=0.2,
    )
    food = result["food"]
    await db.save_food_portion(user_id, food["id"], "medium", 182, "g")
    await db.save_food_portion(user_id, food["id"], "piece", 182, "g")
    await db.save_food_portion(user_id, food["id"], "serving", 150, "g")
    return food


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quantity", "description", "calories", "protein", "carbs", "fat"),
    [
        (["1", "medium"], "1 medium apple", 95, 0.55, 25.48, 0.36),
        (["220gm"], "220 gm apple", 114, 0.66, 30.8, 0.44),
        (["0.22", "kg"], "0.22 kg apple", 114, 0.66, 30.8, 0.44),
        (["2pcs"], "2 pcs apple", 189, 1.09, 50.96, 0.73),
        (["1serving"], "1 serving apple", 78, 0.45, 21.0, 0.3),
    ],
)
async def test_saved_food_logs_named_and_metric_quantities(
    db_with_user,
    user_id: int,
    quantity: list[str],
    description: str,
    calories: int,
    protein: float,
    carbs: float,
    fat: float,
) -> None:
    await _save_apple(db_with_user, user_id)
    message = _message()
    context = _context(
        db_with_user,
        ["snack", "food:apple", *quantity],
    )

    result = await diet_command(_update(user_id, message), context)

    assert result == ConversationHandler.END
    rows = await db_with_user.get_diet_logs(
        user_id, today_local(), today_local()
    )
    row = rows[-1]
    assert row["calories"] == calories
    assert row["protein_g"] == pytest.approx(protein)
    assert row["carbs_g"] == pytest.approx(carbs)
    assert row["fat_g"] == pytest.approx(fat)
    assert row["food_items"] == description
    assert "Diet logged" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_recipe_log_aggregates_ingredients_and_scales_by_yield(
    db_with_user, user_id: int
) -> None:
    apple = await _save_apple(db_with_user, user_id)
    oats = (
        await db_with_user.save_food(
            user_id,
            "oats",
            "g",
            100,
            calories=389,
            protein_g=16.9,
            carbs_g=66.3,
            fat_g=6.9,
        )
    )["food"]
    recipe = (
        await db_with_user.save_recipe(user_id, "apple-oats", 2, "serving")
    )["recipe"]
    await db_with_user.save_recipe_ingredient(
        user_id, recipe["id"], apple["id"], 182, "g", 1, "medium"
    )
    await db_with_user.save_recipe_ingredient(
        user_id, recipe["id"], oats["id"], 100, "g", 100, "g"
    )

    context = _context(
        db_with_user,
        ["breakfast", "recipe:apple-oats", "1", "serving"],
    )
    result = await diet_command(_update(user_id), context)

    assert result == ConversationHandler.END
    row = (
        await db_with_user.get_diet_logs(
            user_id, today_local(), today_local()
        )
    )[-1]
    assert row["food_items"] == "1 serving apple-oats (recipe)"
    assert row["calories"] == 242
    assert row["protein_g"] == pytest.approx(8.72)
    assert row["carbs_g"] == pytest.approx(45.89)
    assert row["fat_g"] == pytest.approx(3.63)


@pytest.mark.asyncio
async def test_archived_food_is_not_directly_loggable_but_existing_recipe_works(
    db_with_user, user_id: int
) -> None:
    apple = await _save_apple(db_with_user, user_id)
    recipe = (
        await db_with_user.save_recipe(user_id, "baked-apple", 1, "serving")
    )["recipe"]
    await db_with_user.save_recipe_ingredient(
        user_id, recipe["id"], apple["id"], 182, "g", 1, "medium"
    )
    await db_with_user.archive_food(user_id, apple["id"])

    direct_message = _message()
    direct_result = await diet_command(
        _update(user_id, direct_message),
        _context(db_with_user, ["snack", "food:apple", "1", "medium"]),
    )
    recipe_result = await diet_command(
        _update(user_id),
        _context(
            db_with_user,
            ["snack", "recipe:baked-apple", "1", "serving"],
        ),
    )

    assert direct_result == ConversationHandler.END
    assert "not found" in direct_message.reply_text.await_args.args[0]
    assert recipe_result == ConversationHandler.END
    rows = await db_with_user.get_diet_logs(
        user_id, today_local(), today_local()
    )
    assert [row["food_items"] for row in rows] == [
        "1 serving baked-apple (recipe)"
    ]


@pytest.mark.asyncio
async def test_recipe_dimension_mismatch_and_empty_recipe_do_not_log(
    db_with_user, user_id: int
) -> None:
    await db_with_user.save_recipe(user_id, "empty", 2, "serving")

    mismatch_message = _message()
    await diet_command(
        _update(user_id, mismatch_message),
        _context(db_with_user, ["lunch", "recipe:empty", "100g"]),
    )
    empty_message = _message()
    await diet_command(
        _update(user_id, empty_message),
        _context(db_with_user, ["lunch", "recipe:empty", "1serving"]),
    )

    assert "different dimension" in mismatch_message.reply_text.await_args.args[0]
    assert "no ingredients" in empty_message.reply_text.await_args.args[0]
    assert not await db_with_user.get_diet_logs(
        user_id, today_local(), today_local()
    )


@pytest.mark.asyncio
async def test_guided_food_step_can_resolve_a_saved_food_directly(
    db_with_user, user_id: int
) -> None:
    await _save_apple(db_with_user, user_id)
    message = _message("food:apple 220g")
    update = _update(user_id, message)
    context = _context(
        db_with_user,
        [],
        user_data={"diet_meal_type": "snack"},
    )
    activate_conversation(update, context, "diet")

    result = await receive_food_items(update, context)

    assert result == ConversationHandler.END
    assert context.user_data == {}
    row = (
        await db_with_user.get_diet_logs(
            user_id, today_local(), today_local()
        )
    )[-1]
    assert row["calories"] == 114


@pytest.mark.asyncio
async def test_catalog_logs_are_snapshots_when_food_definition_changes(
    db_with_user, user_id: int
) -> None:
    await _save_apple(db_with_user, user_id)
    await diet_command(
        _update(user_id),
        _context(db_with_user, ["snack", "food:apple", "100g"]),
    )
    await db_with_user.save_food(
        user_id,
        "apple",
        "g",
        100,
        calories=60,
        protein_g=1,
        carbs_g=15,
        fat_g=0.5,
    )

    rows = await db_with_user.get_diet_logs(
        user_id, today_local(), today_local()
    )
    assert len(rows) == 1
    assert rows[0]["calories"] == 52
    assert rows[0]["protein_g"] == pytest.approx(0.3)
