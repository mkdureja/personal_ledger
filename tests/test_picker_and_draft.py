"""Phase 1b unit B4: picker pagination, meal-type change, draft editing, ranking.

These are the "long list and wrong item" cases: a picker that stays thumb-sized
once you have many saved foods, a way to fix the inferred meal type without
losing the draft, and per-item edit/replace/remove controls whose old keyboards
stop working the moment the draft changes.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType

from bot.callback_data import to_base36
from bot.config import ALLOWED_USER_IDS
from bot.handlers import diet
from bot.keyboards import (
    SUGGESTION_PAGE_SIZE,
    diet_save_keyboard,
    food_choice_keyboard,
    paginate_choices,
)
from bot.meal_models import DietEntryMode

UID = next(iter(ALLOWED_USER_IDS))
# No module-level asyncio mark: this file mixes sync keyboard tests with async
# handler tests, and pytest.ini already runs in asyncio auto mode.


def _enable(monkeypatch):
    monkeypatch.setattr("bot.config.PHASE1_ENABLED_USER_IDS", frozenset({UID}))
    monkeypatch.setattr("bot.config.HOME_KEYBOARD_MODE", "off")


def _choices(count: int) -> list[dict]:
    return [
        {"source_type": "food", "id": n, "name": f"Food {n}"}
        for n in range(1, count + 1)
    ]


def _data(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _callback(data: str, message_id: int = 30):
    sent = SimpleNamespace(message_id=message_id + 1)
    message = SimpleNamespace(
        message_id=message_id, reply_text=AsyncMock(return_value=sent)
    )
    query = SimpleNamespace(
        data=data,
        message=message,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    return SimpleNamespace(
        update_id=700,
        callback_query=query,
        effective_message=message,
        message=None,
        effective_user=SimpleNamespace(id=UID, first_name="Manoj", username="m"),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
    )


def _context(db, **extra):
    user_data = {
        "diet_meal_type": "lunch",
        "diet_ui_message_id": 30,
        "diet_ui_revision": 0,
        "diet_entry_mode": DietEntryMode.QUICK,
        "_ledger_active_conversation": ("diet", UID),
    }
    user_data.update(extra)
    return SimpleNamespace(bot_data={"db": db}, user_data=user_data, args=[])


def _picker_db(foods=(), recipes=(), catalog=(), suggestions_on=True):
    return SimpleNamespace(
        list_foods=AsyncMock(return_value=list(foods)),
        list_recipes=AsyncMock(return_value=list(recipes)),
        get_user_catalog_history=AsyncMock(return_value=list(catalog)),
        get_meal_shortcuts=AsyncMock(return_value=set()),
        get_shortcut_targets=AsyncMock(return_value=[]),
        get_suggestions_enabled=AsyncMock(return_value=suggestions_on),
        get_diet_item_stats=AsyncMock(return_value={}),
        get_food_preferences=AsyncMock(return_value={}),
        get_recent_item_quantities=AsyncMock(return_value=[]),
    )


def _last_markup(update):
    return update.effective_message.reply_text.call_args.kwargs["reply_markup"]


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
def test_a_short_list_is_not_paginated():
    page, index, count = paginate_choices(_choices(SUGGESTION_PAGE_SIZE))
    assert len(page) == SUGGESTION_PAGE_SIZE
    assert (index, count) == (0, 1)


def test_a_long_list_splits_into_pages():
    page, index, count = paginate_choices(_choices(20), page=1)
    assert [c["id"] for c in page] == [9, 10, 11, 12, 13, 14, 15, 16]
    assert (index, count) == (1, 3)


@pytest.mark.parametrize("requested,expected", [(-5, 0), (99, 2)])
def test_an_out_of_range_page_is_clamped(requested, expected):
    _page, index, _count = paginate_choices(_choices(20), page=requested)
    assert index == expected


def test_navigation_appears_only_when_needed():
    short = food_choice_keyboard(UID, _choices(3), paginate=True)
    assert not any(d.startswith("dpage_") for d in _data(short))

    long = food_choice_keyboard(UID, _choices(20), paginate=True, page=1)
    owner = to_base36(UID)
    data = _data(long)
    assert f"dpage_{owner}_{to_base36(0)}" in data  # back
    assert f"dpage_{owner}_{to_base36(2)}" in data  # forward


def test_the_last_page_has_no_forward_button():
    data = _data(food_choice_keyboard(UID, _choices(20), paginate=True, page=2))
    assert f"dpage_{to_base36(UID)}_{to_base36(3)}" not in data


async def test_paging_re_reads_and_renders_the_next_page(monkeypatch):
    _enable(monkeypatch)
    # Personalization off: the plain alphabetical list is the one that can grow
    # past a page, because it is complete by definition. The ranked list is
    # capped at MAX_RANKED_CHOICES (see the cap test below) and never paginates.
    db = _picker_db(
        foods=[{"id": n, "name": f"Food {n}"} for n in range(1, 21)],
        suggestions_on=False,
    )
    update = _callback(f"dpage_{to_base36(UID)}_{to_base36(1)}")
    context = _context(db)

    state = await diet.change_page(update, context)

    assert state == diet.FOOD_CHOICE
    assert context.user_data["diet_choice_page"] == 1
    # Freshly ranked, not served from a cached list.
    db.list_foods.assert_awaited()
    page_two = {
        d for d in _data(_last_markup(update)) if d.startswith(f"dfood_{UID}_")
    }
    assert len(page_two) == SUGGESTION_PAGE_SIZE

    first = _callback(f"dpage_{to_base36(UID)}_{to_base36(0)}")
    context.user_data["diet_ui_message_id"] = 30
    await diet.change_page(first, context)
    page_one = {
        d for d in _data(_last_markup(first)) if d.startswith(f"dfood_{UID}_")
    }
    assert page_one.isdisjoint(page_two)


async def test_the_ranked_picker_stops_at_the_shortlist_cap(monkeypatch):
    """A growing food list must not grow the picker — that is what search is for.

    Keeping every typed entry automatically means the food list only ever gets
    longer, so the quick-fill rows are a shortlist of the top few, not an index.
    """
    _enable(monkeypatch)
    db = _picker_db(foods=[{"id": n, "name": f"Food {n}"} for n in range(1, 31)])
    update = _callback(f"dpage_{to_base36(UID)}_{to_base36(0)}")

    await diet.change_page(update, _context(db))

    rows = [d for d in _data(_last_markup(update)) if d.startswith(f"dfood_{UID}_")]
    assert len(rows) == diet.MAX_RANKED_CHOICES
    # One screenful, so there is nothing left to page through.
    assert not any(d.startswith("dpage_") for d in _data(_last_markup(update)))
    # ...and the ways to reach the other 22 are still on the keyboard.
    assert f"dsearch_{UID}" in _data(_last_markup(update))


async def test_a_page_tap_on_a_stale_message_is_ignored(monkeypatch):
    _enable(monkeypatch)
    db = _picker_db(foods=[{"id": 1, "name": "Dal"}])
    update = _callback(f"dpage_{to_base36(UID)}_{to_base36(1)}", message_id=999)
    context = _context(db)

    state = await diet.change_page(update, context)

    assert state == diet.FOOD_CHOICE
    assert "diet_choice_page" not in context.user_data


# ---------------------------------------------------------------------------
# Change meal type
# ---------------------------------------------------------------------------
async def test_change_meal_type_keeps_the_draft(monkeypatch):
    _enable(monkeypatch)
    draft = [{"display_name": "1 bowl Dal", "calories": 200}]
    context = _context(_picker_db(), diet_items=draft)
    update = _callback(f"dchangemeal_{to_base36(UID)}")

    state = await diet.change_meal_type(update, context)

    assert state == diet.MEAL_TYPE
    assert context.user_data["diet_items"] == draft
    assert context.user_data["diet_entry_mode"] == DietEntryMode.QUICK
    assert context.user_data["diet_meal_message_id"] == 31


async def test_change_meal_type_rejects_another_user(monkeypatch):
    _enable(monkeypatch)
    context = _context(_picker_db())
    update = _callback(f"dchangemeal_{to_base36(UID + 1)}")

    state = await diet.change_meal_type(update, context)

    assert state == diet.FOOD_CHOICE
    assert "another user" in update.callback_query.answer.call_args.args[0]


# ---------------------------------------------------------------------------
# Draft controls
# ---------------------------------------------------------------------------
def test_the_builder_keyboard_keeps_legacy_payloads_when_the_flag_is_off():
    data = _data(diet_save_keyboard(UID))
    assert f"dsave_{UID}" in data and f"dadd_{UID}" in data
    assert not any(d.startswith("dqty_") for d in data)


def test_the_phase1_builder_keyboard_is_revisioned_and_per_item():
    items = [
        {"display_name": "Dal", "source_type": "food", "source_id": 1},
        {"display_name": "Note", "source_type": "freetext"},
    ]
    data = _data(
        diet_save_keyboard(UID, phase1_enabled=True, revision=4, items=items)
    )
    owner, rev = to_base36(UID), to_base36(4)
    assert f"dsave_{owner}_{rev}" in data
    assert f"dqty_{owner}_{rev}_{to_base36(0)}" in data
    assert f"dremove_{owner}_{rev}_{to_base36(1)}" in data
    # Free text has no amount to change.
    assert f"dqty_{owner}_{rev}_{to_base36(1)}" not in data


async def test_removing_an_item_redraws_the_rest(monkeypatch):
    _enable(monkeypatch)
    items = [
        {"display_name": "Dal", "source_type": "food", "source_id": 1},
        {"display_name": "Rice", "source_type": "food", "source_id": 2},
    ]
    context = _context(_picker_db(), diet_items=items, diet_ui_revision=2)
    update = _callback(
        f"dremove_{to_base36(UID)}_{to_base36(2)}_{to_base36(0)}"
    )

    state = await diet.draft_remove(update, context)

    assert state == diet.CONFIRM_ITEM
    assert [i["display_name"] for i in context.user_data["diet_items"]] == ["Rice"]
    assert context.user_data["diet_ui_revision"] == 3


async def test_removing_the_last_item_reopens_the_picker(monkeypatch):
    _enable(monkeypatch)
    context = _context(
        _picker_db(),
        diet_items=[{"display_name": "Dal", "source_type": "food", "source_id": 1}],
        diet_ui_revision=1,
    )
    update = _callback(
        f"dremove_{to_base36(UID)}_{to_base36(1)}_{to_base36(0)}"
    )

    state = await diet.draft_remove(update, context)

    assert state == diet.FOOD_CHOICE
    assert context.user_data["diet_items"] == []


async def test_a_stale_draft_tap_cannot_remove_the_wrong_item(monkeypatch):
    _enable(monkeypatch)
    items = [
        {"display_name": "Dal", "source_type": "food", "source_id": 1},
        {"display_name": "Rice", "source_type": "food", "source_id": 2},
    ]
    context = _context(_picker_db(), diet_items=items, diet_ui_revision=5)
    # Revision 4 was drawn before the draft last changed.
    update = _callback(
        f"dremove_{to_base36(UID)}_{to_base36(4)}_{to_base36(0)}"
    )

    state = await diet.draft_remove(update, context)

    assert state == diet.CONFIRM_ITEM
    assert len(context.user_data["diet_items"]) == 2


async def test_an_out_of_range_index_redraws_instead_of_crashing(monkeypatch):
    _enable(monkeypatch)
    items = [{"display_name": "Dal", "source_type": "food", "source_id": 1}]
    context = _context(_picker_db(), diet_items=items, diet_ui_revision=1)
    update = _callback(
        f"dremove_{to_base36(UID)}_{to_base36(1)}_{to_base36(9)}"
    )

    state = await diet.draft_remove(update, context)

    assert state == diet.CONFIRM_ITEM
    assert len(context.user_data["diet_items"]) == 1


async def test_replace_marks_the_slot_and_reopens_the_picker(monkeypatch):
    _enable(monkeypatch)
    items = [
        {"display_name": "Dal", "source_type": "food", "source_id": 1},
        {"display_name": "Rice", "source_type": "food", "source_id": 2},
    ]
    context = _context(_picker_db(), diet_items=items, diet_ui_revision=1)
    update = _callback(f"dedit_{to_base36(UID)}_{to_base36(1)}_{to_base36(1)}")

    state = await diet.draft_replace(update, context)

    assert state == diet.FOOD_CHOICE
    assert context.user_data["diet_edit_index"] == 1
    assert len(context.user_data["diet_items"]) == 2  # nothing lost yet


async def test_a_resolved_item_replaces_the_marked_slot(monkeypatch):
    _enable(monkeypatch)
    items = [
        {"display_name": "Dal", "source_type": "food", "source_id": 1},
        {"display_name": "Rice", "source_type": "food", "source_id": 2},
    ]
    context = _context(
        _picker_db(), diet_items=items, diet_ui_revision=1, diet_edit_index=0
    )
    entry = SimpleNamespace(
        as_item=lambda: {
            "display_name": "2 roti",
            "source_type": "food",
            "source_id": 3,
            "calories": 200,
        }
    )
    update = _callback("x")

    state = await diet._show_item_preview(
        update, context, update.effective_message, entry
    )

    assert state == diet.CONFIRM_ITEM
    assert [i["display_name"] for i in context.user_data["diet_items"]] == [
        "2 roti",
        "Rice",
    ]
    assert "diet_edit_index" not in context.user_data


async def test_change_quantity_keeps_the_item_until_a_new_one_resolves(monkeypatch):
    _enable(monkeypatch)
    db = _picker_db()
    db.get_food_by_id = AsyncMock(return_value={"id": 1, "name": "Dal"})
    db.get_food_portions = AsyncMock(return_value=[])
    db.get_food_preference = AsyncMock(return_value={})
    items = [{"display_name": "Dal", "source_type": "food", "source_id": 1}]
    context = _context(db, diet_items=items, diet_ui_revision=1)
    update = _callback(f"dqty_{to_base36(UID)}_{to_base36(1)}_{to_base36(0)}")

    state = await diet.draft_change_quantity(update, context)

    assert state == diet.PORTION_CHOICE
    assert context.user_data["diet_edit_index"] == 0
    assert context.user_data["diet_items"] == items


async def test_change_quantity_is_refused_for_a_typed_item(monkeypatch):
    _enable(monkeypatch)
    items = [{"display_name": "Toast", "source_type": "freetext"}]
    context = _context(_picker_db(), diet_items=items, diet_ui_revision=1)
    update = _callback(f"dqty_{to_base36(UID)}_{to_base36(1)}_{to_base36(0)}")

    state = await diet.draft_change_quantity(update, context)

    assert state == diet.CONFIRM_ITEM
    assert "diet_edit_index" not in context.user_data


# ---------------------------------------------------------------------------
# Ranking with catalog history
# ---------------------------------------------------------------------------
async def test_catalog_history_joins_the_suggestions(db_with_user):
    """A catalog food becomes a suggestion only after it has been logged."""
    db = db_with_user
    from bot.catalog_seed import CATALOG_FOODS

    await db.seed_catalog(CATALOG_FOODS)
    hit = (await db.search_catalog(CATALOG_FOODS[0]["display_name"]))[0]
    context = SimpleNamespace(bot_data={"db": db}, user_data={})

    assert await diet._ranked_choices(context, UID, "lunch") == []

    entry = await db.resolve_quantity(UID, "catalog", hit["id"], ["100", "g"])
    await db.log_diet_with_items(UID, "lunch", [entry.as_item()])

    choices = await diet._ranked_choices(context, UID, "lunch")
    assert [(c["source_type"], c["id"]) for c in choices] == [
        ("catalog", hit["id"])
    ]


async def test_catalog_history_is_hidden_when_personalization_is_off(db_with_user):
    db = db_with_user
    from bot.catalog_seed import CATALOG_FOODS

    await db.seed_catalog(CATALOG_FOODS)
    hit = (await db.search_catalog(CATALOG_FOODS[0]["display_name"]))[0]
    entry = await db.resolve_quantity(UID, "catalog", hit["id"], ["100", "g"])
    await db.log_diet_with_items(UID, "lunch", [entry.as_item()])
    await db.set_suggestions_enabled(UID, False)
    context = SimpleNamespace(bot_data={"db": db}, user_data={})

    assert await diet._ranked_choices(context, UID, "lunch") == []


async def test_hidden_private_sources_stay_hidden(db_with_user):
    db = db_with_user
    saved = await db.save_food(UID, "Dal", "g", 100, calories=100, protein_g=1, carbs_g=2, fat_g=3)
    food_id = saved["food"]["id"]
    await db.set_food_preference(UID, "food", food_id, hidden=True)
    context = SimpleNamespace(bot_data={"db": db}, user_data={})

    assert await diet._ranked_choices(context, UID, "lunch") == []
