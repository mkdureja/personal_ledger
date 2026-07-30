"""Fixes from the first live smoke test, plus Phase 6 Duplicate Recipe.

The smoke test lost an assembled meal draft to a five-minute idle timeout whose
message did not say what had been discarded. These cover that, the ambiguous
per-item delete button, and the recipe-variant clone (plan §12 Phase 6).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType
from telegram.ext import ConversationHandler

from bot import config
from bot.callback_data import to_base36
from bot.config import ALLOWED_USER_IDS
from bot.handlers import catalog, diet, settings, start
from bot.keyboards import diet_save_keyboard
from bot.nutrition import NutritionError

UID = next(iter(ALLOWED_USER_IDS))
OTHER_UID = UID + 1


def _enable(monkeypatch):
    monkeypatch.setattr("bot.config.PHASE1_ENABLED_USER_IDS", frozenset({UID}))
    monkeypatch.setattr("bot.config.HOME_KEYBOARD_MODE", "on")
    monkeypatch.setattr("bot.config.HOME_KEYBOARD_PILOT_USER_IDS", frozenset())


def _update():
    message = SimpleNamespace(
        message_id=50, text=None, reply_text=AsyncMock(return_value=SimpleNamespace(message_id=51))
    )
    return SimpleNamespace(
        update_id=1,
        effective_message=message,
        message=message,
        callback_query=None,
        effective_user=SimpleNamespace(id=UID, first_name="Manoj", username="m"),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
    )


def _context(db=None, **user_data):
    return SimpleNamespace(bot_data={"db": db}, user_data=dict(user_data), args=[])


def _text(update):
    return update.effective_message.reply_text.call_args.args[0]


# ---------------------------------------------------------------------------
# The idle window itself
# ---------------------------------------------------------------------------
def test_idle_window_is_long_enough_to_assemble_a_meal():
    # Five minutes discarded a real draft mid-use; this guards the widened value.
    assert config.CONVERSATION_TIMEOUT >= 900


# ---------------------------------------------------------------------------
# Timeout now names what it threw away
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_timeout_lists_the_discarded_builder_draft():
    update = _update()
    context = _context(
        diet_items=[
            {"display_name": "1 medium Banana"},
            {"display_name": "2 bowl Dal"},
        ],
        diet_meal_type="dinner",
    )

    state = await diet.diet_timeout_handler(update, context)

    assert state == ConversationHandler.END
    text = _text(update)
    assert "nothing was logged" in text
    assert "1 medium Banana" in text and "2 bowl Dal" in text
    # State is released, so the next Meal tap starts clean.
    assert "diet_items" not in context.user_data


@pytest.mark.asyncio
async def test_timeout_lists_a_pending_quick_item():
    update = _update()
    context = _context(
        diet_quick_pending_item={"display_name": "1 bowl Dal", "source_type": "food"}
    )
    await diet.diet_timeout_handler(update, context)
    assert "1 bowl Dal" in _text(update)


@pytest.mark.asyncio
async def test_timeout_bounds_a_long_draft_list():
    update = _update()
    context = _context(
        diet_items=[{"display_name": f"Item {n}"} for n in range(1, 9)]
    )
    await diet.diet_timeout_handler(update, context)
    text = _text(update)
    assert "and 3 more" in text
    assert "Item 6" not in text


@pytest.mark.asyncio
async def test_timeout_without_a_draft_says_so_plainly():
    update = _update()
    await diet.diet_timeout_handler(update, _context())
    text = _text(update)
    assert "nothing was logged" in text
    assert "You had" not in text


@pytest.mark.asyncio
async def test_timeout_escapes_a_draft_name():
    update = _update()
    context = _context(diet_items=[{"display_name": "<b>x</b>"}])
    await diet.diet_timeout_handler(update, context)
    assert "&lt;b&gt;x&lt;/b&gt;" in _text(update)


def test_diet_conversation_uses_the_draft_aware_timeout():
    handler = diet.diet_conv_handler.states[ConversationHandler.TIMEOUT][0]
    assert handler.callback is diet.diet_timeout_handler


# ---------------------------------------------------------------------------
# Per-item delete button is unambiguous
# ---------------------------------------------------------------------------
def test_each_draft_row_names_its_own_item():
    items = [
        {"display_name": "Dal", "source_type": "food", "source_id": 1},
        {"display_name": "Rice", "source_type": "food", "source_id": 2},
        {"display_name": "Note", "source_type": "freetext"},
    ]
    markup = diet_save_keyboard(UID, phase1_enabled=True, revision=1, items=items)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    # Every per-item control carries its position, including the bin.
    assert "#1 🗑" in labels and "#2 🗑" in labels and "#3 🗑" in labels
    assert "#3 ✍️ amount" not in labels  # free text has no amount


# ---------------------------------------------------------------------------
# /help and /settings Phase 1 surfaces
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_help_shows_fast_logging_only_when_enabled(monkeypatch):
    off = _update()
    await start.help_command(off, _context())
    assert "Fast logging" not in _text(off)

    _enable(monkeypatch)
    on = _update()
    await start.help_command(on, _context())
    text = _text(on)
    assert "Fast logging" in text
    assert "/keyboard hide|show" in text
    assert "Repeat" in text
    # Unbuilt features must not be advertised.
    for absent in ("Describe", "voice", "forget", "reset-all"):
        assert absent not in text


@pytest.mark.asyncio
async def test_settings_reports_phase1_and_usual_count(db_with_user, monkeypatch):
    _enable(monkeypatch)
    db = db_with_user
    saved = await db.save_food(UID, "Dal", "g", 100, calories=120)
    from bot.meal_models import DefaultQuantity

    await db.set_default_quantity(
        UID, "food", saved["food"]["id"], DefaultQuantity(amount=2, unit="g")
    )
    update = _update()

    await settings.settings_command(update, _context(db))

    text = _text(update)
    assert "Fast logging" in text
    assert "Saved “usual” amounts: <b>1</b>" in text
    assert "Quick-action bar: <b>shown</b>" in text


@pytest.mark.asyncio
async def test_settings_omits_phase1_when_disabled(db_with_user):
    update = _update()
    await settings.settings_command(update, _context(db_with_user))
    assert "Fast logging" not in _text(update)


@pytest.mark.asyncio
async def test_usual_count_is_per_user(db):
    from bot.meal_models import DefaultQuantity

    await db.ensure_user(UID, "a", "A")
    await db.ensure_user(OTHER_UID, "b", "B")
    saved = await db.save_food(UID, "Dal", "g", 100, calories=120)
    await db.set_default_quantity(
        UID, "food", saved["food"]["id"], DefaultQuantity(amount=1, unit="g")
    )
    assert await db.count_default_quantities(UID) == 1
    assert await db.count_default_quantities(OTHER_UID) == 0


# ---------------------------------------------------------------------------
# Phase 6 - Duplicate Recipe
# ---------------------------------------------------------------------------
@pytest.fixture
async def recipe(db_with_user):
    """A two-ingredient recipe yielding 2 servings."""
    db = db_with_user
    dal = (await db.save_food(UID, "dal", "g", 100, calories=120))["food"]["id"]
    rice = (await db.save_food(UID, "rice", "g", 100, calories=130))["food"]["id"]
    await db.save_recipe(UID, "curry", 2, "serving")
    made = await db.get_recipe_by_key(UID, "curry")
    await db.save_recipe_ingredient(UID, made["id"], dal, 300, "g", 300, "g")
    await db.save_recipe_ingredient(UID, made["id"], rice, 200, "g", 200, "g")
    return made


@pytest.mark.asyncio
async def test_duplicate_copies_yield_and_every_ingredient(db_with_user, recipe):
    db = db_with_user
    result = await db.duplicate_recipe(UID, "curry", "curry-light")

    assert result["status"] == "added"
    assert result["ingredient_count"] == 2
    copy = result["recipe"]
    assert copy["id"] != recipe["id"]
    assert copy["yield_amount"] == recipe["yield_amount"]
    assert copy["yield_unit"] == recipe["yield_unit"]

    original_items = await db.get_recipe_ingredients(UID, recipe["id"])
    copied_items = await db.get_recipe_ingredients(UID, copy["id"])
    assert [i["food_id"] for i in copied_items] == [
        i["food_id"] for i in original_items
    ]
    assert [i["base_amount"] for i in copied_items] == [
        i["base_amount"] for i in original_items
    ]
    assert all(
        c["id"] != o["id"] for c, o in zip(copied_items, original_items, strict=True)
    )


@pytest.mark.asyncio
async def test_the_copy_is_independent(db_with_user, recipe):
    """Editing the variant must not touch the original — that is the point."""
    db = db_with_user
    copy = (await db.duplicate_recipe(UID, "curry", "curry-light"))["recipe"]
    ghee = (await db.save_food(UID, "ghee", "g", 100, calories=900))["food"]["id"]
    await db.save_recipe_ingredient(UID, copy["id"], ghee, 20, "g", 20, "g")

    assert len(await db.get_recipe_ingredients(UID, copy["id"])) == 3
    assert len(await db.get_recipe_ingredients(UID, recipe["id"])) == 2


@pytest.mark.asyncio
async def test_a_duplicate_can_itself_be_duplicated(db_with_user, recipe):
    db = db_with_user
    await db.duplicate_recipe(UID, "curry", "curry-light")
    again = await db.duplicate_recipe(UID, "curry-light", "curry-lighter")
    assert again["status"] == "added"
    assert again["ingredient_count"] == 2


@pytest.mark.asyncio
async def test_duplicate_rejects_an_existing_name(db_with_user, recipe):
    db = db_with_user
    await db.save_recipe(UID, "taken", 1, "serving")
    result = await db.duplicate_recipe(UID, "curry", "taken")
    assert result["status"] == "duplicate_name"
    assert result["recipe"] is None


@pytest.mark.asyncio
async def test_duplicate_rejects_copying_onto_itself(db_with_user, recipe):
    result = await db_with_user.duplicate_recipe(UID, "curry", "Curry")
    assert result["status"] == "duplicate_name"


@pytest.mark.asyncio
async def test_duplicate_of_a_missing_recipe_is_not_found(db_with_user):
    result = await db_with_user.duplicate_recipe(UID, "nope", "copy")
    assert result["status"] == "not_found"


@pytest.mark.asyncio
async def test_duplicate_cannot_read_another_users_recipe(db, recipe):
    await db.ensure_user(OTHER_UID, "b", "B")
    result = await db.duplicate_recipe(OTHER_UID, "curry", "stolen")
    assert result["status"] == "not_found"
    assert await db.get_recipe_by_key(OTHER_UID, "stolen") is None


@pytest.mark.asyncio
async def test_a_failed_duplicate_leaves_nothing_behind(
    db_with_user, recipe, monkeypatch
):
    db = db_with_user
    real_execute = db.conn.execute
    calls = {"n": 0}

    async def flaky(sql, *args, **kwargs):
        if "INSERT INTO recipe_ingredients" in sql:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk full")
        return await real_execute(sql, *args, **kwargs)

    monkeypatch.setattr(db.conn, "execute", flaky)
    with pytest.raises(RuntimeError, match="disk full"):
        await db.duplicate_recipe(UID, "curry", "curry-light")
    monkeypatch.undo()

    assert await db.get_recipe_by_key(UID, "curry-light") is None
    assert len(await db.get_recipe_ingredients(UID, recipe["id"])) == 2


@pytest.mark.asyncio
async def test_recipe_duplicate_command(db_with_user, recipe):
    message = SimpleNamespace(reply_text=AsyncMock())
    await catalog._recipe_duplicate(
        message, db_with_user, UID, ["duplicate", "curry", "curry-light"]
    )
    text = message.reply_text.call_args.args[0]
    assert "Duplicated recipe" in text
    assert "curry-light" in text
    assert "2 ingredient(s)" in text


@pytest.mark.asyncio
async def test_recipe_duplicate_command_reports_a_name_clash(db_with_user, recipe):
    message = SimpleNamespace(reply_text=AsyncMock())
    with pytest.raises(NutritionError, match="already a saved recipe"):
        await catalog._recipe_duplicate(
            message, db_with_user, UID, ["duplicate", "curry", "curry"]
        )


@pytest.mark.asyncio
async def test_recipe_duplicate_command_needs_two_names(db_with_user):
    message = SimpleNamespace(reply_text=AsyncMock())
    with pytest.raises(NutritionError, match="Usage"):
        await catalog._recipe_duplicate(
            message, db_with_user, UID, ["duplicate", "curry"]
        )


def test_recipe_usage_advertises_duplicate():
    assert "duplicate" in catalog._RECIPE_USAGE


# ---------------------------------------------------------------------------
# Catalog prompt no longer reads as "still searching"
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_catalog_pick_prompt_uses_the_food_icon(db_with_user, monkeypatch):
    _enable(monkeypatch)
    db = db_with_user
    from bot.catalog_seed import CATALOG_FOODS

    await db.seed_catalog(CATALOG_FOODS)
    hit = (await db.search_catalog(CATALOG_FOODS[0]["display_name"]))[0]

    sent = SimpleNamespace(message_id=61)
    message = SimpleNamespace(message_id=60, reply_text=AsyncMock(return_value=sent))
    query = SimpleNamespace(
        data=f"dcatalog_{UID}_{hit['id']}",
        message=message,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(
        update_id=2,
        callback_query=query,
        effective_message=message,
        message=None,
        effective_user=SimpleNamespace(id=UID, first_name="M", username="m"),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
    )
    context = _context(db, diet_ui_message_id=60, diet_ui_revision=0,
                       diet_meal_type="lunch")

    await diet.choose_catalog(update, context)

    text = message.reply_text.call_args.args[0]
    assert text.startswith("🥫")


def test_base36_helpers_are_not_reexported_from_diet():
    """Guard the import cleanup: tests must not reach base-36 through diet."""
    assert not hasattr(diet, "to_base36")
