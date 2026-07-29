"""Phase 1a real-dispatcher routing matrix (plan §13.1).

These drive the *real* handler table from ``build_application()`` and assert
which registered handler would consume a given Update — the genuine group order,
AUTH_FILTER, conversation state, and callback patterns — with production flags
off. No network loop and no DB are needed to prove routing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User, Voice

_BOT = MagicMock()
_BOT.username = "LedgerTestBot"

from bot.callback_data import to_base36
from bot.handlers.diet import (
    diet_conv_handler,
    stale_phase1_diet_callback,
    stale_receipt_callback,
)
from bot.handlers.receipts import undo_from_receipt
from bot.handlers.gym import gym_conv_handler
from bot.handlers.habits import habits_setup_conv_handler
from bot.handlers.study import study_conv_handler
from bot.handlers import diet as diet_mod
from bot.handlers import gym as gym_mod
from bot.handlers import study as study_mod
from bot.handlers import habits as habits_mod
from bot.main import build_application

MANOJ = 123456789  # matches conftest ALLOWED_USER_IDS
OUTSIDER = 555000111

_CONV_HANDLERS = (
    study_conv_handler,
    gym_conv_handler,
    diet_conv_handler,
    habits_setup_conv_handler,
)


@pytest.fixture(autouse=True)
def _reset_conversations():
    """Clear any conversation state the tests set on the shared singletons."""
    yield
    for handler in _CONV_HANDLERS:
        handler._conversations.clear()


def _first_handler(app, update):
    for group in sorted(app.handlers):
        for handler in app.handlers[group]:
            if handler.check_update(update):
                return handler
    return None


def _name(handler) -> str | None:
    return getattr(getattr(handler, "callback", None), "__name__", None)


def _text_update(user_id: int, text: str, *, chat_type: str = "private") -> Update:
    chat = Chat(id=user_id, type=chat_type)
    user = User(id=user_id, is_bot=False, first_name="U")
    message = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=chat,
        from_user=user, text=text,
    )
    message.set_bot(_BOT)
    return Update(update_id=1, message=message)


def _command_update(user_id: int, text: str) -> Update:
    chat = Chat(id=user_id, type="private")
    user = User(id=user_id, is_bot=False, first_name="U")
    command = text.split()[0]
    entities = [
        MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(command))
    ]
    message = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=chat,
        from_user=user, text=text, entities=entities,
    )
    message.set_bot(_BOT)
    return Update(update_id=1, message=message)


def _voice_update(user_id: int, *, chat_type: str = "private") -> Update:
    chat = Chat(id=user_id, type=chat_type)
    user = User(id=user_id, is_bot=False, first_name="U")
    voice = Voice(file_id="f", file_unique_id="u", duration=1)
    message = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=chat,
        from_user=user, voice=voice,
    )
    message.set_bot(_BOT)
    return Update(update_id=1, message=message)


def _callback_update(user_id: int, data: str) -> Update:
    chat = Chat(id=user_id, type="private")
    user = User(id=user_id, is_bot=False, first_name="U")
    message = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=chat, from_user=user
    )
    message.set_bot(_BOT)
    query = CallbackQuery(
        id="1", from_user=user, chat_instance="ci", data=data, message=message
    )
    query.set_bot(_BOT)
    return Update(update_id=1, callback_query=query)


# ---------------------------------------------------------------------------
# Idle routing (no active conversation)
# ---------------------------------------------------------------------------
def test_meal_label_enters_diet_conversation():
    app = build_application()
    assert _first_handler(app, _text_update(MANOJ, "Meal")) is diet_conv_handler
    # Normalization: trimmed + case-folded whole string.
    assert _first_handler(app, _text_update(MANOJ, "  meal ")) is diet_conv_handler


@pytest.mark.parametrize("text", ["hi", "Hello", "HEY", "home", "Home", "repeat", "Describe"])
def test_control_and_greeting_text_reaches_home_router(text):
    app = build_application()
    assert _name(_first_handler(app, _text_update(MANOJ, text))) == "home_text_router"


@pytest.mark.parametrize("text", ["banana", "meal prep", "hi there"])
def test_arbitrary_text_reaches_home_router(text):
    app = build_application()
    assert _name(_first_handler(app, _text_update(MANOJ, text))) == "home_text_router"


def test_voice_reaches_home_voice_router():
    app = build_application()
    assert _name(_first_handler(app, _voice_update(MANOJ))) == "home_voice_router"


def test_keyboard_command_routes():
    app = build_application()
    assert _name(_first_handler(app, _command_update(MANOJ, "/keyboard hide"))) == "keyboard_command"


def test_section_commands_still_enter_conversations():
    app = build_application()
    assert _first_handler(app, _command_update(MANOJ, "/diet")) is diet_conv_handler
    assert _first_handler(app, _command_update(MANOJ, "/study")) is study_conv_handler
    assert _first_handler(app, _command_update(MANOJ, "/gym")) is gym_conv_handler
    assert _first_handler(app, _command_update(MANOJ, "/habits")) is habits_setup_conv_handler


# ---------------------------------------------------------------------------
# Access control: nothing at Home claims a group chat or an outsider
# ---------------------------------------------------------------------------
def test_group_chat_text_is_not_claimed_by_home():
    app = build_application()
    handler = _first_handler(app, _text_update(MANOJ, "hi", chat_type="group"))
    assert handler is None


def test_outsider_text_is_not_claimed():
    app = build_application()
    assert _first_handler(app, _text_update(OUTSIDER, "hi")) is None
    assert _first_handler(app, _text_update(OUTSIDER, "Meal")) is None
    assert _first_handler(app, _voice_update(OUTSIDER)) is None


# ---------------------------------------------------------------------------
# Stale Phase 1 callbacks (no active flow) route to the inert compat handlers
# ---------------------------------------------------------------------------
_O = to_base36(MANOJ)
_R = to_base36(0)


@pytest.mark.parametrize(
    "data",
    [
        f"dpage_{_O}_{_R}",
        f"dchangemeal_{_O}",
        f"dmanage_{_O}_{_R}_f_{to_base36(5)}",
        f"dpin_{_O}_{_R}_1",
        f"dhide_{_O}_{_R}_0",
        f"dq_log_{_O}_{_R}",
        f"dd_use_{_O}_{_R}",
        f"dadd_{_O}_{_R}",
        f"dsave_{_O}_{_R}",
        f"dqty_{_O}_{_R}_{to_base36(2)}",
        f"dedit_{_O}_{_R}_{to_base36(1)}",
        f"dremove_{_O}_{_R}_{to_base36(0)}",
        f"cv_keep_{_O}_{_R}_{to_base36(9)}",
        f"cv_save_{_O}_{_R}",
    ],
)
def test_phase1_base36_callbacks_route_to_inert_stale_handler(data):
    app = build_application()
    handler = _first_handler(app, _callback_update(MANOJ, data))
    assert handler.callback is stale_phase1_diet_callback


def test_receipt_undo_routes_to_its_real_handler():
    """Undo lives outside every conversation so a receipt stays usable."""
    app = build_application()
    handler = _first_handler(app, _callback_update(MANOJ, f"mr_undo_{_O}_{to_base36(7)}"))
    assert handler.callback is undo_from_receipt


def test_receipt_log_another_routes_to_the_diet_entry_point():
    app = build_application()
    handler = _first_handler(app, _callback_update(MANOJ, f"mr_more_{_O}"))
    assert handler is diet_conv_handler


def test_receipt_log_another_during_an_active_diet_flow_is_retired_inertly():
    """Its entry point cannot fire mid-flow; the inert handler answers instead."""
    app = build_application()
    diet_conv_handler._conversations[(MANOJ, MANOJ)] = diet_mod.FOOD_CHOICE
    handler = _first_handler(app, _callback_update(MANOJ, f"mr_more_{_O}"))
    assert handler.callback is stale_receipt_callback


def test_receipt_current_values_routes_to_the_diet_entry_point():
    app = build_application()
    handler = _first_handler(
        app, _callback_update(MANOJ, f"mr_current_{_O}_{to_base36(7)}")
    )
    assert handler is diet_conv_handler


def test_receipt_current_values_during_an_active_flow_is_retired_inertly():
    app = build_application()
    diet_conv_handler._conversations[(MANOJ, MANOJ)] = diet_mod.FOOD_CHOICE
    handler = _first_handler(
        app, _callback_update(MANOJ, f"mr_current_{_O}_{to_base36(7)}")
    )
    assert handler.callback is stale_receipt_callback


# ---------------------------------------------------------------------------
# Active-flow routing: control/greeting text stays inside the owning flow and
# never leaks to the Home router (plan §8.5).
# ---------------------------------------------------------------------------
def _activate(handler, state):
    handler._conversations[(MANOJ, MANOJ)] = state


@pytest.mark.parametrize("text", ["Meal", "hi", "repeat", "banana"])
def test_active_study_claims_all_text(text):
    app = build_application()
    _activate(study_conv_handler, study_mod.SUBJECT)
    assert _first_handler(app, _text_update(MANOJ, text)) is study_conv_handler


@pytest.mark.parametrize("text", ["Meal", "hello", "describe", "random note"])
def test_active_gym_more_claims_all_text(text):
    app = build_application()
    _activate(gym_conv_handler, gym_mod.MORE)
    assert _first_handler(app, _text_update(MANOJ, text)) is gym_conv_handler


@pytest.mark.parametrize("text", ["Meal", "repeat", "hey", "banana"])
def test_active_diet_food_choice_claims_all_text(text):
    app = build_application()
    _activate(diet_conv_handler, diet_mod.FOOD_CHOICE)
    assert _first_handler(app, _text_update(MANOJ, text)) is diet_conv_handler


@pytest.mark.parametrize("text", ["hi", "describe", "cook dinner"])
def test_active_habit_setup_claims_nonmeal_text(text):
    app = build_application()
    _activate(habits_setup_conv_handler, habits_mod.ADDING_HABIT)
    assert _first_handler(app, _text_update(MANOJ, text)) is habits_setup_conv_handler


def test_meal_during_habit_setup_is_claimed_by_diet_entry_guard():
    """Habits is registered after Diet, so "Meal" is claimed by the Diet entry
    point; its pre-DB conversation_available guard returns the habit hint and
    never mutates habit state (plan §13.1)."""
    app = build_application()
    _activate(habits_setup_conv_handler, habits_mod.ADDING_HABIT)
    assert _first_handler(app, _text_update(MANOJ, "Meal")) is diet_conv_handler


def test_active_flow_voice_stays_in_flow():
    app = build_application()
    _activate(study_conv_handler, study_mod.SUBJECT)
    assert _first_handler(app, _voice_update(MANOJ)) is study_conv_handler


# ---------------------------------------------------------------------------
# Phase 1b states: every new screen must claim text and voice itself, or a
# greeting typed mid-flow would open Home behind the user's back (plan §8.5).
# ---------------------------------------------------------------------------
_PHASE1B_STATES = [
    "QUICK_CONFIRM",
    "DEFAULT_MENU",
    "DEFAULT_AMOUNT",
    "DEFAULT_CONFIRM",
    "CURRENT_VALUES_REVIEW",
]


@pytest.mark.parametrize("state_name", _PHASE1B_STATES)
@pytest.mark.parametrize("text", ["Meal", "hi", "repeat", "banana"])
def test_phase1b_states_claim_all_text(state_name, text):
    app = build_application()
    _activate(diet_conv_handler, getattr(diet_mod, state_name))
    assert _first_handler(app, _text_update(MANOJ, text)) is diet_conv_handler


@pytest.mark.parametrize("state_name", _PHASE1B_STATES)
def test_phase1b_states_claim_voice_without_downloading(state_name):
    app = build_application()
    _activate(diet_conv_handler, getattr(diet_mod, state_name))
    assert _first_handler(app, _voice_update(MANOJ)) is diet_conv_handler


@pytest.mark.parametrize(
    "state_name,data",
    [
        ("QUICK_CONFIRM", f"dq_log_{_O}_{_R}"),
        ("QUICK_CONFIRM", f"dq_default_{_O}_{_R}"),
        ("QUICK_CONFIRM", f"dq_amount_{_O}_{_R}"),
        ("QUICK_CONFIRM", f"dq_cancel_{_O}_{_R}"),
        ("DEFAULT_MENU", f"dd_use_{_O}_{_R}"),
        ("DEFAULT_MENU", f"dd_edit_{_O}_{_R}"),
        ("DEFAULT_MENU", f"dd_clear_{_O}_{_R}"),
        ("DEFAULT_MENU", f"dd_back_{_O}_{_R}"),
        ("DEFAULT_CONFIRM", f"dd_save_{_O}_{_R}"),
        ("DEFAULT_CONFIRM", f"dd_reenter_{_O}_{_R}"),
        ("DEFAULT_CONFIRM", f"dd_cancel_{_O}_{_R}"),
        ("CURRENT_VALUES_REVIEW", f"cv_keep_{_O}_{_R}_{to_base36(3)}"),
        ("CURRENT_VALUES_REVIEW", f"cv_remove_{_O}_{_R}_{to_base36(3)}"),
        ("CURRENT_VALUES_REVIEW", f"cv_save_{_O}_{_R}"),
        ("CURRENT_VALUES_REVIEW", f"cv_cancel_{_O}_{_R}"),
        ("FOOD_CHOICE", f"dmanage_{_O}_{_R}_f_{to_base36(5)}"),
        ("FOOD_CHOICE", f"dpage_{_O}_{to_base36(2)}"),
        ("FOOD_CHOICE", f"dchangemeal_{_O}"),
        ("CONFIRM_ITEM", f"dadd_{_O}_{_R}"),
        ("CONFIRM_ITEM", f"dsave_{_O}_{_R}"),
        ("CONFIRM_ITEM", f"dqty_{_O}_{_R}_{to_base36(0)}"),
        ("CONFIRM_ITEM", f"dedit_{_O}_{_R}_{to_base36(0)}"),
        ("CONFIRM_ITEM", f"dremove_{_O}_{_R}_{to_base36(0)}"),
    ],
)
def test_phase1b_callbacks_reach_their_live_state_handler(state_name, data):
    app = build_application()
    _activate(diet_conv_handler, getattr(diet_mod, state_name))
    assert _first_handler(app, _callback_update(MANOJ, data)) is diet_conv_handler


@pytest.mark.parametrize(
    "data",
    [
        f"dq_log_{_O}_{_R}",
        f"dd_save_{_O}_{_R}",
        f"cv_save_{_O}_{_R}",
        f"dqty_{_O}_{_R}_{to_base36(0)}",
    ],
)
def test_phase1b_callbacks_after_the_flow_ends_are_retired_inertly(data):
    """No active conversation: the global stale handler answers, never mutates."""
    app = build_application()
    handler = _first_handler(app, _callback_update(MANOJ, data))
    assert handler.callback is stale_phase1_diet_callback


def test_every_emitted_callback_fits_telegram_with_max_ids():
    """64 bytes is a hard Telegram limit; base-36 must keep us well under it."""
    from telegram import InlineKeyboardMarkup

    from bot import keyboards

    big = 2**63 - 1  # the largest signed 64-bit id Telegram could ever send
    items = [
        {"display_name": "x", "source_type": "food", "source_id": big}
    ] * 20
    proposals = [
        SimpleNamespace(source_child_id=big, decision="unresolved")
        for _ in range(5)
    ]
    markups: list[InlineKeyboardMarkup] = [
        keyboards.meal_receipt_keyboard(big, big, can_use_current=True),
        keyboards.quick_confirm_keyboard(big, big, can_set_default=True),
        keyboards.default_menu_keyboard(
            big, big, has_default=True, needs_repair=True
        ),
        keyboards.default_confirm_keyboard(big, big),
        keyboards.current_values_keyboard(big, big, proposals, can_save=True),
        keyboards.diet_save_keyboard(
            big, phase1_enabled=True, revision=big, items=items
        ),
        keyboards.food_choice_keyboard(
            big,
            [{"source_type": "food", "id": big, "name": "x"}] * 30,
            manage=True,
            revision=big,
            paginate=True,
            change_meal=True,
        ),
    ]
    for markup in markups:
        for row in markup.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode("utf-8")) < 64, (
                    button.callback_data
                )
