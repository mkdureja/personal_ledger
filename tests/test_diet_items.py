"""Structured meal items (Phase 3): diet_logs header + diet_log_items children."""

from __future__ import annotations

import pytest

from bot.config import today_local
from bot.database import MutationSource

pytestmark = pytest.mark.asyncio


def _item(name, cal, p, c, f, *, source_type="food", source_id=1):
    return {
        "source_type": source_type,
        "source_id": source_id,
        "display_name": name,
        "entered_amount": 1.0,
        "entered_unit": "serving",
        "resolved_base_amount": 100.0,
        "resolved_base_unit": "g",
        "calories": cal,
        "protein_g": p,
        "carbs_g": c,
        "fat_g": f,
    }


async def test_meal_header_totals_are_the_sum_of_items(db_with_user, user_id):
    items = [
        _item("apple", 95, 0.5, 25.0, 0.3, source_id=1),
        _item("rice", 200, 4.0, 44.0, 0.4, source_id=2),
    ]
    header_id = await db_with_user.log_diet_with_items(user_id, "lunch", items)

    rows = await db_with_user.get_diet_logs(user_id, today_local(), today_local())
    assert len(rows) == 1  # analytics still counts one meal
    header = rows[-1]
    assert header["calories"] == 295
    assert header["protein_g"] == pytest.approx(4.5)
    assert header["carbs_g"] == pytest.approx(69.0)
    assert header["fat_g"] == pytest.approx(0.7)
    assert header["food_items"] == "apple, rice"

    children = await db_with_user.get_diet_log_items(user_id, header_id)
    assert [c["display_name"] for c in children] == ["apple", "rice"]
    assert [c["item_order"] for c in children] == [0, 1]
    assert children[0]["source_type"] == "food"
    assert children[0]["source_id"] == 1


async def test_unknown_item_nutrient_makes_the_meal_total_unknown(
    db_with_user, user_id
):
    """An unknown item value must not masquerade as a numeric zero in the total."""
    items = [
        _item("apple", 95, 0.5, 25.0, 0.3, source_id=1),
        _item("mystery", None, 4.0, None, 0.4, source_id=2),
    ]
    header_id = await db_with_user.log_diet_with_items(user_id, "dinner", items)

    header = (
        await db_with_user.get_diet_logs(user_id, today_local(), today_local())
    )[-1]
    assert header["calories"] is None  # one item's calories unknown
    assert header["carbs_g"] is None  # one item's carbs unknown
    assert header["protein_g"] == pytest.approx(4.5)  # both known -> summed
    # Items keep their own snapshots, unknowns preserved.
    children = await db_with_user.get_diet_log_items(user_id, header_id)
    assert children[1]["calories"] is None


async def test_log_diet_with_items_is_idempotent_on_replay(db_with_user, user_id):
    source = MutationSource(update_id=555, chat_id=1, message_id=2)
    items = [_item("apple", 95, 0.5, 25.0, 0.3)]

    first = await db_with_user.log_diet_with_items(
        user_id, "snack", items, source=source
    )
    second = await db_with_user.log_diet_with_items(
        user_id, "snack", items, source=source
    )

    assert first == second  # replay returns the same meal id
    rows = await db_with_user.get_diet_logs(user_id, today_local(), today_local())
    assert len(rows) == 1  # not duplicated
    assert len(await db_with_user.get_diet_log_items(user_id, first)) == 1


async def test_undo_meal_cascades_to_items(db_with_user, user_id):
    items = [
        _item("apple", 95, 0.5, 25.0, 0.3, source_id=1),
        _item("rice", 200, 4.0, 44.0, 0.4, source_id=2),
    ]
    header_id = await db_with_user.log_diet_with_items(user_id, "lunch", items)
    assert len(await db_with_user.get_diet_log_items(user_id, header_id)) == 2

    deleted = await db_with_user.delete_log_by_id(user_id, "diet_logs", header_id)
    assert deleted is not None
    assert await db_with_user.get_diet_log_items(user_id, header_id) == []

    cursor = await db_with_user.conn.execute("PRAGMA foreign_key_check")
    assert await cursor.fetchall() == []


async def test_items_are_owner_scoped(db_with_user, user_id):
    header_id = await db_with_user.log_diet_with_items(
        user_id, "lunch", [_item("apple", 95, 0.5, 25.0, 0.3)]
    )
    # A different user cannot read this meal's items.
    assert await db_with_user.get_diet_log_items(user_id + 1, header_id) == []
