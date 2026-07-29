"""Phase 1b unit B2: Quick-mode logging and private default quantities.

Covers the repository contracts (plan Â§7.4) and the handler flows (Â§9.3/Â§9.4):
a saved "usual" turns a source tap into a complete meal, an unusable default is
surfaced for repair instead of guessed at, and `Log + set as usual` commits the
meal and the preference together or not at all.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType
from telegram.ext import ConversationHandler

from bot.callback_data import to_base36
from bot.config import ALLOWED_USER_IDS
from bot.database import MutationSource
from bot.handlers import diet
from bot.meal_models import DefaultQuantity, DietEntryMode, QuickMealStatus
from bot.nutrition import NutritionError

UID = next(iter(ALLOWED_USER_IDS))
OTHER_UID = UID + 1

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def food(db_with_user):
    """A saved food with one named portion ('bowl' = 150 g)."""
    saved = await db_with_user.save_food(
        UID, "Dal", "g", 100, calories=120, protein_g=6, carbs_g=18, fat_g=2
    )
    food_id = saved["food"]["id"]
    await db_with_user.save_food_portion(UID, food_id, "bowl", 150, "g")
    return food_id


async def _bowl_portion_id(db, food_id: int) -> int:
    portions = await db.get_food_portions(UID, food_id)
    return next(p["id"] for p in portions if p["name"] == "bowl")


def _today_range():
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=1), today + timedelta(days=1)


# ---------------------------------------------------------------------------
# set_default_quantity / clear_default_quantity
# ---------------------------------------------------------------------------
async def test_default_is_stored_as_the_resolver_normalized_it(db_with_user, food):
    db = db_with_user
    stored = await db.set_default_quantity(
        UID, "food", food, DefaultQuantity(amount=2, unit="bowl")
    )
    assert stored == DefaultQuantity(amount=2.0, unit="bowl")
    assert await db.get_default_quantity(UID, "food", food) == stored


async def test_default_rejects_an_unresolvable_amount(db_with_user, food):
    db = db_with_user
    with pytest.raises(NutritionError):
        await db.set_default_quantity(
            UID, "food", food, DefaultQuantity(amount=1, unit="nonsense")
        )
    assert await db.get_default_quantity(UID, "food", food) is None


@pytest.mark.parametrize("amount", [0, -1, float("inf")])
async def test_default_rejects_a_nonsense_amount(db_with_user, food, amount):
    with pytest.raises(NutritionError):
        await db_with_user.set_default_quantity(
            UID, "food", food, DefaultQuantity(amount=amount, unit="g")
        )


async def test_default_on_a_missing_or_foreign_source_fails_the_same_way(db, food):
    await db.ensure_user(OTHER_UID, "b", "B")
    missing = pytest.raises(ValueError, match="no longer available")
    with missing:
        await db.set_default_quantity(
            UID, "food", 987654, DefaultQuantity(amount=1, unit="g")
        )
    with pytest.raises(ValueError, match="no longer available"):
        # `food` belongs to UID; another user must not be able to address it.
        await db.set_default_quantity(
            OTHER_UID, "food", food, DefaultQuantity(amount=1, unit="g")
        )


async def test_default_preserves_pin_and_hide(db_with_user, food):
    db = db_with_user
    await db.set_food_preference(UID, "food", food, is_pinned=True)
    await db.set_default_quantity(
        UID, "food", food, DefaultQuantity(amount=1, unit="bowl")
    )
    pref = await db.get_food_preference(UID, "food", food)
    assert pref["is_pinned"] == 1
    assert pref["default_unit"] == "bowl"


async def test_clearing_a_default_keeps_pin_but_drops_an_empty_row(
    db_with_user, food
):
    db = db_with_user
    await db.set_default_quantity(
        UID, "food", food, DefaultQuantity(amount=1, unit="bowl")
    )
    await db.set_food_preference(UID, "food", food, is_pinned=True)
    assert await db.clear_default_quantity(UID, "food", food) is True
    pref = await db.get_food_preference(UID, "food", food)
    assert pref["is_pinned"] == 1 and pref["default_amount"] is None

    await db.set_food_preference(UID, "food", food, is_pinned=False)
    await db.set_default_quantity(
        UID, "food", food, DefaultQuantity(amount=1, unit="bowl")
    )
    await db.clear_default_quantity(UID, "food", food)
    assert await db.get_food_preference(UID, "food", food) is None


async def test_clearing_works_after_the_food_is_archived(db_with_user, food):
    """An invalid default must stay removable once its source is gone."""
    db = db_with_user
    await db.set_default_quantity(
        UID, "food", food, DefaultQuantity(amount=1, unit="bowl")
    )
    await db.archive_food(UID, food)
    assert await db.clear_default_quantity(UID, "food", food) is True
    assert await db.get_default_quantity(UID, "food", food) is None


async def test_clearing_a_missing_preference_is_harmless(db_with_user, food):
    assert await db_with_user.clear_default_quantity(UID, "food", food) is False


async def test_a_partial_default_is_reported_not_used(db_with_user, food):
    db = db_with_user
    await db.set_default_quantity(
        UID, "food", food, DefaultQuantity(amount=1, unit="bowl")
    )
    async with db._write_operation():
        await db.conn.execute(
            "UPDATE user_food_preferences SET default_unit = NULL "
            "WHERE user_id = ? AND source_id = ?",
            (UID, food),
        )
    assert await db.get_default_quantity(UID, "food", food) is None
    assert await db.has_partial_default(UID, "food", food) is True


# ---------------------------------------------------------------------------
# create_quick_meal
# ---------------------------------------------------------------------------
async def test_quick_meal_uses_the_stored_default(db_with_user, food):
    db = db_with_user
    await db.set_default_quantity(
        UID, "food", food, DefaultQuantity(amount=2, unit="bowl")
    )
    result = await db.create_quick_meal(UID, "lunch", "food", food)

    assert result.status is QuickMealStatus.CREATED
    item = result.receipt.items[0]
    assert item.entered_amount == 2.0 and item.entered_unit == "bowl"
    assert item.resolved_base_amount == 300.0  # 2 x 150 g
    assert result.receipt.header.meal_type == "lunch"


async def test_quick_meal_without_a_default_asks_for_a_quantity(db_with_user, food):
    result = await db_with_user.create_quick_meal(UID, "lunch", "food", food)
    assert result.status is QuickMealStatus.QUANTITY_REQUIRED
    assert result.receipt is None


async def test_quick_meal_reports_an_invalid_supplied_amount(db_with_user, food):
    result = await db_with_user.create_quick_meal(
        UID,
        "lunch",
        "food",
        food,
        quantity=DefaultQuantity(amount=1, unit="nonsense"),
    )
    assert result.status is QuickMealStatus.QUANTITY_INVALID
    assert await db_with_user.get_diet_logs(UID, *_today_range()) == []


async def test_quick_meal_distinguishes_a_stale_default_from_a_bad_amount(
    db_with_user, food
):
    """A default that stops resolving must ask for repair, not silently fail."""
    db = db_with_user
    await db.set_default_quantity(
        UID, "food", food, DefaultQuantity(amount=1, unit="bowl")
    )
    await db.remove_food_portion(UID, food, await _bowl_portion_id(db, food))

    result = await db.create_quick_meal(UID, "lunch", "food", food)

    assert result.status is QuickMealStatus.DEFAULT_INVALID
    assert await db.get_diet_logs(UID, *_today_range()) == []


async def test_quick_meal_on_an_archived_source_is_unavailable(db_with_user, food):
    db = db_with_user
    await db.set_default_quantity(
        UID, "food", food, DefaultQuantity(amount=1, unit="bowl")
    )
    await db.archive_food(UID, food)
    result = await db.create_quick_meal(UID, "lunch", "food", food)
    assert result.status is QuickMealStatus.SOURCE_UNAVAILABLE


async def test_quick_meal_cannot_read_another_users_food(db, food):
    await db.ensure_user(OTHER_UID, "b", "B")
    result = await db.create_quick_meal(
        OTHER_UID,
        "lunch",
        "food",
        food,
        quantity=DefaultQuantity(amount=1, unit="bowl"),
    )
    assert result.status is QuickMealStatus.SOURCE_UNAVAILABLE


async def test_log_and_set_default_is_all_or_nothing(db_with_user, food):
    db = db_with_user
    result = await db.create_quick_meal(
        UID,
        "dinner",
        "food",
        food,
        quantity=DefaultQuantity(amount=1, unit="bowl"),
        set_as_default=True,
    )
    assert result.status is QuickMealStatus.CREATED
    assert await db.get_default_quantity(UID, "food", food) == DefaultQuantity(
        amount=1.0, unit="bowl"
    )


async def test_a_failed_quick_meal_stores_no_default(db_with_user, food):
    db = db_with_user
    await db.archive_food(UID, food)
    result = await db.create_quick_meal(
        UID,
        "dinner",
        "food",
        food,
        quantity=DefaultQuantity(amount=1, unit="bowl"),
        set_as_default=True,
    )
    assert result.status is QuickMealStatus.SOURCE_UNAVAILABLE
    assert await db.get_default_quantity(UID, "food", food) is None


async def test_catalog_quick_meal_needs_an_explicit_amount(db_with_user):
    db = db_with_user
    from bot.catalog_seed import CATALOG_FOODS

    await db.seed_catalog(CATALOG_FOODS)
    hit = (await db.search_catalog(CATALOG_FOODS[0]["display_name"]))[0]

    assert (
        await db.create_quick_meal(UID, "snack", "catalog", hit["id"])
    ).status is QuickMealStatus.QUANTITY_REQUIRED
    with pytest.raises(ValueError, match="private source"):
        await db.create_quick_meal(
            UID,
            "snack",
            "catalog",
            hit["id"],
            quantity=DefaultQuantity(amount=100, unit="g"),
            set_as_default=True,
        )


async def test_quick_meal_replay_does_not_duplicate(db_with_user, food):
    db = db_with_user
    source = MutationSource(update_id=4242)
    quantity = DefaultQuantity(amount=1, unit="bowl")
    first = await db.create_quick_meal(
        UID, "lunch", "food", food, quantity=quantity, source=source
    )
    second = await db.create_quick_meal(
        UID, "lunch", "food", food, quantity=quantity, source=source
    )
    assert first.status is QuickMealStatus.CREATED
    assert second.status is QuickMealStatus.REPLAYED
    assert second.receipt.header.meal_id == first.receipt.header.meal_id
    assert len(await db.get_diet_logs(UID, *_today_range())) == 1


async def test_quick_meal_replay_after_undo_is_not_recreated(db_with_user, food):
    db = db_with_user
    source = MutationSource(update_id=4343)
    quantity = DefaultQuantity(amount=1, unit="bowl")
    created = await db.create_quick_meal(
        UID, "lunch", "food", food, quantity=quantity, source=source
    )
    await db.delete_meal_if_recent(UID, created.receipt.header.meal_id)

    replayed = await db.create_quick_meal(
        UID, "lunch", "food", food, quantity=quantity, source=source
    )

    assert replayed.status is QuickMealStatus.REPLAYED_REMOVED
    assert await db.get_diet_logs(UID, *_today_range()) == []


async def test_quick_and_guided_receipts_do_not_collide(db_with_user, food):
    """Same update id, different operations â€” each keeps its own outcome."""
    db = db_with_user
    source = MutationSource(update_id=606)
    await db.log_diet_with_items(
        UID, "lunch", [{"display_name": "Toast", "source_type": "freetext"}],
        source=source,
    )
    quick = await db.create_quick_meal(
        UID,
        "lunch",
        "food",
        food,
        quantity=DefaultQuantity(amount=1, unit="bowl"),
        source=source,
    )
    assert quick.status is QuickMealStatus.CREATED
    assert len(await db.get_diet_logs(UID, *_today_range())) == 2


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def _enable(monkeypatch):
    monkeypatch.setattr("bot.config.PHASE1_ENABLED_USER_IDS", frozenset({UID}))
    monkeypatch.setattr("bot.config.HOME_KEYBOARD_MODE", "off")


def _callback(data: str, message_id: int = 10):
    sent = SimpleNamespace(message_id=message_id + 1)
    message = SimpleNamespace(
        message_id=message_id,
        reply_text=AsyncMock(return_value=sent),
    )
    query = SimpleNamespace(
        data=data,
        message=message,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    return SimpleNamespace(
        update_id=900,
        callback_query=query,
        effective_message=message,
        message=None,
        effective_user=SimpleNamespace(id=UID, first_name="Manoj", username="m"),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
    )


def _text_update(text: str, message_id: int = 10):
    sent = SimpleNamespace(message_id=message_id + 1)
    message = SimpleNamespace(
        message_id=message_id, text=text, reply_text=AsyncMock(return_value=sent)
    )
    return SimpleNamespace(
        update_id=901,
        callback_query=None,
        effective_message=message,
        message=message,
        effective_user=SimpleNamespace(id=UID, first_name="Manoj", username="m"),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
    )


def _quick_context(db, **extra):
    user_data = {
        "diet_entry_mode": DietEntryMode.QUICK,
        "diet_meal_type": "lunch",
        "diet_ui_message_id": 10,
        "diet_ui_revision": 0,
        "_ledger_active_conversation": ("diet", UID),
    }
    user_data.update(extra)
    return SimpleNamespace(bot_data={"db": db}, user_data=user_data, args=[])


def _last_text(update):
    return update.effective_message.reply_text.call_args.args[0]


async def test_tapping_a_food_with_a_usual_logs_it_in_one_tap(
    db_with_user, food, monkeypatch
):
    _enable(monkeypatch)
    db = db_with_user
    await db.set_default_quantity(
        UID, "food", food, DefaultQuantity(amount=2, unit="bowl")
    )
    update = _callback(f"dfood_{UID}_{food}")
    context = _quick_context(db)

    state = await diet.choose_food(update, context)

    assert state == ConversationHandler.END
    assert len(await db.get_diet_logs(UID, *_today_range())) == 1
    text = _last_text(update)
    assert "Logged" in text and "Dal" in text


async def test_tapping_a_food_without_a_usual_asks_for_an_amount(
    db_with_user, food, monkeypatch
):
    _enable(monkeypatch)
    update = _callback(f"dfood_{UID}_{food}")
    state = await diet.choose_food(update, _quick_context(db_with_user))
    assert state == diet.PORTION_CHOICE
    assert await db_with_user.get_diet_logs(UID, *_today_range()) == []


async def test_a_broken_usual_opens_the_repair_menu_instead_of_logging(
    db_with_user, food, monkeypatch
):
    _enable(monkeypatch)
    db = db_with_user
    await db.set_default_quantity(
        UID, "food", food, DefaultQuantity(amount=1, unit="bowl")
    )
    await db.remove_food_portion(UID, food, await _bowl_portion_id(db, food))
    update = _callback(f"dfood_{UID}_{food}")

    state = await diet.choose_food(update, _quick_context(db))

    assert state == diet.DEFAULT_MENU
    assert "repair" in _last_text(update).lower()
    assert await db.get_diet_logs(UID, *_today_range()) == []


async def test_builder_mode_ignores_the_usual(db_with_user, food, monkeypatch):
    """A /diet-started meal keeps the multi-item flow even with a default set."""
    _enable(monkeypatch)
    db = db_with_user
    await db.set_default_quantity(
        UID, "food", food, DefaultQuantity(amount=1, unit="bowl")
    )
    context = _quick_context(db, diet_entry_mode=DietEntryMode.BUILDER)

    state = await diet.choose_food(_callback(f"dfood_{UID}_{food}"), context)

    assert state == diet.PORTION_CHOICE
    assert await db.get_diet_logs(UID, *_today_range()) == []


async def test_quick_mode_is_off_when_the_flag_is_off(db_with_user, food):
    db = db_with_user
    await db.set_default_quantity(
        UID, "food", food, DefaultQuantity(amount=1, unit="bowl")
    )
    state = await diet.choose_food(
        _callback(f"dfood_{UID}_{food}"), _quick_context(db)
    )
    assert state == diet.PORTION_CHOICE
    assert await db.get_diet_logs(UID, *_today_range()) == []


async def test_quick_confirm_offers_log_and_set_default(
    db_with_user, food, monkeypatch
):
    _enable(monkeypatch)
    db = db_with_user
    context = _quick_context(db, diet_sel_kind="food", diet_sel_id=food)
    entry = await db.resolve_quantity(UID, "food", food, ["1", "bowl"])
    update = _text_update("1 bowl")

    state = await diet._show_item_preview(
        update, context, update.effective_message, entry
    )

    assert state == diet.QUICK_CONFIRM
    markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    owner, rev = to_base36(UID), to_base36(1)
    assert f"dq_log_{owner}_{rev}" in data
    assert f"dq_default_{owner}_{rev}" in data


async def test_quick_confirm_log_writes_the_meal(db_with_user, food, monkeypatch):
    _enable(monkeypatch)
    db = db_with_user
    context = _quick_context(
        db,
        diet_ui_revision=3,
        diet_quick_pending_item={
            "source_type": "food",
            "source_id": food,
            "entered_amount": 1.0,
            "entered_unit": "bowl",
            "display_name": "1 bowl Dal",
        },
    )
    update = _callback(f"dq_log_{to_base36(UID)}_{to_base36(3)}")

    state = await diet.quick_log(update, context)

    assert state == ConversationHandler.END
    assert len(await db.get_diet_logs(UID, *_today_range())) == 1
    assert await db.get_default_quantity(UID, "food", food) is None


async def test_quick_confirm_log_and_default_saves_both(
    db_with_user, food, monkeypatch
):
    _enable(monkeypatch)
    db = db_with_user
    context = _quick_context(
        db,
        diet_ui_revision=3,
        diet_quick_pending_item={
            "source_type": "food",
            "source_id": food,
            "entered_amount": 2.0,
            "entered_unit": "bowl",
            "display_name": "2 bowl Dal",
        },
    )
    update = _callback(f"dq_default_{to_base36(UID)}_{to_base36(3)}")

    await diet.quick_log_and_default(update, context)

    assert len(await db.get_diet_logs(UID, *_today_range())) == 1
    assert await db.get_default_quantity(UID, "food", food) == DefaultQuantity(
        amount=2.0, unit="bowl"
    )
    assert "usual" in _last_text(update)


async def test_a_stale_quick_confirm_tap_logs_nothing(
    db_with_user, food, monkeypatch
):
    _enable(monkeypatch)
    db = db_with_user
    context = _quick_context(
        db,
        diet_ui_revision=5,
        diet_quick_pending_item={
            "source_type": "food",
            "source_id": food,
            "entered_amount": 1.0,
            "entered_unit": "bowl",
            "display_name": "1 bowl Dal",
        },
    )
    # Revision 4 belongs to a superseded screen.
    update = _callback(f"dq_log_{to_base36(UID)}_{to_base36(4)}")

    state = await diet.quick_log(update, context)

    assert state == diet.QUICK_CONFIRM
    assert await db.get_diet_logs(UID, *_today_range()) == []


async def test_quick_cancel_logs_nothing(db_with_user, monkeypatch):
    _enable(monkeypatch)
    context = _quick_context(db_with_user, diet_ui_revision=1)
    update = _callback(f"dq_cancel_{to_base36(UID)}_{to_base36(1)}")

    state = await diet.quick_cancel(update, context)

    assert state == ConversationHandler.END
    assert await db_with_user.get_diet_logs(UID, *_today_range()) == []


# --- default management screens --------------------------------------------
async def test_manage_button_opens_the_default_menu(db_with_user, food, monkeypatch):
    _enable(monkeypatch)
    data = f"dmanage_{to_base36(UID)}_{to_base36(0)}_f_{to_base36(food)}"
    update = _callback(data)
    context = _quick_context(db_with_user)

    state = await diet.manage_default(update, context)

    assert state == diet.DEFAULT_MENU
    assert context.user_data["diet_default_source_id"] == food
    assert "No usual amount saved yet" in _last_text(update)


async def test_manage_rejects_another_users_token(db_with_user, food, monkeypatch):
    _enable(monkeypatch)
    data = f"dmanage_{to_base36(OTHER_UID)}_{to_base36(0)}_f_{to_base36(food)}"
    update = _callback(data)
    context = _quick_context(db_with_user)

    state = await diet.manage_default(update, context)

    assert state == diet.FOOD_CHOICE
    assert "diet_default_source_id" not in context.user_data


async def test_setting_a_default_through_the_screens(db_with_user, food, monkeypatch):
    _enable(monkeypatch)
    db = db_with_user
    context = _quick_context(
        db,
        diet_default_source_type="food",
        diet_default_source_id=food,
        diet_ui_revision=1,
    )

    edit = _callback(f"dd_edit_{to_base36(UID)}_{to_base36(1)}")
    assert await diet.default_edit(edit, context) == diet.DEFAULT_AMOUNT

    typed = _text_update("2 bowl")
    assert await diet.receive_default_amount(typed, context) == diet.DEFAULT_CONFIRM
    assert "2 bowl" in _last_text(typed)

    revision = context.user_data["diet_ui_revision"]
    context.user_data["diet_ui_message_id"] = 11  # the confirm message we sent
    save = _callback(f"dd_save_{to_base36(UID)}_{to_base36(revision)}", message_id=11)
    assert await diet.default_save(save, context) == diet.DEFAULT_MENU

    assert await db.get_default_quantity(UID, "food", food) == DefaultQuantity(
        amount=2.0, unit="bowl"
    )


async def test_an_invalid_typed_default_is_rejected_without_writing(
    db_with_user, food, monkeypatch
):
    _enable(monkeypatch)
    context = _quick_context(
        db_with_user, diet_default_source_type="food", diet_default_source_id=food
    )
    typed = _text_update("banana")

    state = await diet.receive_default_amount(typed, context)

    assert state == diet.DEFAULT_AMOUNT
    assert "diet_pending_default" not in context.user_data
    assert await db_with_user.get_default_quantity(UID, "food", food) is None


async def test_removing_a_default_from_the_menu(db_with_user, food, monkeypatch):
    _enable(monkeypatch)
    db = db_with_user
    await db.set_default_quantity(
        UID, "food", food, DefaultQuantity(amount=1, unit="bowl")
    )
    context = _quick_context(
        db,
        diet_default_source_type="food",
        diet_default_source_id=food,
        diet_ui_revision=2,
    )
    update = _callback(f"dd_clear_{to_base36(UID)}_{to_base36(2)}")

    state = await diet.default_clear(update, context)

    assert state == diet.DEFAULT_MENU
    assert await db.get_default_quantity(UID, "food", food) is None


async def test_default_menu_flags_a_default_that_stopped_resolving(
    db_with_user, food, monkeypatch
):
    _enable(monkeypatch)
    db = db_with_user
    await db.set_default_quantity(
        UID, "food", food, DefaultQuantity(amount=1, unit="bowl")
    )
    await db.remove_food_portion(UID, food, await _bowl_portion_id(db, food))
    update = _callback("x")
    context = _quick_context(db)

    state = await diet._open_default_menu(
        update, context, update.effective_message, "food", food
    )

    assert state == diet.DEFAULT_MENU
    assert "no longer works" in _last_text(update)


async def test_default_back_leaves_everything_alone(db_with_user, food, monkeypatch):
    _enable(monkeypatch)
    context = _quick_context(
        db_with_user,
        diet_default_source_type="food",
        diet_default_source_id=food,
        diet_ui_revision=1,
    )
    update = _callback(f"dd_back_{to_base36(UID)}_{to_base36(1)}")

    state = await diet.default_back(update, context)

    assert state == diet.FOOD_CHOICE
    assert "diet_default_source_id" not in context.user_data
    assert await db_with_user.get_default_quantity(UID, "food", food) is None

