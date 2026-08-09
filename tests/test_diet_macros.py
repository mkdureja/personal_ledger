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
from bot.handlers.common import activate_conversation, active_conversation_flow

def assert_log_diet(db_mock, user_id, meal_type, food_items, calories, protein_g=None, carbs_g=None, fat_g=None, source=None):
    db_mock.log_diet_with_items.assert_awaited_once()
    args, kwargs = db_mock.log_diet_with_items.call_args
    assert args[0] == user_id
    assert args[1] == meal_type
    assert kwargs.get("source") == source
    assert len(args[2]) == 1
    child = args[2][0]
    assert child["source_type"] == "freetext"
    assert child["display_name"] == food_items
    assert child["calories"] == calories
    assert child.get("protein_g") == protein_g
    assert child.get("carbs_g") == carbs_g
    assert child.get("fat_g") == fat_g



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
    ("args", "missing"),
    [
        (["lunch", "dal+rice", "650"], "p="),          # calories only
        (["snack", "apple"], "calories"),               # nothing at all
        (["breakfast", "eggs", "P=30", "f=12"], "c="),  # partial macros
    ],
)
async def test_the_shortcut_refuses_an_incomplete_meal(
    args: list[str], missing: str
) -> None:
    """The fastest way to log is not allowed to be the sloppiest.

    ``/diet lunch dal+rice 650`` used to write a row with calories and no
    macros. Now it explains what is missing and writes nothing.
    """
    db = SimpleNamespace(ensure_user=AsyncMock(), log_diet_with_items=AsyncMock())
    message = _message()
    context = _context(db, args=args)

    result = await diet.diet_command(_update(message), context)

    assert result == ConversationHandler.END
    db.log_diet_with_items.assert_not_awaited()
    said = message.reply_text.await_args.args[0]
    assert missing in said
    assert "/food add" in said, "the reply must point at the way to avoid retyping"


@pytest.mark.asyncio
async def test_the_shortcut_saves_when_every_nutrient_is_present() -> None:
    db = SimpleNamespace(ensure_user=AsyncMock(), log_diet_with_items=AsyncMock())
    context = _context(db, args=["lunch", "dal+rice", "650", "p=25", "c=80", "f=15"])

    result = await diet.diet_command(_update(), context)

    assert result == ConversationHandler.END
    assert_log_diet(
        db,
        _user().id,
        "lunch",
        "dal, rice",
        650,
        protein_g=25.0,
        carbs_g=80.0,
        fat_g=15.0,
        source=None,
    )


@pytest.mark.asyncio
async def test_shortcut_accepts_case_insensitive_decimal_macro_suffix() -> None:
    db = SimpleNamespace(ensure_user=AsyncMock(), log_diet_with_items=AsyncMock())
    message = _message()
    context = _context(
        db,
        args=["lunch", "dal+rice", "650", "P=25.5", "c=80", "F=15.25"],
    )

    result = await diet.diet_command(_update(message), context)

    assert result == ConversationHandler.END
    assert_log_diet(
        db,
        _user().id,
        "lunch",
        "dal, rice",
        650,
        protein_g=25.5,
        carbs_g=80.0,
        fat_g=15.25,
        source=None,
    )
    confirmation = message.reply_text.await_args.args[0]
    assert "P 25.5 g · C 80 g · F 15.25 g" in confirmation
    assert message.reply_text.await_args.kwargs["parse_mode"] == ParseMode.HTML


@pytest.mark.asyncio
async def test_the_shortcut_names_every_missing_nutrient_at_once() -> None:
    """One reply listing all the gaps beats four rounds of trial and error."""
    db = SimpleNamespace(ensure_user=AsyncMock(), log_diet_with_items=AsyncMock())
    message = _message()
    context = _context(db, args=["breakfast", "eggs", "P=30"])

    await diet.diet_command(_update(message), context)

    # Only the "Missing …" line lists the gaps; the rest of the reply is a
    # worked example, which naturally contains every label.
    missing_line = message.reply_text.await_args.args[0].splitlines()[0]
    assert "calories" in missing_line
    assert "c=" in missing_line and "f=" in missing_line
    assert "p=" not in missing_line, "protein was supplied; don't ask for it again"
    db.log_diet_with_items.assert_not_awaited()


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
    db = SimpleNamespace(ensure_user=AsyncMock(), log_diet_with_items=AsyncMock())
    message = _message()
    context = _context(db, args=["lunch", "dal", *macro_tokens])

    result = await diet.diet_command(_update(message), context)

    assert result == ConversationHandler.END
    db.log_diet_with_items.assert_not_awaited()
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
    db = SimpleNamespace(log_diet_with_items=AsyncMock())
    state = {"diet_meal_type": "lunch", "diet_food_items": "dal"}
    context = _context(db, user_data=state)
    message = _message("650")

    result = await diet.receive_calories(_update(message), context)

    assert result == diet.MACROS
    assert context.user_data["diet_food_items"] == "dal"
    assert context.user_data["diet_calories"] == 650
    assert "protein carbs fat" in message.reply_text.await_args.args[0]
    db.log_diet_with_items.assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_is_answered_and_the_calorie_step_is_kept() -> None:
    """``/skip`` is retired, but both users have it in their fingers.

    Left unhandled the command filter would swallow it and the bot would look
    frozen, so it explains itself and stays on the same step.
    """
    db = SimpleNamespace(log_diet_with_items=AsyncMock())
    state = {"diet_meal_type": "lunch", "diet_food_items": "dal"}
    context = _context(db, user_data=state)
    message = _message("/skip")

    handler = diet._skip_retired(diet.CALORIES)
    result = await handler(_update(message), context)

    assert result == diet.CALORIES
    assert context.user_data["diet_food_items"] == "dal"
    assert "diet_calories" not in context.user_data
    db.log_diet_with_items.assert_not_awaited()
    said = message.reply_text.await_args.args[0]
    assert "/skip" in said and "required" in said.lower()


@pytest.mark.asyncio
async def test_a_non_numeric_calorie_answer_re_prompts_without_skip() -> None:
    """The retry guidance must not advertise an escape hatch that no longer exists."""
    db = SimpleNamespace(log_diet_with_items=AsyncMock())
    state = {"diet_meal_type": "lunch", "diet_food_items": "dal"}
    context = _context(db, user_data=state)
    message = _message("lots")

    result = await diet.receive_calories(_update(message), context)

    assert result == diet.CALORIES
    said = message.reply_text.await_args.args[0]
    assert "/skip if unsure" not in said
    db.log_diet_with_items.assert_not_awaited()


@pytest.mark.asyncio
async def test_guided_macros_are_saved_and_loop_is_offered() -> None:
    db = SimpleNamespace(log_diet_with_items=AsyncMock())
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

    # After a save, the flow stays open with a keep-logging prompt.
    assert result == diet.LOG_ANOTHER
    assert_log_diet(
        db,
        _user().id,
        "dinner",
        "tofu & rice",
        700,
        protein_g=40.5,
        carbs_g=90.0,
        fat_g=20.0,
        source=None,
    )
    # This meal's working data is cleared, but the conversation remains active.
    assert "diet_meal_type" not in context.user_data
    assert "diet_food_items" not in context.user_data
    assert "diet_calories" not in context.user_data
    assert active_conversation_flow(context) == "diet"
    # The confirmation is delivered first, then the "log another" prompt.
    assert "tofu &amp; rice" in message.reply_text.await_args_list[0].args[0]
    assert "Log another" in message.reply_text.await_args_list[1].args[0]


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
    db = SimpleNamespace(log_diet_with_items=AsyncMock())
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
    db.log_diet_with_items.assert_not_awaited()
    message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_skip_saves_the_meal_with_no_macros() -> None:
    """``/skip`` is back, at the macro step only.

    It was retired because the fastest path through the flow was also the one
    that produced an untotalable row. What actually happened once it was gone is
    that people typed macros they had estimated, which is the same guess with
    the app's knowledge of it removed. A blank macro is honest; totals keep it
    contagious and the summary says how many values it excluded.
    """
    db = SimpleNamespace(
        log_diet=AsyncMock(return_value=1),
        log_diet_with_items=AsyncMock(return_value=1),
        get_food_preferences=AsyncMock(return_value={}),
    )
    state = {
        "diet_meal_type": "snack",
        "diet_food_items": "apple",
        "diet_calories": 95,
    }
    context = _context(db, user_data=state)
    message = _message("/skip")
    update = _update(message)
    activate_conversation(update, context, "diet")

    await diet.skip_macros(update, context)

    db.log_diet_with_items.assert_awaited()
    items = db.log_diet_with_items.await_args.args[2]
    assert len(items) == 1
    assert items[0]["protein_g"] is None
    assert items[0]["carbs_g"] is None
    assert items[0]["fat_g"] is None
    # Calories are not optional, and the meal still carries them.
    assert items[0]["calories"] == 95


@pytest.mark.asyncio
async def test_skip_is_still_gone_at_the_calorie_step() -> None:
    """Only macros became optional. A meal with no calories is not a record."""
    assert not hasattr(diet, "skip_calories")

    db = SimpleNamespace(log_diet=AsyncMock(), log_diet_with_items=AsyncMock())
    context = _context(db, user_data={"diet_meal_type": "snack", "diet_food_items": "apple"})
    message = _message("/skip")
    update = _update(message)
    activate_conversation(update, context, "diet")

    handler = diet._skip_retired(diet.CALORIES)
    result = await handler(update, context)

    assert result == diet.CALORIES
    db.log_diet.assert_not_awaited()
    db.log_diet_with_items.assert_not_awaited()
    assert active_conversation_flow(context) == "diet"


@pytest.mark.asyncio
async def test_macro_prompt_failure_cleans_unpersisted_conversation() -> None:
    db = SimpleNamespace(log_diet_with_items=AsyncMock())
    state = {"diet_meal_type": "lunch", "diet_food_items": "dal"}
    context = _context(db, user_data=state)
    message = _message("650")
    message.reply_text.side_effect = NetworkError("offline")
    update = _update(message)
    activate_conversation(update, context, "diet")

    result = await diet.receive_calories(update, context)

    assert result == ConversationHandler.END
    assert context.user_data == {}
    db.log_diet_with_items.assert_not_awaited()


@pytest.mark.asyncio
async def test_macro_validation_delivery_failure_cleans_conversation() -> None:
    db = SimpleNamespace(log_diet_with_items=AsyncMock())
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
    db.log_diet_with_items.assert_not_awaited()


@pytest.mark.asyncio
async def test_macro_database_failure_preserves_pending_state() -> None:
    db = SimpleNamespace(log_diet_with_items=AsyncMock(side_effect=RuntimeError("db down")))
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
    db = SimpleNamespace(log_diet_with_items=AsyncMock())
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
    db.log_diet_with_items.assert_awaited_once()
    assert context.user_data == {}
