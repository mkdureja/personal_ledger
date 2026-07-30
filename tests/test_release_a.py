"""Release A removal rehearsal (plan §14.1 / §14.3).

Automated proof of the rollback-critical behavior: under the safe-off production
config every Home/compatibility synchronization removes the persistent keyboard,
and the ``remove`` mode forces removal even for a would-be keyboard-eligible
user. The live real-client confirmation is a human step (it needs the deployed
bot + a Telegram client); these tests stand in for the CI-provable part.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import ReplyKeyboardRemove
from telegram.constants import ChatType

from bot import config
from bot.callback_data import to_base36
from bot.config import ALLOWED_USER_IDS
from bot.handlers import diet, home
from bot.main import build_application

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


def _context(db=None):
    return SimpleNamespace(bot_data={"db": db}, user_data={}, args=[])


def _snapshot_db():
    return SimpleNamespace(
        get_today_meal_count=AsyncMock(return_value=0),
        get_today_calories=AsyncMock(return_value=(0, False)),
        get_today_study_total=AsyncMock(return_value=0),
        get_today_gym_count=AsyncMock(return_value=0),
        get_active_habits=AsyncMock(return_value=[]),
        get_checked_habits=AsyncMock(return_value=set()),
        get_last_meal_summary=AsyncMock(return_value=None),
    )


def _last_markup(mock):
    return mock.call_args.kwargs.get("reply_markup")


# ---------------------------------------------------------------------------
# The rollback flag block loads and forces removal
# ---------------------------------------------------------------------------
async def test_remove_mode_forces_removal_even_for_phase1_user(monkeypatch):
    # remove mode ignores Phase 1 eligibility — the rollback guarantee.
    monkeypatch.setattr("bot.config.PHASE1_ENABLED_USER_IDS", frozenset({UID}))
    monkeypatch.setattr("bot.config.HOME_KEYBOARD_MODE", "remove")
    assert config.home_keyboard_action_for(UID) == "remove"


async def test_safe_off_config_builds_application():
    # The default conftest env is the safe-off Release A config (no Phase 1 IDs,
    # mode off); the whole handler table must construct cleanly.
    assert config.HOME_KEYBOARD_MODE in config.HOME_KEYBOARD_MODES
    assert config.PHASE1_ENABLED_USER_IDS == frozenset()
    app = build_application()
    assert app.update_processor.max_concurrent_updates == 1


# ---------------------------------------------------------------------------
# Every synchronization path removes the keyboard when Phase 1 is disabled
# ---------------------------------------------------------------------------
async def test_greeting_sync_removes_keyboard(monkeypatch):
    # In rollback (remove mode) a greeting opens Home whose second message carries
    # an explicit ReplyKeyboardRemove, synchronizing the client.
    monkeypatch.setattr("bot.config.HOME_KEYBOARD_MODE", "remove")
    update = _update("hi")
    await home.home_text_router(update, _context(_snapshot_db()))
    assert isinstance(_last_markup(update.effective_message.reply_text), ReplyKeyboardRemove)


async def test_meal_label_sync_removes_keyboard():
    update = _update("Meal")
    await diet.diet_home_entry(update, _context(SimpleNamespace(ensure_user=AsyncMock())))
    assert isinstance(_last_markup(update.effective_message.reply_text), ReplyKeyboardRemove)


async def test_voice_sync_removes_keyboard():
    update = _update()
    await home.home_voice_router(update, _context())
    assert isinstance(_last_markup(update.effective_message.reply_text), ReplyKeyboardRemove)


async def test_keyboard_hide_removes():
    update = _update()
    ctx = SimpleNamespace(bot_data={}, user_data={}, args=["hide"])
    await home.keyboard_command(update, ctx)
    assert isinstance(_last_markup(update.effective_message.reply_text), ReplyKeyboardRemove)


async def test_show_home_never_sends_bar_in_remove_mode(monkeypatch):
    # Even a Phase 1-enabled Home (so the snapshot renders) removes the bar.
    monkeypatch.setattr("bot.config.PHASE1_ENABLED_USER_IDS", frozenset({UID}))
    monkeypatch.setattr("bot.config.HOME_KEYBOARD_MODE", "remove")
    update = _update()
    await home.show_home(update, _context(_snapshot_db()))
    # Message 2 (the quick-action line) carries a removal, never a reply keyboard.
    assert isinstance(
        update.effective_message.reply_text.call_args_list[1].kwargs.get("reply_markup"),
        ReplyKeyboardRemove,
    )


async def test_stale_receipt_synchronizes_removal():
    query = SimpleNamespace(
        data=f"mr_undo_{to_base36(UID)}_{to_base36(7)}",
        message=SimpleNamespace(reply_text=AsyncMock()),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=UID),
        effective_chat=SimpleNamespace(type=ChatType.PRIVATE),
    )
    await diet.stale_receipt_callback(update, _context())
    assert isinstance(_last_markup(query.message.reply_text), ReplyKeyboardRemove)
