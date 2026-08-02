"""Per-meal-type shortcuts: "this belongs to my snacks", said explicitly.

The picker was already meal-type aware, but only by *learning* from completed
meals — so a food you know you eat at snack time had to be logged through Search
several times before it became tappable, and a shared-catalog item could never be
personalised at all. These tests pin the manual half.

The guarantees worth protecting:

* a shortcut is the top row of *its* meal and only its meal;
* it works for an item with no history whatsoever, including a catalog food;
* it is a pointer — starring something copies no nutrition and no amount;
* it is per user.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import keyboards, suggestions
from bot.handlers import shortcuts

UID = 123456789
OTHER = 987654321


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
class TestStorage:
    async def test_a_shortcut_is_scoped_to_one_meal(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        assert await db.add_meal_shortcut(user_id, "snack", "catalog", 158) is True

        assert await db.get_meal_shortcuts(user_id, "snack") == {("catalog", 158)}
        for other in ("breakfast", "lunch", "dinner"):
            assert await db.get_meal_shortcuts(user_id, other) == set()

    async def test_adding_twice_is_idempotent(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        assert await db.add_meal_shortcut(user_id, "snack", "catalog", 158) is True
        assert await db.add_meal_shortcut(user_id, "snack", "catalog", 158) is False
        assert len(await db.get_meal_shortcuts(user_id, "snack")) == 1

    async def test_removing_reports_whether_anything_went(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        await db.add_meal_shortcut(user_id, "snack", "catalog", 158)

        assert await db.remove_meal_shortcut(user_id, "snack", "catalog", 158) is True
        assert await db.remove_meal_shortcut(user_id, "snack", "catalog", 158) is False
        assert await db.get_meal_shortcuts(user_id, "snack") == set()

    async def test_shortcuts_are_per_user(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        await db.ensure_user(OTHER, None, None)
        await db.add_meal_shortcut(user_id, "snack", "catalog", 158)

        assert await db.get_meal_shortcuts(OTHER, "snack") == set()

    @pytest.mark.parametrize("meal", ["brunch", "", "SNACK"])
    async def test_an_unknown_meal_type_is_refused(self, db, user_id, meal):
        await db.ensure_user(user_id, None, None)
        with pytest.raises(ValueError):
            await db.add_meal_shortcut(user_id, meal, "catalog", 158)

    async def test_an_unknown_source_type_is_refused(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        with pytest.raises(ValueError):
            await db.add_meal_shortcut(user_id, "snack", "supplement", 1)

    async def test_a_catalog_item_may_be_a_shortcut(self, db, user_id):
        """Unlike user_food_preferences, which is food/recipe only."""
        from bot.catalog_seed import CATALOG_FOODS

        await db.seed_catalog(CATALOG_FOODS)
        await db.ensure_user(user_id, None, None)
        banana = (await db.search_catalog("banana"))[0]

        await db.add_meal_shortcut(user_id, "snack", "catalog", banana["id"])
        targets = await db.get_shortcut_targets(user_id, "snack")

        assert [t["name"] for t in targets] == ["Banana"]
        assert targets[0]["source_type"] == "catalog"

    async def test_a_dead_pointer_is_skipped_not_shown_broken(self, db, user_id):
        """A shortcut stores only a pointer; the food may be archived later."""
        await db.ensure_user(user_id, None, None)
        result = await db.save_food(user_id, "oats", "g", 100, 389, 16.9, 66, 6.9)
        food_id = result["food"]["id"]
        await db.add_meal_shortcut(user_id, "breakfast", "food", food_id)
        assert len(await db.get_shortcut_targets(user_id, "breakfast")) == 1

        await db.archive_food(user_id, food_id)

        assert await db.get_shortcut_targets(user_id, "breakfast") == []
        # The row survives, so restoring the food restores the shortcut.
        assert await db.get_meal_shortcuts(user_id, "breakfast") == {("food", food_id)}


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
class TestRanking:
    def _rank(self, candidates):
        return [c.name_key for c in suggestions.rank(candidates, datetime(2026, 8, 3))]

    def test_a_shortcut_outranks_heavy_usage(self):
        order = self._rank(
            [
                suggestions.Candidate("food", 1, "oats", meal_uses=99, total_uses=200),
                suggestions.Candidate("catalog", 158, "skyr", is_meal_shortcut=True),
            ]
        )
        assert order == ["skyr", "oats"]

    def test_a_shortcut_outranks_a_pin_within_its_meal(self):
        """A pin says "always show me this"; a shortcut is the narrower claim."""
        order = self._rank(
            [
                suggestions.Candidate("food", 2, "dal", is_pinned=True),
                suggestions.Candidate("catalog", 158, "skyr", is_meal_shortcut=True),
            ]
        )
        assert order == ["skyr", "dal"]

    def test_shortcuts_tie_break_by_name(self):
        order = self._rank(
            [
                suggestions.Candidate("food", 1, "zucchini", is_meal_shortcut=True),
                suggestions.Candidate("food", 2, "almonds", is_meal_shortcut=True),
            ]
        )
        assert order == ["almonds", "zucchini"]

    def test_the_label_marks_it_as_deliberate(self):
        """The user should see *why* a row is first: they put it there."""
        label = keyboards.choice_button_label(
            {"source_type": "catalog", "name": "Skyr", "is_meal_shortcut": True},
            quick=False,
        )
        assert label.startswith("⭐")
        plain = keyboards.choice_button_label(
            {"source_type": "catalog", "name": "Skyr"}, quick=False
        )
        assert plain.startswith("🔎")


# ---------------------------------------------------------------------------
# The picker
# ---------------------------------------------------------------------------
class TestPicker:
    async def test_a_never_logged_catalog_shortcut_appears_first(self, db, user_id):
        """The whole point: no history required, and no Search detour."""
        from bot.catalog_seed import CATALOG_FOODS
        from bot.handlers.diet import _ranked_choices

        await db.seed_catalog(CATALOG_FOODS)
        await db.ensure_user(user_id, None, None)
        await db.save_food(user_id, "oats", "g", 100, 389, 16.9, 66, 6.9)
        skyr = (await db.search_catalog("banana"))[0]
        await db.add_meal_shortcut(user_id, "snack", "catalog", skyr["id"])

        context = SimpleNamespace(bot_data={"db": db}, user_data={})
        choices = await _ranked_choices(context, user_id, "snack")

        assert choices[0]["name"] == "Banana"
        assert choices[0]["is_meal_shortcut"] is True

    async def test_it_does_not_leak_into_another_meal(self, db, user_id):
        from bot.catalog_seed import CATALOG_FOODS
        from bot.handlers.diet import _ranked_choices

        await db.seed_catalog(CATALOG_FOODS)
        await db.ensure_user(user_id, None, None)
        banana = (await db.search_catalog("banana"))[0]
        await db.add_meal_shortcut(user_id, "snack", "catalog", banana["id"])

        context = SimpleNamespace(bot_data={"db": db}, user_data={})
        lunch = await _ranked_choices(context, user_id, "lunch")

        assert [c["name"] for c in lunch] == []

    async def test_starring_copies_no_nutrition(self, db, user_id):
        """A shortcut is a pointer; editing the food still changes the numbers."""
        from bot.catalog_seed import CATALOG_FOODS

        await db.seed_catalog(CATALOG_FOODS)
        await db.ensure_user(user_id, None, None)
        banana = (await db.search_catalog("banana"))[0]
        await db.add_meal_shortcut(user_id, "snack", "catalog", banana["id"])

        cursor = await db.conn.execute("PRAGMA table_info(meal_shortcuts)")
        columns = {row["name"] for row in await cursor.fetchall()}
        assert not columns & {"calories", "protein_g", "carbs_g", "fat_g", "amount"}


# ---------------------------------------------------------------------------
# The /shortcuts flow
# ---------------------------------------------------------------------------
def _callback(data: str, user_id: int = UID):
    message = SimpleNamespace(reply_text=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=message,
        edit_message_reply_markup=AsyncMock(),
    )
    return SimpleNamespace(
        callback_query=query,
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id, username="t", first_name="T"),
        effective_chat=SimpleNamespace(id=user_id, type="private"),
    )


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _last_markup(message):
    for call in reversed(message.reply_text.call_args_list):
        if call.kwargs.get("reply_markup") is not None:
            return call.kwargs["reply_markup"]
    return None


class TestFlow:
    async def test_it_opens_on_a_meal_picker(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            message=message,
            effective_user=SimpleNamespace(id=user_id, username="t", first_name="T"),
            effective_chat=SimpleNamespace(id=user_id, type="private"),
        )
        context = SimpleNamespace(bot_data={"db": db}, user_data={}, args=[])

        state = await shortcuts.shortcuts_command(update, context)

        assert state == shortcuts.MEAL
        labels = _labels(_last_markup(message))
        assert any("Snack" in label for label in labels)
        assert any("Breakfast" in label for label in labels)

    async def test_picking_a_meal_lists_your_items(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        await db.save_food(user_id, "oats", "g", 100, 389, 16.9, 66, 6.9)
        context = SimpleNamespace(bot_data={"db": db}, user_data={})
        update = _callback(f"sc_m_{user_id}_snack")

        state = await shortcuts.meal_callback(update, context)

        assert state == shortcuts.LIST
        assert context.user_data["shortcut_meal"] == "snack"
        labels = _labels(_last_markup(update.callback_query.message))
        assert "☆ oats" in labels
        assert "🔍 Search the catalog" in labels

    async def test_tapping_a_row_toggles_it_both_ways(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        result = await db.save_food(user_id, "oats", "g", 100, 389, 16.9, 66, 6.9)
        food_id = result["food"]["id"]
        context = SimpleNamespace(
            bot_data={"db": db}, user_data={"shortcut_meal": "breakfast"}
        )

        await shortcuts.list_callback(_callback(f"sc_t_{user_id}_f{food_id}"), context)
        assert await db.get_meal_shortcuts(user_id, "breakfast") == {("food", food_id)}

        await shortcuts.list_callback(_callback(f"sc_t_{user_id}_f{food_id}"), context)
        assert await db.get_meal_shortcuts(user_id, "breakfast") == set()

    async def test_a_starred_row_renders_starred(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        result = await db.save_food(user_id, "oats", "g", 100, 389, 16.9, 66, 6.9)
        await db.add_meal_shortcut(user_id, "breakfast", "food", result["food"]["id"])
        context = SimpleNamespace(bot_data={"db": db}, user_data={})
        update = _callback(f"sc_m_{user_id}_breakfast")

        await shortcuts.meal_callback(update, context)

        assert "⭐ oats" in _labels(_last_markup(update.callback_query.message))

    async def test_the_limit_is_enforced_with_an_explanation(self, db, user_id):
        from bot.database import MAX_MEAL_SHORTCUTS

        await db.ensure_user(user_id, None, None)
        for index in range(MAX_MEAL_SHORTCUTS):
            await db.add_meal_shortcut(user_id, "snack", "catalog", 1000 + index)
        result = await db.save_food(user_id, "oats", "g", 100, 389, 16.9, 66, 6.9)
        context = SimpleNamespace(
            bot_data={"db": db}, user_data={"shortcut_meal": "snack"}
        )
        update = _callback(f"sc_t_{user_id}_f{result['food']['id']}")

        await shortcuts.list_callback(update, context)

        alert = update.callback_query.answer.await_args
        assert alert.kwargs.get("show_alert") is True
        assert "limit" in alert.args[0]
        assert len(await db.get_meal_shortcuts(user_id, "snack")) == MAX_MEAL_SHORTCUTS

    async def test_a_button_stamped_with_another_owner_is_refused(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        context = SimpleNamespace(bot_data={"db": db}, user_data={})
        update = _callback(f"sc_m_{OTHER}_snack")

        state = await shortcuts.meal_callback(update, context)

        assert state == shortcuts.MEAL
        assert update.callback_query.answer.await_args.kwargs["show_alert"] is True

    async def test_search_offers_catalog_matches_as_shortcuts(self, db, user_id):
        from bot.catalog_seed import CATALOG_FOODS

        await db.seed_catalog(CATALOG_FOODS)
        await db.ensure_user(user_id, None, None)
        message = SimpleNamespace(
            text="banana", reply_text=AsyncMock(return_value=SimpleNamespace(message_id=1))
        )
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_user=SimpleNamespace(id=user_id, username="t", first_name="T"),
            effective_chat=SimpleNamespace(id=user_id, type="private"),
        )
        context = SimpleNamespace(
            bot_data={"db": db}, user_data={"shortcut_meal": "snack"}
        )

        state = await shortcuts.receive_search(update, context)

        assert state == shortcuts.LIST
        assert any("Banana" in label for label in _labels(_last_markup(message)))

    async def test_a_search_with_no_matches_stays_in_search(self, db, user_id):
        from bot.catalog_seed import CATALOG_FOODS

        await db.seed_catalog(CATALOG_FOODS)
        await db.ensure_user(user_id, None, None)
        message = SimpleNamespace(text="zzzz", reply_text=AsyncMock())
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_user=SimpleNamespace(id=user_id, username="t", first_name="T"),
            effective_chat=SimpleNamespace(id=user_id, type="private"),
        )
        context = SimpleNamespace(
            bot_data={"db": db}, user_data={"shortcut_meal": "snack"}
        )

        state = await shortcuts.receive_search(update, context)

        assert state == shortcuts.SEARCH
        assert "/food add" in message.reply_text.await_args.args[0]
