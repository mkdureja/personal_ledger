"""Phase 1a Unit A1 behaviors: inference, control normalization, pin/hide
desired-state writes, and reset default-preservation."""

from __future__ import annotations

import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType

from bot.callback_data import to_base36
from bot.config import ALLOWED_USER_IDS
from bot.handlers import diet
from bot.handlers.common import (
    GREETINGS,
    HOME_ACTIONS,
    HOME_WORDS,
    normalize_control_text,
)
from bot.keyboards import food_portion_keyboard
from bot.services.meal_logging import infer_meal_type


# ---------------------------------------------------------------------------
# infer_meal_type — half-open boundaries (plan §6.2)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hour, minute, expected",
    [
        (3, 59, "snack"),
        (4, 0, "breakfast"),
        (10, 59, "breakfast"),
        (11, 0, "lunch"),
        (15, 59, "lunch"),
        (16, 0, "dinner"),
        (21, 59, "dinner"),
        (22, 0, "snack"),
        (0, 0, "snack"),
        (23, 59, "snack"),
    ],
)
def test_infer_meal_type_boundaries(hour, minute, expected):
    assert infer_meal_type(datetime.time(hour, minute)) == expected


# ---------------------------------------------------------------------------
# normalize_control_text — trimmed, case-folded, whole-string (plan §8.1)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  Meal ", "meal"),
        ("HELLO", "hello"),
        ("Repeat", "repeat"),
        ("HoMe", "home"),
        ("\tHi\n", "hi"),
    ],
)
def test_normalize_control_text(raw, expected):
    assert normalize_control_text(raw) == expected


def test_control_sets_match_normalized_forms():
    assert normalize_control_text("Meal") in HOME_ACTIONS
    assert normalize_control_text("Repeat") in HOME_ACTIONS
    assert normalize_control_text("Describe") in HOME_ACTIONS
    assert normalize_control_text("Home") in HOME_WORDS
    for greeting in ("hi", "hello", "hey"):
        assert greeting in GREETINGS


def test_longer_strings_are_not_controls():
    # Whole-string matching only: "meal prep" is ordinary text, not a control.
    assert normalize_control_text("meal prep") not in HOME_ACTIONS
    assert normalize_control_text("hi there") not in GREETINGS


# ---------------------------------------------------------------------------
# pin/hide keyboard <-> handler contract
# ---------------------------------------------------------------------------
def _pref_callbacks(markup) -> dict[str, str]:
    found = {}
    for row in markup.inline_keyboard:
        for button in row:
            data = button.callback_data or ""
            if data.startswith("dpin_"):
                found["pin"] = data
            elif data.startswith("dhide_"):
                found["hide"] = data
    return found


def test_keyboard_emits_revisioned_base36_pin_hide():
    uid = next(iter(ALLOWED_USER_IDS))
    markup = food_portion_keyboard(
        uid,
        [{"id": 1, "name": "1 cup"}],
        [],
        is_pinned=False,
        hidden=False,
        revision=3,
    )
    cbs = _pref_callbacks(markup)
    # Desired next state: pin -> 1 (currently unpinned), hide -> 1 (currently shown).
    assert cbs["pin"] == f"dpin_{to_base36(uid)}_{to_base36(3)}_1"
    assert cbs["hide"] == f"dhide_{to_base36(uid)}_{to_base36(3)}_1"


def test_keyboard_pin_hide_match_registered_handler_patterns():
    """The emitted callbacks must match the CallbackQueryHandler patterns that
    route to set_pin/set_hide (the A1 regression: keyboard emitted the old
    single-token form that no handler matched)."""
    import re

    uid = next(iter(ALLOWED_USER_IDS))
    markup = food_portion_keyboard(
        uid, [{"id": 1, "name": "1 cup"}], [], revision=0
    )
    cbs = _pref_callbacks(markup)

    patterns = {}
    for state_handlers in diet.diet_conv_handler.states.values():
        for handler in state_handlers:
            cb = getattr(handler, "callback", None)
            name = getattr(cb, "__name__", "")
            pat = getattr(handler, "pattern", None)
            if name == "set_pin" and pat is not None:
                patterns["pin"] = pat
            elif name == "set_hide" and pat is not None:
                patterns["hide"] = pat

    assert "pin" in patterns and "hide" in patterns
    assert re.fullmatch(patterns["pin"], cbs["pin"])
    assert re.fullmatch(patterns["hide"], cbs["hide"])


def _pin_update(owner_id: int, acting_id: int, rev: int, desired: int, msg_id: int):
    query = SimpleNamespace(
        data=f"dpin_{to_base36(owner_id)}_{to_base36(rev)}_{desired}",
        message=SimpleNamespace(message_id=msg_id),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=acting_id),
        effective_chat=SimpleNamespace(type=ChatType.PRIVATE),
    )


def _pin_context(db):
    return SimpleNamespace(
        bot_data={"db": db},
        user_data={
            "diet_ui_revision": 0,
            "diet_ui_message_id": 555,
            "diet_sel_kind": "food",
            "diet_sel_id": 7,
            "diet_recent_qtys": [],
        },
    )


@pytest.mark.asyncio
async def test_set_pin_writes_desired_state_and_bumps_revision():
    uid = next(iter(ALLOWED_USER_IDS))
    db = SimpleNamespace(
        set_food_preference=AsyncMock(),
        get_food_preference=AsyncMock(return_value={"is_pinned": 1, "hidden": 0}),
        get_food_portions=AsyncMock(return_value=[]),
    )
    context = _pin_context(db)
    update = _pin_update(uid, uid, rev=0, desired=1, msg_id=555)

    result = await diet.set_pin(update, context)

    assert result == diet.PORTION_CHOICE
    db.set_food_preference.assert_awaited_once_with(uid, "food", 7, is_pinned=True)
    assert context.user_data["diet_ui_revision"] == 1


@pytest.mark.asyncio
async def test_set_pin_rejects_stale_revision_without_writing():
    uid = next(iter(ALLOWED_USER_IDS))
    db = SimpleNamespace(
        set_food_preference=AsyncMock(),
        get_food_preference=AsyncMock(return_value={}),
        get_food_portions=AsyncMock(return_value=[]),
    )
    context = _pin_context(db)  # server revision is 0
    update = _pin_update(uid, uid, rev=5, desired=1, msg_id=555)  # stale

    result = await diet.set_pin(update, context)

    assert result == diet.PORTION_CHOICE
    db.set_food_preference.assert_not_awaited()
    assert context.user_data["diet_ui_revision"] == 0


@pytest.mark.asyncio
async def test_set_pin_rejects_foreign_owner_without_writing():
    uid = next(iter(ALLOWED_USER_IDS))
    db = SimpleNamespace(
        set_food_preference=AsyncMock(),
        get_food_preference=AsyncMock(return_value={}),
        get_food_portions=AsyncMock(return_value=[]),
    )
    context = _pin_context(db)
    # Callback claims a different owner than the acting user.
    update = _pin_update(uid + 1, uid, rev=0, desired=1, msg_id=555)

    result = await diet.set_pin(update, context)

    assert result == diet.PORTION_CHOICE
    db.set_food_preference.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_pin_rejects_stale_message_without_writing():
    uid = next(iter(ALLOWED_USER_IDS))
    db = SimpleNamespace(
        set_food_preference=AsyncMock(),
        get_food_preference=AsyncMock(return_value={}),
        get_food_portions=AsyncMock(return_value=[]),
    )
    context = _pin_context(db)
    update = _pin_update(uid, uid, rev=0, desired=1, msg_id=999)  # wrong message

    result = await diet.set_pin(update, context)

    assert result == diet.PORTION_CHOICE
    db.set_food_preference.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_pin_fails_closed_on_malformed_token():
    uid = next(iter(ALLOWED_USER_IDS))
    db = SimpleNamespace(
        set_food_preference=AsyncMock(),
        get_food_preference=AsyncMock(return_value={}),
        get_food_portions=AsyncMock(return_value=[]),
    )
    context = _pin_context(db)
    update = _pin_update(uid, uid, rev=0, desired=1, msg_id=555)
    update.callback_query.data = "dpin_00_0_1"  # non-canonical owner token

    result = await diet.set_pin(update, context)

    assert result == diet.PORTION_CHOICE
    db.set_food_preference.assert_not_awaited()


# ---------------------------------------------------------------------------
# /suggestions reset preserves saved defaults (plan §7.5)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reset_preserves_default_but_clears_pin(db_with_user, user_id):
    food = (await db_with_user.save_food(user_id, "apple", "g", 100, calories=52))[
        "food"
    ]
    await db_with_user.set_food_preference(user_id, "food", food["id"], is_pinned=True)
    # Seed a saved default quantity directly (the B-phase setter is not built yet).
    async with db_with_user._write_operation():
        await db_with_user.conn.execute(
            "UPDATE user_food_preferences "
            "SET default_amount = ?, default_unit = ? "
            "WHERE user_id = ? AND source_type = 'food' AND source_id = ?",
            (150.0, "g", user_id, food["id"]),
        )

    cleared = await db_with_user.reset_food_preferences(user_id)

    assert cleared == 1
    pref = await db_with_user.get_food_preference(user_id, "food", food["id"])
    assert pref is not None  # row survives because it still carries a default
    assert pref["is_pinned"] == 0
    assert pref["hidden"] == 0
    assert pref["default_amount"] == 150.0
    assert pref["default_unit"] == "g"


@pytest.mark.asyncio
async def test_reset_deletes_pin_only_row(db_with_user, user_id):
    food = (await db_with_user.save_food(user_id, "rice", "g", 100, calories=130))[
        "food"
    ]
    await db_with_user.set_food_preference(user_id, "food", food["id"], hidden=True)

    cleared = await db_with_user.reset_food_preferences(user_id)

    assert cleared == 1
    # No default on this row, so reset removes it entirely.
    assert await db_with_user.get_food_preference(user_id, "food", food["id"]) is None
