"""Focused tests for optional macro tracking in the /diet handler."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ParseMode
from telegram.error import NetworkError
from telegram.ext import ConversationHandler

from bot.config import ALLOWED_USER_IDS
from bot.handlers import diet
from bot.handlers.common import activate_conversation


def _user() -> SimpleNamespace:
    return SimpleNamespace(
        id=next(iter(ALLOWED_USER_IDS)),
        username="tester",
        first_name="Test",
    )


def _message(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(text=text, reply_text=AsyncMock())


def _context(
    db: object,
    *,
    args: list[str] | None = None,
    user_data: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        bot_data={"db": db},
        args=args or [],
        user_data=user_data if user_data is not None else {},
    )


def _update(message: SimpleNamespace | None = None) -> SimpleNamespace:
    message = message or _message()
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=_user(),
        effective_chat=SimpleNamespace(id=42),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "food_items", "calories"),
    [
        (["lunch", "dal+rice", "650"], "dal, rice", 650),
        (["snack", "apple"], "apple", None),
    ],
)
async def test_shortcut_without_macros_remains_backward_compatible(
    args: list[str], food_items: str, calories: int | None
) -> None:
    db = SimpleNamespace(ensure_user=AsyncMock(), log_diet=AsyncMock())
    context = _context(db, args=args)

    result = await diet.diet_command(_update(), context)

    assert result == ConversationHandler.END
    db.log_diet.assert_awaited_once_with(
        _user().id,
        args[0],
        food_items,
        calories,
        protein_g=None,
        carbs_g=None,
        fat_g=None,
    )


@pytest.mark.asyncio
async def test_shortcut_accepts_case_insensitive_decimal_macro_suffix() -> None:
    db = SimpleNamespace(ensure_user=AsyncMock(), log_diet=AsyncMock())
    message = _message()
    context = _context(
        db,
        args=["lunch", "dal+rice", "650", "P=25.5", "c=80", "F=15.25"],
    )

    result = await diet.diet_command(_update(message), context)

    assert result == ConversationHandler.END
    db.log_diet.assert_awaited_once_with(
        _user().id,
        "lunch",
        "dal, rice",
        650,
        protein_g=25.5,
        carbs_g=80.0,
        fat_g=15.25,
    )
    confirmation = message.reply_text.await_args.args[0]
    assert "P 25.5 g · C 80 g · F 15.25 g" in confirmation
    assert message.reply_text.await_args.kwargs["parse_mode"] == ParseMode.HTML


@pytest.mark.asyncio
async def test_shortcut_allows_partial_macros_without_calories() -> None:
    db = SimpleNamespace(ensure_user=AsyncMock(), log_diet=AsyncMock())
    context = _context(db, args=["breakfast", "eggs", "P=30", "f=12"])

    await diet.diet_command(_update(), context)

    db.log_diet.assert_awaited_once_with(
        _user().id,
        "breakfast",
        "eggs",
        None,
        protein_g=30.0,
        carbs_g=None,
        fat_g=12.0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "macro_tokens",
    [
        ["p=nan"],
        ["c=inf"],
        ["f=-0.1"],
        [f"p={diet.MAX_MACRO_GRAMS + 0.1}"],
        ["c=not-a-number"],
        ["p=20", "P=30"],
    ],
)
async def test_shortcut_rejects_invalid_or_duplicate_macros(
    macro_tokens: list[str],
) -> None:
    db = SimpleNamespace(ensure_user=AsyncMock(), log_diet=AsyncMock())
    message = _message()
    context = _context(db, args=["lunch", "dal", *macro_tokens])

    result = await diet.diet_command(_update(message), context)

    assert result == ConversationHandler.END
    db.log_diet.assert_not_awaited()
    message.reply_text.assert_awaited_once()


@pytest.mark.parametrize("raw", ["0", "0.0", "-0"])
def test_zero_macro_grams_are_valid_and_normalized(raw: str) -> None:
    assert diet._parse_macro_grams(raw, "Protein") == (0.0, None)


def test_extract_shortcut_macros_stops_at_non_macro() -> None:
    """TEST-5: Extracting macros stops at the first non-macro token from the end."""
    tokens = ["lunch", "p=10", "200", "c=20"]
    remaining, macros, error = diet._extract_shortcut_macros(tokens)
    assert remaining == ["lunch", "p=10", "200"]
    assert macros == {"protein_g": None, "carbs_g": 20.0, "fat_g": None}
    assert error is None


def test_looks_like_number_rejects_signs_and_fractions() -> None:
    """EDGE-3: Only unsigned integers are consumed as calories."""
    assert diet._looks_like_number("500") is True
    assert diet._looks_like_number("+500") is False
    assert diet._looks_like_number("-500") is False
    assert diet._looks_like_number(".5") is False
    assert diet._looks_like_number("0.5") is False


def test_macro_confirmation_escapes_food_and_only_renders_known_values() -> None:
    text = diet._confirmation(
        "lunch",
        "dal < rice & veg",
        None,
        protein_g=25.0,
        carbs_g=None,
        fat_g=10.5,
    )

    assert "dal &lt; rice &amp; veg" in text
    assert "P 25 g · F 10.5 g" in text
    assert " C " not in text


@pytest.mark.asyncio
async def test_guided_calories_advance_to_macro_prompt() -> None:
    db = SimpleNamespace(log_diet=AsyncMock())
    state = {"diet_meal_type": "lunch", "diet_food_items": "dal"}
    context = _context(db, user_data=state)
    message = _message("650")

    result = await diet.receive_calories(_update(message), context)

    assert result == diet.MACROS
    assert context.user_data["diet_food_items"] == "dal"
    assert context.user_data["diet_calories"] == 650
    assert "protein carbs fat" in message.reply_text.await_args.args[0]
    db.log_diet.assert_not_awaited()


@pytest.mark.asyncio
async def test_guided_skip_calories_still_offers_macros() -> None:
    db = SimpleNamespace(log_diet=AsyncMock())
    state = {"diet_meal_type": "lunch", "diet_food_items": "dal"}
    context = _context(db, user_data=state)
    message = _message("/skip")

    result = await diet.skip_calories(_update(message), context)

    assert result == diet.MACROS
    assert context.user_data["diet_food_items"] == "dal"
    assert context.user_data["diet_calories"] is None
    db.log_diet.assert_not_awaited()


@pytest.mark.asyncio
async def test_guided_macros_are_saved_and_conversation_is_cleaned() -> None:
    db = SimpleNamespace(log_diet=AsyncMock())
    state = {
        "diet_meal_type": "dinner",
        "diet_food_items": "tofu & rice",
        "diet_calories": 700,
    }
    context = _context(db, user_data=state)
    message = _message("40.5 90 20")
    update = _update(message)
    activate_conversation(update, context, "diet")

    result = await diet.receive_macros(update, context)

    assert result == ConversationHandler.END
    db.log_diet.assert_awaited_once_with(
        _user().id,
        "dinner",
        "tofu & rice",
        700,
        protein_g=40.5,
        carbs_g=90.0,
        fat_g=20.0,
    )
    assert context.user_data == {}
    assert "tofu &amp; rice" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "25 80",
        "25 80 15 5",
        "-1 80 15",
        "nan 80 15",
        f"{diet.MAX_MACRO_GRAMS + 1} 80 15",
    ],
)
async def test_guided_invalid_macros_stay_in_macro_state(text: str) -> None:
    db = SimpleNamespace(log_diet=AsyncMock())
    state = {
        "diet_meal_type": "dinner",
        "diet_food_items": "rice",
        "diet_calories": 500,
    }
    context = _context(db, user_data=state)
    message = _message(text)

    result = await diet.receive_macros(_update(message), context)

    assert result == diet.MACROS
    assert context.user_data == state
    db.log_diet.assert_not_awaited()
    message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_guided_skip_macros_saves_null_macro_values() -> None:
    db = SimpleNamespace(log_diet=AsyncMock())
    state = {
        "diet_meal_type": "snack",
        "diet_food_items": "apple",
        "diet_calories": None,
    }
    context = _context(db, user_data=state)
    update = _update(_message("/skip"))
    activate_conversation(update, context, "diet")

    result = await diet.skip_macros(update, context)

    assert result == ConversationHandler.END
    db.log_diet.assert_awaited_once_with(
        _user().id,
        "snack",
        "apple",
        None,
        protein_g=None,
        carbs_g=None,
        fat_g=None,
    )
    assert context.user_data == {}


@pytest.mark.asyncio
async def test_macro_prompt_failure_cleans_unpersisted_conversation() -> None:
    db = SimpleNamespace(log_diet=AsyncMock())
    state = {"diet_meal_type": "lunch", "diet_food_items": "dal"}
    context = _context(db, user_data=state)
    message = _message("650")
    message.reply_text.side_effect = NetworkError("offline")
    update = _update(message)
    activate_conversation(update, context, "diet")

    result = await diet.receive_calories(update, context)

    assert result == ConversationHandler.END
    assert context.user_data == {}
    db.log_diet.assert_not_awaited()


@pytest.mark.asyncio
async def test_macro_validation_delivery_failure_cleans_conversation() -> None:
    db = SimpleNamespace(log_diet=AsyncMock())
    state = {
        "diet_meal_type": "lunch",
        "diet_food_items": "dal",
        "diet_calories": 650,
    }
    context = _context(db, user_data=state)
    message = _message("invalid")
    message.reply_text.side_effect = NetworkError("offline")
    update = _update(message)
    activate_conversation(update, context, "diet")

    result = await diet.receive_macros(update, context)

    assert result == ConversationHandler.END
    assert context.user_data == {}
    db.log_diet.assert_not_awaited()


@pytest.mark.asyncio
async def test_macro_database_failure_preserves_pending_state() -> None:
    db = SimpleNamespace(log_diet=AsyncMock(side_effect=RuntimeError("db down")))
    state = {
        "diet_meal_type": "dinner",
        "diet_food_items": "rice",
        "diet_calories": 500,
    }
    context = _context(db, user_data=state)
    message = _message("25 80 15")
    update = _update(message)
    activate_conversation(update, context, "diet")
    expected_state = dict(context.user_data)

    with pytest.raises(RuntimeError, match="db down"):
        await diet.receive_macros(update, context)

    assert context.user_data == expected_state
    message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_macro_confirmation_failure_still_ends_persisted_flow() -> None:
    db = SimpleNamespace(log_diet=AsyncMock())
    state = {
        "diet_meal_type": "dinner",
        "diet_food_items": "rice",
        "diet_calories": 500,
    }
    context = _context(db, user_data=state)
    message = _message("25 80 15")
    message.reply_text.side_effect = NetworkError("offline")
    update = _update(message)
    activate_conversation(update, context, "diet")

    result = await diet.receive_macros(update, context)

    assert result == ConversationHandler.END
    db.log_diet.assert_awaited_once()
    assert context.user_data == {}
