"""``/suggest``: the one thing a user writes down about the app itself.

The guarantees worth protecting, in the order they can break:

* **Everything after the command is the suggestion.** No word is quietly
  reserved as a subcommand, and the text is stored as typed — a capture command
  that edits or refuses one phrasing is worse than one with no shortcuts.
* **A suggestion is not ledger data.** It joins no total, no chart, and no
  undo; filing one must leave the day's records exactly as they were.
* **It belongs to the person who sent it.** Nobody reads or withdraws another
  user's, and the withdraw button proves it against a forged payload.
* **A restart never files the same idea twice**, for the same reason a
  redelivered meal never double-logs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User
from telegram.ext import ConversationHandler

from bot import keyboards
from bot.database import MAX_SUGGESTION_LENGTH, MutationSource
from bot.handlers import suggest

UID = 123456789  # matches conftest ALLOWED_USER_IDS
OTHER = 987654321


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
class TestStorage:
    async def test_a_suggestion_is_stored_as_it_was_written(self, db, user_id):
        """Only the outer whitespace goes; the words are somebody's opinion."""
        await db.ensure_user(user_id, None, None)

        await db.add_app_suggestion(user_id, "  Let me log Water,\nplease.  ")

        rows = await db.get_app_suggestions(user_id)
        assert rows[0]["suggestion"] == "Let me log Water,\nplease."

    async def test_the_newest_suggestion_comes_first(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        for text in ("first", "second", "third"):
            await db.add_app_suggestion(user_id, text)

        rows = await db.get_app_suggestions(user_id)

        assert [row["suggestion"] for row in rows] == ["third", "second", "first"]

    async def test_a_read_is_bounded(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        for index in range(5):
            await db.add_app_suggestion(user_id, f"idea {index}")

        assert len(await db.get_app_suggestions(user_id, limit=2)) == 2

    async def test_one_user_never_reads_the_others(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        await db.ensure_user(OTHER, None, None)
        await db.add_app_suggestion(user_id, "mine")
        await db.add_app_suggestion(OTHER, "theirs")

        assert [row["suggestion"] for row in await db.get_app_suggestions(user_id)] == [
            "mine"
        ]
        assert await db.count_app_suggestions(user_id) == 1

    async def test_the_maintainers_read_spans_both_users(self, db, user_id):
        """The one caller with no owner to scope to — and it is not a handler."""
        await db.ensure_user(user_id, None, None)
        await db.ensure_user(OTHER, None, None)
        await db.add_app_suggestion(user_id, "mine")
        await db.add_app_suggestion(OTHER, "theirs")

        rows = await db.get_app_suggestions()

        assert {row["suggestion"] for row in rows} == {"mine", "theirs"}

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
    async def test_an_empty_suggestion_is_refused(self, db, user_id, blank):
        await db.ensure_user(user_id, None, None)

        with pytest.raises(ValueError):
            await db.add_app_suggestion(user_id, blank)

        assert await db.get_app_suggestions(user_id) == []

    async def test_an_over_long_suggestion_is_refused_by_the_write_path(
        self, db, user_id
    ):
        """The handler checks first; this is the backstop that makes it true."""
        await db.ensure_user(user_id, None, None)

        with pytest.raises(ValueError):
            await db.add_app_suggestion(user_id, "x" * (MAX_SUGGESTION_LENGTH + 1))

        assert await db.get_app_suggestions(user_id) == []

    async def test_the_limit_itself_is_accepted(self, db, user_id):
        await db.ensure_user(user_id, None, None)

        await db.add_app_suggestion(user_id, "x" * MAX_SUGGESTION_LENGTH)

        assert len(await db.get_app_suggestions(user_id)) == 1

    async def test_withdrawing_is_scoped_to_the_owner(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        await db.ensure_user(OTHER, None, None)
        suggestion_id = await db.add_app_suggestion(OTHER, "theirs")

        assert await db.delete_app_suggestion(user_id, suggestion_id) is False
        assert len(await db.get_app_suggestions(OTHER)) == 1

    async def test_withdrawing_twice_removes_one_row(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        suggestion_id = await db.add_app_suggestion(user_id, "mine")

        assert await db.delete_app_suggestion(user_id, suggestion_id) is True
        assert await db.delete_app_suggestion(user_id, suggestion_id) is False
        assert await db.get_app_suggestions(user_id) == []

    async def test_a_redelivered_update_files_one_suggestion(self, db, user_id):
        """A restart replays updates; the same idea must not arrive twice."""
        await db.ensure_user(user_id, None, None)
        source = MutationSource(update_id=42, chat_id=user_id, message_id=7)

        first = await db.add_app_suggestion(user_id, "let me log water", source=source)
        second = await db.add_app_suggestion(user_id, "let me log water", source=source)

        assert first == second
        assert len(await db.get_app_suggestions(user_id)) == 1

    async def test_a_suggestion_is_not_part_of_the_ledger(self, db, user_id):
        """It must never surface as something the user *did* that day."""
        await db.ensure_user(user_id, None, None)

        await db.add_app_suggestion(user_id, "add a dark mode")

        assert await db.get_recent_entries(user_id, 10) == []


async def test_migrating_a_populated_v13_database_adds_suggestions_and_keeps_rows(
    monkeypatch,
):
    """The exact step the deployed database will take on next startup."""
    from bot import migrations
    from bot.config import today_local
    from bot.database import DatabaseManager

    mgr = DatabaseManager(":memory:")
    await mgr.connect()
    try:
        with monkeypatch.context() as patched:
            patched.setattr(migrations, "LATEST_VERSION", 13)
            await migrations.run_migrations(mgr.conn)
        assert await migrations.get_user_version(mgr.conn) == 13

        await mgr.ensure_user(UID, "t", "Test")
        await mgr.log_weight(UID, 72.4, today_local())
        habit_id, _ = await mgr.add_habit(UID, "Read")
        await mgr.check_habit(UID, habit_id, today_local())

        with monkeypatch.context() as patched:
            patched.setattr(migrations, "LATEST_VERSION", 14)
            await migrations.run_migrations(mgr.conn)

        assert await migrations.get_user_version(mgr.conn) == 14
        cursor = await mgr.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        assert "app_suggestions" in {row["name"] for row in await cursor.fetchall()}

        # Pre-existing data is untouched and the new table starts empty.
        assert await mgr.get_weight_on(UID, today_local()) == 72.4
        assert await mgr.get_checked_habits(UID, today_local()) == {habit_id}
        assert await mgr.get_app_suggestions(UID) == []

        cursor = await mgr.conn.execute("PRAGMA foreign_key_check")
        assert await cursor.fetchall() == []
    finally:
        await mgr.close()


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------
def _message(text: str = ""):
    return SimpleNamespace(
        text=text,
        message_id=11,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=12)),
    )


def _update(text: str = "", user_id: int = UID, *, update_id: int | None = None):
    message = _message(text)
    return SimpleNamespace(
        update_id=update_id,
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id, username="t", first_name="T"),
        effective_chat=SimpleNamespace(id=user_id, type="private"),
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


def _last_markup(message):
    for call in reversed(message.reply_text.call_args_list):
        if call.kwargs.get("reply_markup") is not None:
            return call.kwargs["reply_markup"]
    return None


class TestFlow:
    async def test_the_one_shot_form_files_it_without_opening_a_flow(
        self, db, user_id
    ):
        """A complete thought needs no follow-up state to get stuck in."""
        update = _update("/suggest let me log water", user_id)
        context = _context(db)

        state = await suggest.suggest_command(update, context)

        assert state == ConversationHandler.END
        assert "_ledger_active_conversation" not in context.user_data
        rows = await db.get_app_suggestions(user_id)
        assert [row["suggestion"] for row in rows] == ["let me log water"]

    async def test_the_text_is_filed_as_typed(self, db, user_id):
        """Casing, punctuation, and line breaks all survive the trip."""
        update = _update("/suggest Two things:\n- water\n- a Dark mode!", user_id)

        await suggest.suggest_command(update, _context(db))

        rows = await db.get_app_suggestions(user_id)
        assert rows[0]["suggestion"] == "Two things:\n- water\n- a Dark mode!"

    @pytest.mark.parametrize("word", ["list", "help", "on", "reset"])
    async def test_no_word_is_reserved_as_a_subcommand(self, db, user_id, word):
        """The whole promise: text after /suggest is a suggestion, always."""
        update = _update(f"/suggest {word}", user_id)

        await suggest.suggest_command(update, _context(db))

        rows = await db.get_app_suggestions(user_id)
        assert [row["suggestion"] for row in rows] == [word]

    async def test_a_bare_command_asks_and_owns_the_flow(self, db, user_id):
        """The marker must be set, or Home's guards cannot see this flow."""
        update = _update("/suggest", user_id)
        context = _context(db)

        state = await suggest.suggest_command(update, context)

        assert state == suggest.SAY
        assert context.user_data["_ledger_active_conversation"] == ("suggest", user_id)
        assert "Suggest something" in _texts(update.effective_message)[0]
        assert await db.get_app_suggestions(user_id) == []

    async def test_a_first_ever_prompt_shows_no_history_block(self, db, user_id):
        """Nothing sent yet is an empty list, not a heading over one."""
        await db.ensure_user(user_id, None, None)
        update = _update("/suggest", user_id)

        await suggest.suggest_command(update, _context(db))

        text = _texts(update.effective_message)[0]
        assert "•" not in text and "suggested" not in text

    async def test_the_prompt_shows_what_this_user_already_sent(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        await db.add_app_suggestion(user_id, "let me log water")
        update = _update("/suggest", user_id)

        await suggest.suggest_command(update, _context(db))

        assert "let me log water" in _texts(update.effective_message)[0]

    async def test_the_prompt_never_shows_the_other_users_ideas(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        await db.ensure_user(OTHER, None, None)
        await db.add_app_suggestion(OTHER, "their private thought")
        update = _update("/suggest", user_id)

        await suggest.suggest_command(update, _context(db))

        assert "their private thought" not in _texts(update.effective_message)[0]

    async def test_the_answer_to_the_prompt_is_filed(self, db, user_id):
        context = _context(db)
        await suggest.suggest_command(_update("/suggest", user_id), context)
        update = _update("gym needs a rest timer", user_id)

        state = await suggest.receive_suggestion(update, context)

        assert state == ConversationHandler.END
        rows = await db.get_app_suggestions(user_id)
        assert [row["suggestion"] for row in rows] == ["gym needs a rest timer"]

    async def test_filing_releases_the_flow_marker(self, db, user_id):
        """A marker left behind would make the next ordinary message look stuck."""
        context = _context(db)
        await suggest.suggest_command(_update("/suggest", user_id), context)

        await suggest.receive_suggestion(_update("a rest timer", user_id), context)

        assert "_ledger_active_conversation" not in context.user_data

    async def test_an_over_long_suggestion_keeps_the_prompt_alive(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        update = _update("x" * (MAX_SUGGESTION_LENGTH + 1), user_id)

        state = await suggest.receive_suggestion(update, _context(db))

        assert state == suggest.SAY
        assert "characters" in _texts(update.effective_message)[0]
        assert await db.get_app_suggestions(user_id) == []

    async def test_an_over_long_one_shot_ends_rather_than_half_starting(
        self, db, user_id
    ):
        """Returning SAY here would leave PTB in a state nothing else believes in."""
        update = _update("/suggest " + "x" * (MAX_SUGGESTION_LENGTH + 1), user_id)
        context = _context(db)

        state = await suggest.suggest_command(update, context)

        assert state == ConversationHandler.END
        assert "_ledger_active_conversation" not in context.user_data
        assert await db.get_app_suggestions(user_id) == []

    async def test_a_blank_answer_asks_again_instead_of_saving_nothing(
        self, db, user_id
    ):
        await db.ensure_user(user_id, None, None)
        update = _update("   ", user_id)

        state = await suggest.receive_suggestion(update, _context(db))

        assert state == suggest.SAY
        assert await db.get_app_suggestions(user_id) == []

    async def test_a_failed_write_says_so_rather_than_confirming(
        self, db, user_id, monkeypatch
    ):
        await db.ensure_user(user_id, None, None)
        monkeypatch.setattr(
            db, "add_app_suggestion", AsyncMock(side_effect=RuntimeError("disk"))
        )
        update = _update("something", user_id)

        state = await suggest.receive_suggestion(update, _context(db))

        assert state == suggest.SAY
        assert "Couldn't save" in _texts(update.effective_message)[0]

    async def test_a_redelivered_command_files_one_suggestion(self, db, user_id):
        """End to end: the same update twice, as a restart would deliver it."""
        context = _context(db)

        await suggest.suggest_command(
            _update("/suggest let me log water", user_id, update_id=99), context
        )
        await suggest.suggest_command(
            _update("/suggest let me log water", user_id, update_id=99), context
        )

        assert len(await db.get_app_suggestions(user_id)) == 1


class TestWithdrawing:
    async def test_the_receipt_offers_to_withdraw(self, db, user_id):
        update = _update("/suggest let me log water", user_id)

        await suggest.suggest_command(update, _context(db))

        markup = _last_markup(update.effective_message)
        assert markup.inline_keyboard[0][0].text == "🗑 Withdraw"

    async def test_withdrawing_removes_the_suggestion(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        suggestion_id = await db.add_app_suggestion(user_id, "let me log water")
        update = _callback(
            keyboards.app_suggestion_remove_data(user_id, suggestion_id), user_id
        )

        await suggest.withdraw_suggestion_callback(update, _context(db))

        assert await db.get_app_suggestions(user_id) == []
        assert "withdrawn" in _texts(update.callback_query.message)[0].lower()

    async def test_a_button_stamped_with_another_owner_removes_nothing(
        self, db, user_id
    ):
        await db.ensure_user(user_id, None, None)
        await db.ensure_user(OTHER, None, None)
        suggestion_id = await db.add_app_suggestion(OTHER, "theirs")
        update = _callback(
            keyboards.app_suggestion_remove_data(OTHER, suggestion_id), user_id
        )

        await suggest.withdraw_suggestion_callback(update, _context(db))

        assert update.callback_query.answer.await_args.kwargs["show_alert"] is True
        assert len(await db.get_app_suggestions(OTHER)) == 1

    async def test_withdrawing_an_already_gone_suggestion_says_so(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        suggestion_id = await db.add_app_suggestion(user_id, "mine")
        await db.delete_app_suggestion(user_id, suggestion_id)
        update = _callback(
            keyboards.app_suggestion_remove_data(user_id, suggestion_id), user_id
        )

        await suggest.withdraw_suggestion_callback(update, _context(db))

        assert "already gone" in _texts(update.callback_query.message)[0]


class TestCallbackEncoding:
    def test_a_suggestion_id_survives_the_round_trip(self):
        data = keyboards.app_suggestion_remove_data(UID, 7)

        assert keyboards.parse_app_suggestion_remove(data, UID) == 7

    def test_another_users_button_does_not_decode(self):
        data = keyboards.app_suggestion_remove_data(UID, 7)

        assert keyboards.parse_app_suggestion_remove(data, OTHER) is None

    @pytest.mark.parametrize(
        "data", ["", "sug", "sug_x_1", "sug_x_abc_1", "sug_x_1_zero", "wt_v_1_100"]
    )
    def test_a_malformed_payload_is_rejected(self, data):
        assert keyboards.parse_app_suggestion_remove(data, UID) is None

    def test_the_registered_pattern_matches_what_the_keyboard_emits(self):
        """A pattern that misses its own button would make Withdraw dead."""
        import re

        pattern = re.compile(suggest._PATTERN)
        assert pattern.match(keyboards.app_suggestion_remove_data(UID, 7))


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
        suggest.suggest_conv_handler._conversations.clear()

    @pytest.mark.parametrize("text", ["/suggest", "/suggest let me log water"])
    def test_the_command_reaches_the_suggest_conversation(self, text):
        from bot.main import build_application

        app = build_application()

        assert (
            _first_handler(app, _command_update(UID, text))
            is suggest.suggest_conv_handler
        )

    def test_the_withdraw_button_is_handled_outside_any_conversation(self):
        """It lives on a receipt that outlives the flow that produced it."""
        from bot.main import build_application

        app = build_application()
        data = keyboards.app_suggestion_remove_data(UID, 7)

        handler = _first_handler(app, _callback_update(UID, data))

        assert handler is suggest.withdraw_suggestion_handler

    def test_an_outsider_cannot_reach_the_command(self):
        from bot.main import build_application

        app = build_application()

        handler = _first_handler(app, _command_update(555000111, "/suggest hello"))

        assert handler is not suggest.suggest_conv_handler


async def test_help_names_the_command(db, user_id):
    """A capture command nobody knows about captures nothing."""
    from bot.handlers.start import help_command

    update = _update("/help", user_id)

    await help_command(update, _context(db))

    assert "/suggest " in _texts(update.effective_message)[0]
