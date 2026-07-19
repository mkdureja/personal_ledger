"""Command and registration coverage for saved foods and recipes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ParseMode
from telegram.ext import CommandHandler

from bot import main as main_module
from bot.handlers.catalog import (
    _nutrient_summary,
    food_command,
    recipe_command,
    resolve_catalog_diet_entry,
)
from bot.handlers.common import activate_conversation


def _message(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(text=text, reply_text=AsyncMock())


def _update(
    user_id: int,
    message: SimpleNamespace | None = None,
    *,
    chat_id: int | None = None,
) -> SimpleNamespace:
    message = message or _message()
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(
            id=user_id,
            username="catalog-user",
            first_name="Catalog",
        ),
        effective_chat=SimpleNamespace(id=user_id if chat_id is None else chat_id),
    )


def _context(db, args: list[str], user_data: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        bot_data={"db": db},
        args=args,
        user_data=user_data if user_data is not None else {},
    )


@pytest.mark.asyncio
async def test_food_commands_add_portion_show_unportion_and_archive(
    db_with_user, user_id: int
) -> None:
    add_message = _message()
    await food_command(
        _update(user_id, add_message),
        _context(
            db_with_user,
            ["add", "apple", "per=100g", "kcal=52", "p=0.3", "c=14", "f=0.2"],
        ),
    )
    food = await db_with_user.get_food_by_key(user_id, "apple")
    assert food is not None
    assert food["base_unit"] == "g"
    assert food["basis_amount"] == 100
    assert "Added food" in add_message.reply_text.await_args.args[0]
    assert add_message.reply_text.await_args.kwargs["parse_mode"] == ParseMode.HTML

    await food_command(
        _update(user_id),
        _context(db_with_user, ["portion", "apple", "medium=182g"]),
    )
    portions = await db_with_user.get_food_portions(user_id, food["id"])
    assert [(row["name_key"], row["base_amount"]) for row in portions] == [
        ("medium", 182)
    ]

    show_message = _message()
    await food_command(
        _update(user_id, show_message),
        _context(db_with_user, ["show", "apple"]),
    )
    assert "medium" in show_message.reply_text.await_args.args[0]
    assert "P 0.3g" in show_message.reply_text.await_args.args[0]

    await food_command(
        _update(user_id),
        _context(db_with_user, ["unportion", "apple", "medium"]),
    )
    assert await db_with_user.get_food_portions(user_id, food["id"]) == []

    await food_command(
        _update(user_id),
        _context(db_with_user, ["remove", "apple"]),
    )
    assert await db_with_user.get_food_by_key(user_id, "apple") is None


@pytest.mark.asyncio
async def test_recipe_commands_add_update_ingredient_show_remove_and_archive(
    db_with_user, user_id: int
) -> None:
    food = (
        await db_with_user.save_food(
            user_id,
            "apple",
            "g",
            100,
            calories=52,
            protein_g=0.3,
            carbs_g=14,
            fat_g=0.2,
        )
    )["food"]
    await db_with_user.save_food_portion(user_id, food["id"], "medium", 182, "g")

    await recipe_command(
        _update(user_id),
        _context(db_with_user, ["add", "baked-apple", "yield=2servings"]),
    )
    recipe = await db_with_user.get_recipe_by_key(user_id, "baked-apple")
    assert recipe is not None
    assert (recipe["yield_amount"], recipe["yield_unit"]) == (2, "serving")

    await recipe_command(
        _update(user_id),
        _context(
            db_with_user,
            ["ingredient", "baked-apple", "food:apple", "1", "medium"],
        ),
    )
    ingredients = await db_with_user.get_recipe_ingredients(user_id, recipe["id"])
    assert len(ingredients) == 1
    assert ingredients[0]["base_amount"] == 182

    await recipe_command(
        _update(user_id),
        _context(
            db_with_user,
            ["ingredient", "baked-apple", "food:apple", "220gm"],
        ),
    )
    ingredients = await db_with_user.get_recipe_ingredients(user_id, recipe["id"])
    assert len(ingredients) == 1
    assert ingredients[0]["base_amount"] == 220
    assert ingredients[0]["display_unit"] == "gm"

    show_message = _message()
    await recipe_command(
        _update(user_id, show_message),
        _context(db_with_user, ["show", "baked-apple"]),
    )
    rendered = show_message.reply_text.await_args.args[0]
    assert "apple" in rendered
    assert "Total nutrition" in rendered

    await recipe_command(
        _update(user_id),
        _context(
            db_with_user,
            ["removeitem", "baked-apple", "food:apple"],
        ),
    )
    assert await db_with_user.get_recipe_ingredients(user_id, recipe["id"]) == []

    await recipe_command(
        _update(user_id),
        _context(db_with_user, ["remove", "baked-apple"]),
    )
    assert await db_with_user.get_recipe_by_key(user_id, "baked-apple") is None


@pytest.mark.asyncio
async def test_catalog_commands_are_blocked_during_guided_flow(user_id: int) -> None:
    db = SimpleNamespace(ensure_user=AsyncMock(), list_foods=AsyncMock())
    update = _update(user_id)
    context = _context(db, ["list"])
    activate_conversation(update, context, "diet")

    await food_command(update, context)

    db.ensure_user.assert_awaited_once()
    db.list_foods.assert_not_awaited()
    assert "Finish the current guided log" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_catalog_block_explains_cross_chat_cancel(user_id: int) -> None:
    db = SimpleNamespace(ensure_user=AsyncMock(), list_foods=AsyncMock())
    context = _context(db, ["list"])
    activate_conversation(_update(user_id, chat_id=111), context, "diet")
    message = _message()

    await food_command(_update(user_id, message, chat_id=222), context)

    db.list_foods.assert_not_awaited()
    assert "use /cancel in the chat where it started" in (
        message.reply_text.await_args.args[0]
    )


@pytest.mark.asyncio
async def test_recipe_show_preserves_quantity_provenance_and_marks_archived_food(
    db_with_user, user_id: int
) -> None:
    food = (
        await db_with_user.save_food(
            user_id, "apple", "g", 100, calories=52
        )
    )["food"]
    await db_with_user.save_food_portion(user_id, food["id"], "medium", 182, "g")
    recipe = (
        await db_with_user.save_recipe(user_id, "apple-pie", 1, "serving")
    )["recipe"]
    await db_with_user.save_recipe_ingredient(
        user_id, recipe["id"], food["id"], 182, "g", 1, "medium"
    )

    await db_with_user.save_food_portion(user_id, food["id"], "medium", 200, "g")
    active_message = _message()
    await recipe_command(
        _update(user_id, active_message),
        _context(db_with_user, ["show", "apple-pie"]),
    )
    active_rendered = active_message.reply_text.await_args.args[0]
    assert "1 medium" in active_rendered
    assert "resolved: 182 g" in active_rendered
    assert "archived food definition" not in active_rendered

    await db_with_user.archive_food(user_id, food["id"])
    await db_with_user.save_food(user_id, "apple", "g", 100, calories=80)
    archived_message = _message()
    await recipe_command(
        _update(user_id, archived_message),
        _context(db_with_user, ["show", "apple-pie"]),
    )
    archived_rendered = archived_message.reply_text.await_args.args[0]
    assert "resolved: 182 g" in archived_rendered
    assert "archived food definition" in archived_rendered


@pytest.mark.asyncio
async def test_food_list_is_chunked_and_html_escaped(user_id: int) -> None:
    foods = [
        {
            "name_key": f"food-{index}<unsafe>",
            "basis_amount": 100,
            "base_unit": "g",
            "calories": 50,
            "protein_g": 1,
            "carbs_g": 2,
            "fat_g": 3,
        }
        for index in range(120)
    ]
    db = SimpleNamespace(ensure_user=AsyncMock(), list_foods=AsyncMock(return_value=foods))
    message = _message()

    await food_command(_update(user_id, message), _context(db, ["list"]))

    assert message.reply_text.await_count > 1
    for call in message.reply_text.await_args_list:
        text = call.args[0]
        assert len(text.encode("utf-16-le")) // 2 <= 3_800
        assert "<unsafe>" not in text
        assert "&lt;unsafe&gt;" in text
        assert call.kwargs["parse_mode"] == ParseMode.HTML


@pytest.mark.asyncio
async def test_catalog_validation_error_is_bounded_for_telegram(user_id: int) -> None:
    db = SimpleNamespace(ensure_user=AsyncMock(), save_food=AsyncMock())
    message = _message()

    await food_command(
        _update(user_id, message),
        _context(db, ["add", "apple", "per=100g", "x=" + "a" * 4_000]),
    )

    text = message.reply_text.await_args.args[0]
    assert len(text.encode("utf-16-le")) // 2 < 4_096
    assert text.endswith("…")
    db.save_food.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_lookup_is_exact_and_rejects_reserved_portion(
    db_with_user, user_id: int
) -> None:
    await db_with_user.save_food(user_id, "apple", "g", 100, calories=52)
    missing_message = _message()
    await food_command(
        _update(user_id, missing_message),
        _context(db_with_user, ["show", "app"]),
    )
    assert "'app' was not found" in missing_message.reply_text.await_args.args[0]

    reserved_message = _message()
    await food_command(
        _update(user_id, reserved_message),
        _context(db_with_user, ["portion", "apple", "grams=20g"]),
    )
    assert "standard base unit" in reserved_message.reply_text.await_args.args[0]
    food = await db_with_user.get_food_by_key(user_id, "apple")
    assert await db_with_user.get_food_portions(user_id, food["id"]) == []

    await food_command(
        _update(user_id),
        _context(db_with_user, ["portion", "apple", "pcs=182g"]),
    )
    portions = await db_with_user.get_food_portions(user_id, food["id"])
    assert [(row["name_key"], row["base_amount"]) for row in portions] == [
        ("piece", 182)
    ]
    await food_command(
        _update(user_id),
        _context(db_with_user, ["unportion", "apple", "pcs"]),
    )
    assert await db_with_user.get_food_portions(user_id, food["id"]) == []


@pytest.mark.asyncio
async def test_cross_dimension_alias_command_normalizes_and_resolves(
    db_with_user, user_id: int
) -> None:
    await food_command(
        _update(user_id),
        _context(db_with_user, ["add", "soup", "per=100ml", "kcal=50"]),
    )
    await food_command(
        _update(user_id),
        _context(db_with_user, ["portion", "soup", "kg=1000ml"]),
    )
    soup = await db_with_user.get_food_by_key(user_id, "soup")
    portions = await db_with_user.get_food_portions(user_id, soup["id"])
    assert [(row["name_key"], row["base_amount"]) for row in portions] == [
        ("g", 1)
    ]

    resolved = await resolve_catalog_diet_entry(
        db_with_user, user_id, "food:soup", ["1kg"]
    )
    assert resolved.display_text == "1 kg soup"
    assert resolved.calories == 500


def test_main_registers_food_and_recipe_commands(monkeypatch) -> None:
    class FakeApplication:
        def __init__(self) -> None:
            self.handlers = []

        def add_handler(self, handler) -> None:
            self.handlers.append(handler)

        def add_error_handler(self, _handler) -> None:
            pass

        def run_polling(self, **_kwargs) -> None:
            pass

    application = FakeApplication()

    class FakeBuilder:
        def token(self, _value):
            return self

        def post_init(self, _value):
            return self

        def post_shutdown(self, _value):
            return self

        def build(self):
            return application

    monkeypatch.setattr(main_module, "ApplicationBuilder", FakeBuilder)
    main_module.main()

    commands = {
        command
        for handler in application.handlers
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    assert {"food", "recipe"} <= commands


def test_catalog_nutrient_display_bounds_repeating_decimals() -> None:
    text = _nutrient_summary(
        {
            "calories": "0.3333333333333333333333333333",
            "protein_g": "0.6666666666666666666666666667",
            "carbs_g": 1,
            "fat_g": None,
        }
    )

    assert "kcal 0.33" in text
    assert "P 0.67g" in text
    assert "3333333333" not in text

    edge_text = _nutrient_summary(
        {
            "calories": "1e30",
            "protein_g": "0.001",
            "carbs_g": "0.004",
            "fat_g": "0.00001",
        }
    )
    assert "kcal 1e+30" in edge_text
    assert "P 0.001g" in edge_text
    assert "C 0.004g" in edge_text
    assert "F 0.00001g" in edge_text
