"""Phase 1a Home-surface behavior: snapshot, routers, /keyboard, Diet entry,
per-state interceptors, and the Meal re-render (plan §8.3/§8.5/§8.6/§13.2)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import (
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ChatType
from telegram.ext import ConversationHandler

from bot.config import ALLOWED_USER_IDS
from bot.handlers import diet, home
from bot.handlers.common import activate_conversation, active_conversation_flow

UID = next(iter(ALLOWED_USER_IDS))

pytestmark = pytest.mark.asyncio


def _update(text: str | None = None):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(
        effective_message=message,
        message=message,
        effective_user=SimpleNamespace(id=UID, first_name="Manoj", username="m"),
        effective_chat=SimpleNamespace(id=UID, type=ChatType.PRIVATE),
    )


def _context(db=None, user_data=None, args=None):
    return SimpleNamespace(
        bot_data={"db": db},
        user_data=user_data if user_data is not None else {},
        args=args or [],
    )


def _snapshot_db():
    return SimpleNamespace(
        get_today_meal_count=AsyncMock(return_value=2),
        get_today_calories=AsyncMock(return_value=(1500, True)),
        get_today_study_total=AsyncMock(return_value=90),
        get_today_gym_count=AsyncMock(return_value=3),
        get_active_habits=AsyncMock(return_value=[{"id": 1}, {"id": 2}]),
        get_checked_habits=AsyncMock(return_value={1}),
    )


def _markup(mock, call_index):
    _, kwargs = mock.call_args_list[call_index]
    return kwargs.get("reply_markup")


def _enable_phase1(monkeypatch, *, keyboard="off"):
    monkeypatch.setattr("bot.config.PHASE1_ENABLED_USER_IDS", frozenset({UID}))
    monkeypatch.setattr("bot.config.HOME_KEYBOARD_MODE", keyboard)
    if keyboard in ("on",):
        monkeypatch.setattr("bot.config.HOME_KEYBOARD_PILOT_USER_IDS", frozenset())


# ---------------------------------------------------------------------------
# show_home
# ---------------------------------------------------------------------------
async def test_show_home_off_mode_is_single_message_snapshot_plus_menu():
    update = _update()
    db = _snapshot_db()
    await home.show_home(update, _context(db))

    reply = update.effective_message.reply_text
    # Dark Home is one clean message: today snapshot + inline main menu, no bar,
    # no keyboard-removal noise.
    assert reply.await_count == 1
    snapshot_text = reply.call_args_list[0].args[0]
    assert "2 meal(s)" in snapshot_text
    assert "1500 cal (some incomplete)" in snapshot_text
    assert "90 min" in snapshot_text
    assert "3 exercise(s)" in snapshot_text
    assert "1/2 done" in snapshot_text
    assert isinstance(_markup(reply, 0), InlineKeyboardMarkup)


async def test_show_home_sends_keyboard_when_eligible(monkeypatch):
    _enable_phase1(monkeypatch, keyboard="on")
    update = _update()
    await home.show_home(update, _context(_snapshot_db()))
    assert isinstance(
        _markup(update.effective_message.reply_text, 1), ReplyKeyboardMarkup
    )


async def test_show_home_escapes_first_name():
    update = _update()
    update.effective_user.first_name = "<b>x</b>"
    await home.show_home(update, _context(_snapshot_db()))
    snapshot_text = update.effective_message.reply_text.call_args_list[0].args[0]
    assert "&lt;b&gt;x&lt;/b&gt;" in snapshot_text


# ---------------------------------------------------------------------------
# home_text_router — Phase 1 disabled (Release A production)
# ---------------------------------------------------------------------------
async def test_router_greeting_opens_home_even_when_disabled():
    # The whole point of the fix: a greeting opens the home page (snapshot +
    # inline menu) for everyone, not the terse "Use /menu" guidance.
    update = _update("hi")
    await home.home_text_router(update, _context(_snapshot_db()))
    reply = update.effective_message.reply_text
    assert reply.await_count == 1
    assert isinstance(_markup(reply, 0), InlineKeyboardMarkup)


async def test_router_disabled_fast_action_says_not_enabled():
    # Meal/Repeat/Describe (the B fast actions) stay gated when Phase 1 is off.
    update = _update("repeat")
    await home.home_text_router(update, _context())
    reply = update.effective_message.reply_text
    reply.assert_awaited_once()
    assert "hi" in reply.call_args.args[0].lower()


async def test_router_disabled_arbitrary_text_is_silent():
    update = _update("banana")
    await home.home_text_router(update, _context())
    update.effective_message.reply_text.assert_not_awaited()


async def test_router_active_flow_gets_hint_and_no_mutation():
    update = _update("hi")
    context = _context()
    activate_conversation(update, context, "study")
    await home.home_text_router(update, context)
    reply = update.effective_message.reply_text
    reply.assert_awaited_once()
    assert "Finish this flow" in reply.call_args.args[0]


# ---------------------------------------------------------------------------
# home_text_router — Phase 1 enabled
# ---------------------------------------------------------------------------
async def test_router_enabled_greeting_shows_home(monkeypatch):
    _enable_phase1(monkeypatch, keyboard="on")
    update = _update("hello")
    await home.home_text_router(update, _context(_snapshot_db()))
    # Home snapshot + (keyboard on) the quick-action bar = two messages.
    assert update.effective_message.reply_text.await_count == 2


async def test_router_enabled_repeat_is_no_op_message(monkeypatch):
    _enable_phase1(monkeypatch)
    update = _update("repeat")
    await home.home_text_router(update, _context(_snapshot_db()))
    reply = update.effective_message.reply_text
    reply.assert_awaited_once()
    assert "Repeat isn't enabled" in reply.call_args.args[0]


async def test_router_enabled_meal_is_defensive_diet_guidance(monkeypatch):
    _enable_phase1(monkeypatch)
    update = _update("meal")
    await home.home_text_router(update, _context(_snapshot_db()))
    reply = update.effective_message.reply_text
    reply.assert_awaited_once()
    assert "/diet" in reply.call_args.args[0]


# ---------------------------------------------------------------------------
# home_voice_router — never downloads
# ---------------------------------------------------------------------------
async def test_voice_router_disabled_removes_keyboard():
    update = _update()
    await home.home_voice_router(update, _context())
    reply = update.effective_message.reply_text
    reply.assert_awaited_once()
    assert isinstance(reply.call_args.kwargs.get("reply_markup"), ReplyKeyboardRemove)


async def test_voice_router_enabled_says_not_enabled(monkeypatch):
    _enable_phase1(monkeypatch)
    update = _update()
    await home.home_voice_router(update, _context())
    reply = update.effective_message.reply_text
    reply.assert_awaited_once()
    assert "Voice logging isn't enabled" in reply.call_args.args[0]


# ---------------------------------------------------------------------------
# /keyboard hide|show
# ---------------------------------------------------------------------------
async def test_keyboard_hide_always_removes():
    update = _update()
    await home.keyboard_command(update, _context(args=["hide"]))
    reply = update.effective_message.reply_text
    assert isinstance(reply.call_args.kwargs.get("reply_markup"), ReplyKeyboardRemove)


async def test_keyboard_show_ineligible_removes():
    update = _update()
    await home.keyboard_command(update, _context(args=["show"]))
    reply = update.effective_message.reply_text
    assert isinstance(reply.call_args.kwargs.get("reply_markup"), ReplyKeyboardRemove)


async def test_keyboard_show_eligible_sends_bar(monkeypatch):
    _enable_phase1(monkeypatch, keyboard="on")
    update = _update()
    await home.keyboard_command(update, _context(args=["show"]))
    reply = update.effective_message.reply_text
    assert isinstance(reply.call_args.kwargs.get("reply_markup"), ReplyKeyboardMarkup)


async def test_keyboard_show_during_flow_only_removes(monkeypatch):
    _enable_phase1(monkeypatch, keyboard="on")
    update = _update()
    context = _context(args=["show"])
    activate_conversation(update, context, "diet")
    await home.keyboard_command(update, context)
    reply = update.effective_message.reply_text
    assert isinstance(reply.call_args.kwargs.get("reply_markup"), ReplyKeyboardRemove)
    assert "Finish this flow" in reply.call_args.args[0]


# ---------------------------------------------------------------------------
# diet_home_entry (the "Meal" entry point)
# ---------------------------------------------------------------------------
async def test_diet_home_entry_disabled_gives_guidance_no_db():
    update = _update("Meal")
    db = SimpleNamespace(ensure_user=AsyncMock())
    context = _context(db)
    result = await diet.diet_home_entry(update, context)
    assert result == ConversationHandler.END
    db.ensure_user.assert_not_awaited()
    assert active_conversation_flow(context) is None
    assert isinstance(
        update.effective_message.reply_text.call_args.kwargs.get("reply_markup"),
        ReplyKeyboardRemove,
    )


async def test_diet_home_entry_enabled_opens_quick_food_choice(monkeypatch):
    _enable_phase1(monkeypatch)
    update = _update("Meal")
    db = SimpleNamespace(
        ensure_user=AsyncMock(),
        list_foods=AsyncMock(return_value=[]),
        list_recipes=AsyncMock(return_value=[]),
    )
    context = _context(db)
    result = await diet.diet_home_entry(update, context)

    assert result == diet.FOOD_CHOICE
    db.ensure_user.assert_awaited_once()
    assert active_conversation_flow(context) == "diet"
    assert context.user_data["diet_entry_mode"] == diet.DietEntryMode.QUICK
    assert context.user_data["diet_meal_type"] in {
        "breakfast", "lunch", "dinner", "snack",
    }
    assert context.user_data["diet_choice_page"] == 0
    assert context.user_data["diet_ui_revision"] == 0


async def test_diet_home_entry_blocked_by_other_active_flow(monkeypatch):
    _enable_phase1(monkeypatch)
    update = _update("Meal")
    db = SimpleNamespace(ensure_user=AsyncMock())
    context = _context(db)
    activate_conversation(update, context, "study")
    result = await diet.diet_home_entry(update, context)
    assert result == ConversationHandler.END
    db.ensure_user.assert_not_awaited()
    # Study flow marker is untouched.
    assert active_conversation_flow(context) == "study"


# ---------------------------------------------------------------------------
# Shared per-state interceptors
# ---------------------------------------------------------------------------
async def test_control_interceptor_nudges_and_preserves_state():
    from bot.handlers.common import active_flow_control_interceptor

    update = _update("Repeat")
    result = await active_flow_control_interceptor(update, _context())
    assert result is None
    assert "Finish this flow" in update.effective_message.reply_text.call_args.args[0]


async def test_voice_interceptor_does_not_download():
    from bot.handlers.common import voice_not_enabled_interceptor

    # No get_file on the update object: a download attempt would AttributeError.
    update = _update()
    result = await voice_not_enabled_interceptor(update, _context())
    assert result is None
    assert "Voice logging isn't enabled" in update.effective_message.reply_text.call_args.args[0]


async def test_text_catchall_absorbs_arbitrary_text():
    from bot.handlers.common import buttons_or_cancel_catchall

    update = _update("banana")
    result = await buttons_or_cancel_catchall(update, _context())
    assert result is None
    assert "buttons or /cancel" in update.effective_message.reply_text.call_args.args[0]


# ---------------------------------------------------------------------------
# Meal re-render preserves the draft and state (plan §8.5)
# ---------------------------------------------------------------------------
async def test_meal_rerender_meal_type_state():
    update = _update("Meal")
    context = _context(_snapshot_db(), user_data={"diet_meal_type": "lunch"})
    result = await diet._rerender_diet_state(update, context, diet.MEAL_TYPE)
    assert result == diet.MEAL_TYPE
    assert context.user_data["diet_meal_type"] == "lunch"  # unchanged
    update.effective_message.reply_text.assert_awaited_once()


async def test_meal_rerender_calories_state_preserves_draft():
    update = _update("Meal")
    context = _context(
        _snapshot_db(),
        user_data={
            "diet_meal_type": "lunch",
            "diet_food_items": "dal",
            "diet_calories": 650,
        },
    )
    result = await diet._rerender_diet_state(update, context, diet.CALORIES)
    assert result == diet.CALORIES
    assert context.user_data["diet_food_items"] == "dal"
    assert context.user_data["diet_calories"] == 650


async def test_meal_rerender_fails_closed_when_meal_type_missing():
    update = _update("Meal")
    context = _context(_snapshot_db(), user_data={})
    result = await diet._rerender_diet_state(update, context, diet.FOOD_CHOICE)
    assert result == ConversationHandler.END
    assert "expired" in update.effective_message.reply_text.call_args.args[0].lower()
