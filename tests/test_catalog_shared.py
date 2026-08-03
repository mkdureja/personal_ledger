"""Phase 5: shared curated catalog — seed, search, resolve, and logging."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType

from bot.catalog_seed import CATALOG_FOODS, CATALOG_PROVIDER, CATALOG_REVISION
from bot.config import today_local
from bot.handlers import diet
from bot.handlers.catalog import resolve_catalog_food_entry

USER = 123456789
pytestmark = pytest.mark.asyncio


@pytest.fixture
async def seeded(db_with_user):
    await db_with_user.seed_catalog(CATALOG_FOODS)
    return db_with_user


# ---------------------------------------------------------------------------
# DB-level: seed, search, resolve
# ---------------------------------------------------------------------------
async def test_seed_is_idempotent(seeded):
    await seeded.seed_catalog(CATALOG_FOODS)  # second run
    cursor = await seeded.conn.execute("SELECT COUNT(*) AS n FROM catalog_foods")
    assert (await cursor.fetchone())["n"] == len(CATALOG_FOODS)


async def test_the_branded_items_resolve_at_their_declared_values(seeded):
    """Pack values, typed the way each product is actually measured.

    Both are branded rows whose numbers come from a label rather than a public
    average, so a silent edit here would put wrong macros under real meals.
    """
    bread = (await seeded.search_catalog("protein bread"))[0]
    entry = resolve_catalog_food_entry(
        bread, await seeded.get_catalog_portions(bread["id"]), ["100", "g"]
    )
    assert (entry.calories, entry.protein_g, entry.carbs_g, entry.fat_g) == (
        235, 18.4, 40.7, 2.0
    )

    whey = (await seeded.search_catalog("whey isolate"))[0]
    portions = await seeded.get_catalog_portions(whey["id"])
    one = resolve_catalog_food_entry(whey, portions, ["1", "scoop"])
    assert (one.calories, one.protein_g, one.carbs_g, one.fat_g) == (
        135, 30.0, 1.5, 0.6
    )
    # The daily case: more than one scoop, typed as a plural.
    two = resolve_catalog_food_entry(whey, portions, ["2", "scoops"])
    assert (two.calories, two.protein_g) == (270, 60.0)


async def test_a_generic_bread_query_offers_both_rather_than_guessing(seeded):
    """Adding a branded bread must not silently capture "bread"."""
    names = {row["name"] for row in await seeded.search_catalog("bread")}

    assert {"Bread slice", "Protein Chef protein bread"} <= names


async def test_search_matches_name_prefix_and_alias(seeded):
    by_name = await seeded.search_catalog("app")
    assert any(r["name"] == "Apple" for r in by_name)
    # 'chapati' is an alias of Roti / chapati.
    by_alias = await seeded.search_catalog("chapati")
    assert any("Roti" in r["name"] for r in by_alias)


async def test_catalog_resolve_matches_and_carries_provenance(seeded):
    apple = (await seeded.search_catalog("apple"))[0]
    portions = await seeded.get_catalog_portions(apple["id"])
    entry = resolve_catalog_food_entry(apple, portions, ["1", "medium"])
    assert entry.calories == 95  # 52 cal/100g * 182g
    assert entry.source_type == "catalog"
    assert entry.source_provider == CATALOG_PROVIDER
    assert entry.source_revision == CATALOG_REVISION


async def test_catalog_is_shared_but_logs_are_owner_scoped(seeded, user_id):
    # Search works for any user id (shared reference data).
    assert await seeded.search_catalog("banana")
    apple = (await seeded.search_catalog("apple"))[0]
    entry = resolve_catalog_food_entry(
        apple, await seeded.get_catalog_portions(apple["id"]), ["100", "g"]
    )
    header_id = await seeded.log_diet_with_items(user_id, "snack", [entry.as_item()])
    items = await seeded.get_diet_log_items(user_id, header_id)
    assert items[0]["source_type"] == "catalog"
    assert items[0]["source_provider"] == CATALOG_PROVIDER
    # Another user sees none of this meal's items.
    assert await seeded.get_diet_log_items(user_id + 1, header_id) == []


# ---------------------------------------------------------------------------
# Flow: search -> pick catalog food -> quantity -> save
# ---------------------------------------------------------------------------
def _query(data, *, message_id=100):
    return SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=SimpleNamespace(
            message_id=message_id,
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=777)),
        ),
        edit_message_reply_markup=AsyncMock(),
    )


def _cb_update(query, uid=USER):
    return SimpleNamespace(
        callback_query=query,
        effective_message=query.message,
        effective_user=SimpleNamespace(id=uid, username="u", first_name="U"),
        effective_chat=SimpleNamespace(id=uid, type=ChatType.PRIVATE),
    )


def _msg_update(text, uid=USER):
    message = SimpleNamespace(
        text=text,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=777)),
    )
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=uid, username="u", first_name="U"),
        effective_chat=SimpleNamespace(id=uid, type=ChatType.PRIVATE),
    )


async def test_search_then_log_a_catalog_food(seeded, user_id):
    ctx = SimpleNamespace(
        bot_data={"db": seeded},
        user_data={"diet_meal_type": "snack", "diet_ui_message_id": 100},
    )

    # Type a search query -> results keyboard, state returns to FOOD_CHOICE.
    result = await diet.receive_search_query(_msg_update("banana", uid=user_id), ctx)
    assert result == diet.FOOD_CHOICE

    banana = (await seeded.search_catalog("banana"))[0]
    ctx.user_data["diet_ui_message_id"] = 777  # the results message id
    pick = _query(f"dcatalog_{user_id}_{banana['id']}", message_id=777)
    result = await diet.choose_catalog(_cb_update(pick, uid=user_id), ctx)
    assert result == diet.PORTION_CHOICE
    assert ctx.user_data["diet_sel_kind"] == "catalog"
    assert ctx.user_data["diet_sel_id"] == banana["id"]

    # Enter a custom amount -> preview (one item in the draft).
    result = await diet.receive_custom_amount(_msg_update("100 g", uid=user_id), ctx)
    assert result == diet.CONFIRM_ITEM
    item = ctx.user_data["diet_items"][-1]
    assert item["source_type"] == "catalog"
    assert item["calories"] == 89  # banana 89 cal/100g

    # Save the meal.
    ctx.user_data["diet_ui_message_id"] = 777
    from bot.handlers.common import activate_conversation

    save = _cb_update(_query(f"dsave_{user_id}", message_id=777), uid=user_id)
    activate_conversation(save, ctx, "diet")
    result = await diet.save_item(save, ctx)
    assert result == diet.LOG_ANOTHER

    rows = await seeded.get_diet_logs(user_id, today_local(), today_local())
    assert len(rows) == 1
    items = await seeded.get_diet_log_items(user_id, rows[-1]["id"])
    assert items[0]["source_type"] == "catalog"
    assert items[0]["source_provider"] == CATALOG_PROVIDER
