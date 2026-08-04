"""Keeping a typed meal entry as a saved food.

Typing a meal is the escape hatch for everything the ledger does not already
know, and it costs a name plus four numbers *every* time it is used. The offer
this module covers is what turns the second time into one tap. What must hold:

* **What you kept is what you logged.** ``1 portion`` of the new food prices
  exactly the numbers that were just written to the meal — no rounding drift, no
  invented gram weight, and the same again for two portions.
* **It never rewrites a food you already have.** The offer is suppressed for a
  name already in My Foods, and the tap re-checks, because the same button can
  be pressed twice.
* **It belongs to the person who was offered it.** A forged or borrowed payload
  decodes to nothing.
* **It outlives its flow.** The keyboard sits in the scrollback after the diet
  conversation times out, so the button is registered outside every conversation
  and still works.
* **Failing to offer never costs a keyboard.** If the offer cannot be built, the
  user still gets Log another / Done.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User

from bot import keyboards
from bot.database import MAX_ACTIVE_FOODS
from bot.handlers import diet, keep_food

UID = 123456789  # matches conftest ALLOWED_USER_IDS
OTHER = 987654321


def _typed(name: str = "Rajma chawal", **overrides) -> dict:
    """One free-text meal child, as the guided flow assembles it."""
    item = {
        "source_type": "freetext",
        "source_id": None,
        "source_provider": None,
        "source_revision": None,
        "display_name": name,
        "entered_amount": None,
        "entered_unit": None,
        "resolved_base_amount": None,
        "resolved_base_unit": None,
        "calories": 520,
        "protein_g": 18.0,
        "carbs_g": 78.0,
        "fat_g": 12.5,
    }
    item.update(overrides)
    return item


def _structured(name: str = "Banana") -> dict:
    """A child that came from a stored source, so it needs no keeping."""
    return _typed(
        name,
        source_type="catalog",
        source_id=4,
        entered_amount=1.0,
        entered_unit="medium",
    )


# ---------------------------------------------------------------------------
# Which items get offered
# ---------------------------------------------------------------------------
class TestOffers:
    async def test_a_typed_item_is_offered(self, db_with_user, user_id):
        offers = await keep_food.keep_food_offers(
            db_with_user, user_id, [_typed()]
        )

        assert offers == [(0, "Rajma chawal")]

    async def test_an_item_from_a_stored_source_is_not_offered(
        self, db_with_user, user_id
    ):
        """It is already saved somewhere; keeping it again would duplicate it."""
        offers = await keep_food.keep_food_offers(
            db_with_user, user_id, [_structured()]
        )

        assert offers == []

    async def test_a_food_you_already_have_is_not_offered(
        self, db_with_user, user_id
    ):
        await db_with_user.save_food(
            user_id, "Rajma chawal", "g", 100,
            calories=200, protein_g=1, carbs_g=2, fat_g=3,
        )

        offers = await keep_food.keep_food_offers(
            db_with_user, user_id, [_typed()]
        )

        assert offers == []

    async def test_the_match_ignores_case(self, db_with_user, user_id):
        await db_with_user.save_food(
            user_id, "RAJMA CHAWAL", "g", 100,
            calories=200, protein_g=1, carbs_g=2, fat_g=3,
        )

        assert await keep_food.keep_food_offers(
            db_with_user, user_id, [_typed()]
        ) == []

    async def test_another_users_food_does_not_suppress_the_offer(
        self, db_with_user, user_id
    ):
        """Foods are private, so their ledger says nothing about mine."""
        await db_with_user.ensure_user(OTHER, None, None)
        await db_with_user.save_food(
            OTHER, "Rajma chawal", "g", 100,
            calories=200, protein_g=1, carbs_g=2, fat_g=3,
        )

        assert await keep_food.keep_food_offers(
            db_with_user, user_id, [_typed()]
        ) == [(0, "Rajma chawal")]

    async def test_the_position_offered_is_the_position_in_the_meal(
        self, db_with_user, user_id
    ):
        items = [_structured(), _typed("Dal"), _typed("Chapati")]

        offers = await keep_food.keep_food_offers(db_with_user, user_id, items)

        assert offers == [(1, "Dal"), (2, "Chapati")]

    async def test_a_stored_row_uses_its_own_recorded_order(
        self, db_with_user, user_id
    ):
        """Rows read back carry item_order; drafts on the way in do not."""
        rows = [
            {**_typed("Dal"), "item_order": 5},
            {**_typed("Chapati"), "item_order": 9},
        ]

        offers = await keep_food.keep_food_offers(db_with_user, user_id, rows)

        assert offers == [(5, "Dal"), (9, "Chapati")]

    async def test_the_same_name_twice_is_offered_once(
        self, db_with_user, user_id
    ):
        """Two buttons doing one thing, the second reporting failure."""
        items = [_typed("Dal"), _typed("dal")]

        offers = await keep_food.keep_food_offers(db_with_user, user_id, items)

        assert offers == [(0, "Dal")]

    async def test_the_offers_are_bounded(self, db_with_user, user_id):
        items = [_typed(f"Item {index}") for index in range(10)]

        offers = await keep_food.keep_food_offers(db_with_user, user_id, items)

        assert len(offers) == keep_food.MAX_KEEP_OFFERS

    async def test_a_name_too_long_for_a_food_is_not_offered(
        self, db_with_user, user_id
    ):
        """A meal description may be a sentence; a food name may not.

        Offering it would draw a button that fails only once pressed.
        """
        sentence = "x" * 101

        offers = await keep_food.keep_food_offers(
            db_with_user, user_id, [_typed(sentence)]
        )

        assert offers == []

    @pytest.mark.parametrize("field", ["calories", "protein_g", "carbs_g", "fat_g"])
    async def test_an_incomplete_item_is_not_offered(
        self, db_with_user, user_id, field
    ):
        """A saved food is a definition; a hole in it spreads to every log."""
        offers = await keep_food.keep_food_offers(
            db_with_user, user_id, [_typed(**{field: None})]
        )

        assert offers == []


# ---------------------------------------------------------------------------
# What saving actually produces
# ---------------------------------------------------------------------------
def _message():
    return SimpleNamespace(
        text="",
        message_id=11,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=12)),
    )


def _callback(data: str, user_id: int = UID):
    message = _message()
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


def _context(db):
    return SimpleNamespace(bot_data={"db": db}, user_data={}, args=[])


def _texts(message):
    return [call.args[0] for call in message.reply_text.call_args_list]


async def _log_typed_meal(db, user_id, *items) -> int:
    return await db.log_diet_with_items(
        user_id, "lunch", list(items) or [_typed()]
    )


async def _tap(db, user_id, meal_id, item_order=0, tapper=None):
    update = _callback(
        keyboards.keep_food_data(user_id, meal_id, item_order), tapper or user_id
    )
    await keep_food.keep_food_callback(update, _context(db))
    return update


class TestSaving:
    async def test_the_food_prices_exactly_what_was_logged(
        self, db_with_user, user_id
    ):
        """The whole promise: next time costs one tap and the same numbers."""
        meal_id = await _log_typed_meal(db_with_user, user_id)

        await _tap(db_with_user, user_id, meal_id)

        food = await db_with_user.get_food_by_key(user_id, "Rajma chawal")
        entry = await db_with_user.resolve_quantity(
            user_id, "food", food["id"], ["1", keep_food.KEEP_PORTION_NAME]
        )
        item = entry.as_item()
        assert item["calories"] == 520
        assert item["protein_g"] == 18.0
        assert item["carbs_g"] == 78.0
        assert item["fat_g"] == 12.5

    async def test_the_food_is_defined_as_one_helping(
        self, db_with_user, user_id
    ):
        """No gram weight was ever supplied, so none may be invented."""
        meal_id = await _log_typed_meal(db_with_user, user_id)

        await _tap(db_with_user, user_id, meal_id)

        food = await db_with_user.get_food_by_key(user_id, "Rajma chawal")
        assert food["base_unit"] == "piece"
        assert food["basis_amount"] == 1.0

    async def test_more_than_one_helping_scales(self, db_with_user, user_id):
        meal_id = await _log_typed_meal(db_with_user, user_id)

        await _tap(db_with_user, user_id, meal_id)

        food = await db_with_user.get_food_by_key(user_id, "Rajma chawal")
        entry = await db_with_user.resolve_quantity(
            user_id, "food", food["id"], ["2", "portions"]
        )
        assert entry.as_item()["calories"] == 1040

    async def test_the_amount_screen_gets_something_to_tap(
        self, db_with_user, user_id
    ):
        """Without a portion the only route is 'Custom amount' — typing again."""
        meal_id = await _log_typed_meal(db_with_user, user_id)

        await _tap(db_with_user, user_id, meal_id)

        food = await db_with_user.get_food_by_key(user_id, "Rajma chawal")
        portions = await db_with_user.get_food_portions(user_id, food["id"])
        assert [row["name"] for row in portions] == [keep_food.KEEP_PORTION_NAME]

    async def test_one_helping_becomes_the_usual(self, db_with_user, user_id):
        """This is what promotes the food to a one-tap row in the picker."""
        meal_id = await _log_typed_meal(db_with_user, user_id)

        await _tap(db_with_user, user_id, meal_id)

        food = await db_with_user.get_food_by_key(user_id, "Rajma chawal")
        pref = await db_with_user.get_food_preference(user_id, "food", food["id"])
        assert pref["default_amount"] == 1.0
        assert pref["default_unit"] == keep_food.KEEP_PORTION_NAME

    async def test_the_new_food_appears_in_the_picker(
        self, db_with_user, user_id
    ):
        meal_id = await _log_typed_meal(db_with_user, user_id)

        await _tap(db_with_user, user_id, meal_id)

        context = SimpleNamespace(bot_data={"db": db_with_user}, user_data={})
        choices = await diet._ranked_choices(context, user_id, "lunch")
        assert [c["name"] for c in choices] == ["Rajma chawal"]

    async def test_the_meal_itself_is_untouched(self, db_with_user, user_id):
        """Keeping a food is a catalog write, never a correction to history."""
        meal_id = await _log_typed_meal(db_with_user, user_id)
        before = await db_with_user.get_diet_log_items(user_id, meal_id)

        await _tap(db_with_user, user_id, meal_id)

        assert await db_with_user.get_diet_log_items(user_id, meal_id) == before

    async def test_the_confirmation_names_the_food(self, db_with_user, user_id):
        meal_id = await _log_typed_meal(db_with_user, user_id)

        update = await _tap(db_with_user, user_id, meal_id)

        assert "Rajma chawal" in _texts(update.callback_query.message)[0]

    async def test_the_used_row_goes_and_the_rest_stays(
        self, db_with_user, user_id
    ):
        """Log another / Done share the message and must survive the save."""
        meal_id = await _log_typed_meal(
            db_with_user, user_id, _typed("Dal"), _typed("Chapati")
        )

        update = await _tap(db_with_user, user_id, meal_id, item_order=0)

        markup = update.callback_query.edit_message_reply_markup.call_args.kwargs[
            "reply_markup"
        ]
        assert [b.text for row in markup.inline_keyboard for b in row] == [
            "💾 Save “Chapati”",
            "🍽️ Log another",
            "✅ Done",
        ]

    async def test_the_last_row_leaves_the_plain_keyboard(
        self, db_with_user, user_id
    ):
        meal_id = await _log_typed_meal(db_with_user, user_id)

        update = await _tap(db_with_user, user_id, meal_id)

        markup = update.callback_query.edit_message_reply_markup.call_args.kwargs[
            "reply_markup"
        ]
        assert len(markup.inline_keyboard) == 1

    async def test_the_second_item_of_a_meal_can_be_kept(
        self, db_with_user, user_id
    ):
        meal_id = await _log_typed_meal(
            db_with_user, user_id, _typed("Dal"), _typed("Chapati")
        )

        await _tap(db_with_user, user_id, meal_id, item_order=1)

        assert await db_with_user.get_food_by_key(user_id, "Chapati") is not None
        assert await db_with_user.get_food_by_key(user_id, "Dal") is None


class TestRefusals:
    async def test_another_users_button_does_nothing(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        await db.ensure_user(OTHER, None, None)
        meal_id = await _log_typed_meal(db, user_id)

        # OTHER presses a button encoded for user_id.
        update = _callback(keyboards.keep_food_data(user_id, meal_id, 0), OTHER)
        await keep_food.keep_food_callback(update, _context(db))

        assert await db.get_food_by_key(OTHER, "Rajma chawal") is None
        assert await db.get_food_by_key(user_id, "Rajma chawal") is None

    async def test_a_second_press_changes_nothing(self, db_with_user, user_id):
        """The offer stays on screen, so pressing it twice is ordinary."""
        meal_id = await _log_typed_meal(db_with_user, user_id)
        await _tap(db_with_user, user_id, meal_id)
        await db_with_user.save_food(
            user_id, "Rajma chawal", "piece", 1,
            calories=999, protein_g=9, carbs_g=9, fat_g=9,
        )

        update = await _tap(db_with_user, user_id, meal_id)

        food = await db_with_user.get_food_by_key(user_id, "Rajma chawal")
        assert food["calories"] == 999
        assert "already have" in _texts(update.callback_query.message)[0]
        # …and the dead row goes, so a third press has nothing to hit.
        markup = update.callback_query.edit_message_reply_markup.call_args.kwargs[
            "reply_markup"
        ]
        assert len(markup.inline_keyboard) == 1

    async def test_an_undone_meal_saves_nothing(self, db_with_user, user_id):
        meal_id = await _log_typed_meal(db_with_user, user_id)
        await db_with_user.delete_meal_if_recent(user_id, meal_id)

        update = await _tap(db_with_user, user_id, meal_id)

        assert await db_with_user.get_food_by_key(user_id, "Rajma chawal") is None
        update.callback_query.answer.assert_awaited()

    async def test_a_structured_item_is_refused_at_the_tap(
        self, db_with_user, user_id
    ):
        """It is never offered; a forged payload must be refused anyway."""
        meal_id = await _log_typed_meal(db_with_user, user_id, _structured())

        await _tap(db_with_user, user_id, meal_id)

        assert await db_with_user.get_food_by_key(user_id, "Banana") is None

    async def test_a_full_catalog_is_reported_not_swallowed(
        self, db_with_user, user_id, monkeypatch
    ):
        meal_id = await _log_typed_meal(db_with_user, user_id)
        monkeypatch.setattr(
            db_with_user,
            "save_food",
            AsyncMock(return_value={"status": "limit", "food": None, "limit": 500}),
        )

        update = await _tap(db_with_user, user_id, meal_id)

        assert "500" in _texts(update.callback_query.message)[0]
        assert MAX_ACTIVE_FOODS == 500

    async def test_a_name_taken_by_a_gram_based_food_changes_nothing(
        self, db_with_user, user_id
    ):
        """save_food would refuse the unit change; say so instead of failing."""
        meal_id = await _log_typed_meal(db_with_user, user_id)
        monkeypatch_free = await db_with_user.save_food(
            user_id, "Rajma chawal", "g", 100,
            calories=200, protein_g=1, carbs_g=2, fat_g=3,
        )
        assert monkeypatch_free["status"] == "added"

        update = await _tap(db_with_user, user_id, meal_id)

        food = await db_with_user.get_food_by_key(user_id, "Rajma chawal")
        assert food["base_unit"] == "g"
        assert food["calories"] == 200
        assert "already have" in _texts(update.callback_query.message)[0]

    async def test_a_write_failure_is_reported(self, db_with_user, user_id):
        meal_id = await _log_typed_meal(db_with_user, user_id)
        db = SimpleNamespace(
            get_diet_log_items=AsyncMock(
                return_value=await db_with_user.get_diet_log_items(user_id, meal_id)
            ),
            get_food_by_key=AsyncMock(return_value=None),
            save_food=AsyncMock(side_effect=RuntimeError("disk")),
        )
        update = _callback(keyboards.keep_food_data(user_id, meal_id, 0))

        await keep_food.keep_food_callback(update, _context(db))

        assert "Couldn't save" in _texts(update.callback_query.message)[0]

    async def test_a_food_without_its_one_tap_amount_is_still_saved(
        self, db_with_user, user_id
    ):
        """The portion and the usual are conveniences layered on the save."""
        meal_id = await _log_typed_meal(db_with_user, user_id)
        original = db_with_user.save_food_portion

        async def _fail(*args, **kwargs):
            raise ValueError("no room")

        db_with_user.save_food_portion = _fail
        try:
            update = await _tap(db_with_user, user_id, meal_id)
        finally:
            db_with_user.save_food_portion = original

        food = await db_with_user.get_food_by_key(user_id, "Rajma chawal")
        assert food is not None and food["calories"] == 520.0
        assert "1 piece" in _texts(update.callback_query.message)[0]


# ---------------------------------------------------------------------------
# The keyboard and its payloads
# ---------------------------------------------------------------------------
class TestKeyboard:
    def test_the_payload_survives_the_round_trip(self):
        data = keyboards.keep_food_data(UID, 42, 3)

        assert keyboards.parse_keep_food(data, UID) == (42, 3)

    def test_another_users_payload_does_not_decode(self):
        data = keyboards.keep_food_data(UID, 42, 3)

        assert keyboards.parse_keep_food(data, OTHER) is None

    @pytest.mark.parametrize(
        "data",
        [
            "",
            "kf",
            "kf_1_2",
            "kf_1_2_3_4",
            "kf_21i3v9_0_0",  # meal ids start at 1
            "kf_21i3v9_01_0",  # non-canonical base-36
            "kf_21i3v9_A_0",  # uppercase
            "sug_x_123456789_1",
        ],
    )
    def test_a_malformed_payload_is_rejected(self, data):
        assert keyboards.parse_keep_food(data, UID) is None

    def test_the_registered_pattern_matches_what_the_keyboard_emits(self):
        """A pattern that misses its own button makes the offer dead."""
        pattern = re.compile(keep_food.KEEP_FOOD_PATTERN)

        assert pattern.match(keyboards.keep_food_data(UID, 42, 3))

    def test_without_offers_the_keyboard_is_unchanged(self):
        """The habitual pair must not move for a meal with nothing to keep."""
        plain = keyboards.log_another_keyboard(UID)

        assert len(plain.inline_keyboard) == 1
        assert [b.callback_data for b in plain.inline_keyboard[0]] == [
            f"dmore_{UID}_yes",
            f"dmore_{UID}_no",
        ]

    def test_offers_sit_above_log_another(self):
        kb = keyboards.log_another_keyboard(
            UID, meal_id=7, keepable=[(0, "Dal"), (1, "Chapati")]
        )

        assert [b.text for row in kb.inline_keyboard for b in row] == [
            "💾 Save “Dal”",
            "💾 Save “Chapati”",
            "🍽️ Log another",
            "✅ Done",
        ]

    def test_a_long_name_stays_identifiable(self):
        kb = keyboards.log_another_keyboard(
            UID, meal_id=7, keepable=[(0, "x" * 90)]
        )
        label = kb.inline_keyboard[0][0].text

        assert label.endswith("…”")
        assert len(label) < 45


# ---------------------------------------------------------------------------
# How the offer reaches the user
# ---------------------------------------------------------------------------
class TestPostSaveOffer:
    async def test_a_typed_meal_offers_to_keep_it(self, db_with_user, user_id):
        update = _callback("x")
        context = SimpleNamespace(
            bot_data={"db": db_with_user}, user_data={}, args=[]
        )
        meal_id = await _log_typed_meal(db_with_user, user_id)

        state = await diet._offer_log_another(
            update, context, update.effective_message,
            meal_id=meal_id, items=[_typed()],
        )

        assert state == diet.LOG_ANOTHER
        markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].callback_data == (
            keyboards.keep_food_data(user_id, meal_id, 0)
        )

    async def test_a_tapped_meal_offers_nothing_to_keep(
        self, db_with_user, user_id
    ):
        update = _callback("x")
        context = SimpleNamespace(
            bot_data={"db": db_with_user}, user_data={}, args=[]
        )
        meal_id = await _log_typed_meal(db_with_user, user_id, _structured())

        await diet._offer_log_another(
            update, context, update.effective_message,
            meal_id=meal_id, items=[_structured()],
        )

        markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
        assert len(markup.inline_keyboard) == 1

    async def test_a_failed_offer_still_leaves_a_keyboard(self, user_id):
        """Building the offer is a convenience; losing Done would not be."""
        update = _callback("x")
        broken = SimpleNamespace(get_food_by_key=AsyncMock(side_effect=RuntimeError))
        context = SimpleNamespace(bot_data={"db": broken}, user_data={}, args=[])

        state = await diet._offer_log_another(
            update, context, update.effective_message,
            meal_id=1, items=[_typed()],
        )

        assert state == diet.LOG_ANOTHER
        markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
        assert len(markup.inline_keyboard) == 1

    async def test_the_meal_id_reaches_the_offer(self, db_with_user, user_id):
        """A discarded meal id would make every button point at meal 0."""
        update = _callback("x")
        context = SimpleNamespace(
            bot_data={"db": db_with_user},
            user_data={"diet_meal_type": "lunch"},
            args=[],
        )
        update.effective_user = SimpleNamespace(
            id=user_id, username="t", first_name="T"
        )

        await diet._finish_structured_meal(update, context, [_typed()])

        markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
        meal_id, order = keyboards.parse_keep_food(
            markup.inline_keyboard[0][0].callback_data, user_id
        )
        assert order == 0
        assert await db_with_user.get_diet_log_items(user_id, meal_id)


# ---------------------------------------------------------------------------
# Routing, through the real handler table
# ---------------------------------------------------------------------------
_BOT = MagicMock()
_BOT.username = "LedgerTestBot"


def _first_handler(app, update):
    for group in sorted(app.handlers):
        for handler in app.handlers[group]:
            if handler.check_update(update):
                return handler
    return None


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


class TestRouting:
    @pytest.fixture(autouse=True)
    def _reset_conversation(self):
        yield
        diet.diet_conv_handler._conversations.clear()

    def test_the_button_is_handled_outside_any_conversation(self):
        from bot.main import build_application

        app = build_application()
        data = keyboards.keep_food_data(UID, 42, 0)

        assert _first_handler(app, _callback_update(UID, data)) is (
            keep_food.keep_food_handler
        )

    def test_it_still_reaches_the_handler_during_a_live_diet_flow(self):
        """The keyboard's other buttons belong to the flow; this one does not."""
        from bot.main import build_application

        app = build_application()
        key = (UID, UID)
        diet.diet_conv_handler._conversations[key] = diet.LOG_ANOTHER
        data = keyboards.keep_food_data(UID, 42, 0)

        assert _first_handler(app, _callback_update(UID, data)) is (
            keep_food.keep_food_handler
        )

    def test_log_another_still_belongs_to_the_conversation(self):
        """The regression the new row could cause: stealing the habitual tap."""
        from bot.main import build_application

        app = build_application()
        key = (UID, UID)
        diet.diet_conv_handler._conversations[key] = diet.LOG_ANOTHER

        handler = _first_handler(app, _callback_update(UID, f"dmore_{UID}_yes"))

        assert handler is diet.diet_conv_handler
