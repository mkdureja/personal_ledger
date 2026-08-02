"""The tappable, per-set gym flow.

Live use produced two complaints that this covers: the old flow had no buttons
anywhere, and it could only record a workout where every set was identical.

Tests are behavioural — what a user taps and what ends up in the ledger — so the
flow can be reshaped without rewriting them, as long as the guarantees hold:

* an exercise is reachable in two taps, and never typed unless it is new;
* each set keeps its own reps and weight;
* the header stays readable when the sets *are* uniform, and is honestly empty
  when they are not;
* nothing is written until the exercise is finished.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.exercise_seed import MUSCLE_GROUPS, SEED_EXERCISES, seed_rows
from bot.handlers import gym
from bot.handlers.common import activate_conversation

UID = 123456789
OTHER = 987654321


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
def _message():
    message = SimpleNamespace()
    message.reply_text = AsyncMock(return_value=SimpleNamespace(message_id=1))
    message.message_id = 1
    return message


def _update(text: str = "", user_id: int = UID):
    message = _message()
    message.text = text
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id, username="t", first_name="T"),
        effective_chat=SimpleNamespace(id=user_id, type="private"),
    )


def _callback(data: str, user_id: int = UID):
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=_message(),
        edit_message_reply_markup=AsyncMock(),
    )
    return SimpleNamespace(
        callback_query=query,
        effective_message=query.message,
        effective_user=SimpleNamespace(id=user_id, username="t", first_name="T"),
        effective_chat=SimpleNamespace(id=user_id, type="private"),
    )


def _context(db):
    return SimpleNamespace(bot_data={"db": db}, user_data={}, args=[])


def _said(mock_message) -> str:
    return " ".join(
        str(call.args[0]) for call in mock_message.reply_text.call_args_list if call.args
    )


def _labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _last_markup(mock_message):
    for call in reversed(mock_message.reply_text.call_args_list):
        if call.kwargs.get("reply_markup") is not None:
            return call.kwargs["reply_markup"]
    return None


# ---------------------------------------------------------------------------
# The seed list
# ---------------------------------------------------------------------------
class TestSeedList:
    def test_every_group_has_exercises(self):
        assert set(SEED_EXERCISES) == set(MUSCLE_GROUPS)
        for group, names in SEED_EXERCISES.items():
            assert names, f"{group} is empty"

    def test_names_are_unique_across_the_whole_list(self):
        """A duplicate would collide on the shared unique index at seed time."""
        names = [name.casefold() for _group, name in seed_rows()]
        assert len(names) == len(set(names))

    def test_names_fit_the_column(self):
        for _group, name in seed_rows():
            assert len(name) <= gym.MAX_EXERCISE_NAME_LENGTH


# ---------------------------------------------------------------------------
# Picking: group -> exercise
# ---------------------------------------------------------------------------
class TestPicking:
    async def test_opening_the_flow_offers_every_muscle_group(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        update, context = _update(), _context(db)

        state = await gym.gym_command(update, context)

        assert state == gym.EXERCISE
        labels = _labels(_last_markup(update.message))
        for _emoji, name in MUSCLE_GROUPS.values():
            assert any(name in label for label in labels)

    async def test_a_group_tap_lists_its_exercises(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        await db.seed_exercises(seed_rows())
        context = _context(db)
        update = _callback(f"gx_g_{user_id}_chest")

        state = await gym.group_callback(update, context)

        assert state == gym.PICK
        labels = _labels(_last_markup(update.callback_query.message))
        assert "Bench press" in labels
        assert "➕ Add your own" in labels
        assert "⬅️ Back" in labels

    async def test_recent_exercises_are_offered_first(self, db, user_id):
        """Repeat work is the norm, so last session's exercise is one tap."""
        await db.ensure_user(user_id, None, None)
        await db.log_gym_sets(user_id, "Deadlift", [{"reps": 5, "weight_kg": 100.0}])
        update, context = _update(), _context(db)

        await gym.gym_command(update, context)

        labels = _labels(_last_markup(update.message))
        assert labels[0] == "🔁 Deadlift"

    async def test_a_button_stamped_with_another_owner_is_refused(self, db, user_id):
        """Every callback re-validates the owner encoded in its own data.

        (An *unauthorized* user is stopped earlier still, by
        ``authorized_callback``; this is the second line — a button that leaked
        from another chat cannot act on this user's flow.)
        """
        await db.ensure_user(user_id, None, None)
        context = _context(db)
        update = _callback(f"gx_g_{OTHER}_chest")  # owner ≠ tapper

        state = await gym.group_callback(update, context)

        assert state == gym.EXERCISE
        assert update.callback_query.answer.await_args.kwargs["show_alert"] is True
        assert update.callback_query.message.reply_text.await_count == 0


# ---------------------------------------------------------------------------
# Logging sets
# ---------------------------------------------------------------------------
class TestSetEntry:
    @pytest.mark.parametrize(
        ("text", "reps", "weight"),
        [
            ("10 50", 10, 50.0),
            ("10x50", 10, 50.0),
            ("12 42.5", 12, 42.5),
            ("15", 15, None),
        ],
    )
    def test_a_set_line_parses(self, text, reps, weight):
        entry, error = gym._parse_set(text)
        assert error is None
        assert entry == {"reps": reps, "weight_kg": weight}

    @pytest.mark.parametrize("text", ["", "lots", "10 50 60", "0 50", "-3"])
    def test_a_bad_set_line_is_refused(self, text):
        entry, error = gym._parse_set(text)
        assert entry is None and error

    async def test_the_first_set_offers_same_again(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        context = _context(db)
        context.user_data["gym_current_exercise"] = "Chest press"
        update = _update("10 50")

        state = await gym.receive_set(update, context)

        assert state == gym.AFTER_SET
        labels = _labels(_last_markup(update.message))
        assert "🔁 Same again" in labels
        assert "✅ Done with this exercise" in labels
        assert context.user_data["gym_sets"] == [{"reps": 10, "weight_kg": 50.0}]

    async def test_same_again_repeats_the_last_set_exactly(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        context = _context(db)
        context.user_data["gym_current_exercise"] = "Chest press"
        context.user_data["gym_sets"] = [{"reps": 10, "weight_kg": 50.0}]
        update = _callback(f"gx_same_{user_id}")

        state = await gym.after_set_callback(update, context)

        assert state == gym.AFTER_SET
        assert context.user_data["gym_sets"] == [
            {"reps": 10, "weight_kg": 50.0},
            {"reps": 10, "weight_kg": 50.0},
        ]

    async def test_nothing_is_written_until_the_exercise_is_finished(
        self, db, user_id
    ):
        await db.ensure_user(user_id, None, None)
        context = _context(db)
        context.user_data["gym_current_exercise"] = "Chest press"

        await gym.receive_set(_update("10 50"), context)
        await gym.receive_set(_update("8 55"), context)

        assert await db.get_gym_logs(user_id, __import__("datetime").date.today(),
                                     __import__("datetime").date.today()) == []


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------
class TestSaving:
    async def _log(self, db, user_id, context, sets):
        context.user_data["gym_current_exercise"] = "Chest press"
        context.user_data["gym_sets"] = list(sets)
        update = _callback(f"gx_done_{user_id}")
        activate_conversation(update, context, "gym")
        state = await gym.after_set_callback(update, context)
        return state, update

    async def test_varying_sets_are_kept_individually(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        context = _context(db)
        sets = [
            {"reps": 12, "weight_kg": 40.0},
            {"reps": 10, "weight_kg": 45.0},
            {"reps": 8, "weight_kg": 50.0},
        ]

        state, _update_obj = await self._log(db, user_id, context, sets)

        assert state == gym.AFTER_EXERCISE
        cursor = await db.conn.execute(
            "SELECT id, sets, reps, weight_kg, total_volume_kg, total_reps "
            "FROM gym_logs WHERE user_id = ?",
            (user_id,),
        )
        header = dict(await cursor.fetchone())
        assert header["sets"] == 3
        # No single rep count or weight describes this exercise, so the header
        # says nothing rather than something misleading.
        assert header["reps"] is None
        assert header["weight_kg"] is None
        assert header["total_volume_kg"] == pytest.approx(1330.0)
        assert header["total_reps"] == 30

        children = await db.get_gym_sets(user_id, header["id"])
        assert [(c["set_number"], c["reps"], c["weight_kg"]) for c in children] == [
            (1, 12, 40.0),
            (2, 10, 45.0),
            (3, 8, 50.0),
        ]

    async def test_uniform_sets_keep_a_readable_header(self, db, user_id):
        """So ``/recent`` and the daily summary can still say "3×10 @ 50kg"."""
        await db.ensure_user(user_id, None, None)
        context = _context(db)
        sets = [{"reps": 10, "weight_kg": 50.0}] * 3

        await self._log(db, user_id, context, sets)

        cursor = await db.conn.execute(
            "SELECT sets, reps, weight_kg, total_volume_kg FROM gym_logs "
            "WHERE user_id = ?",
            (user_id,),
        )
        header = dict(await cursor.fetchone())
        assert (header["sets"], header["reps"], header["weight_kg"]) == (3, 10, 50.0)
        assert header["total_volume_kg"] == pytest.approx(1500.0)

    async def test_bodyweight_sets_store_reps_and_no_volume(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        context = _context(db)
        sets = [{"reps": 20, "weight_kg": None}, {"reps": 15, "weight_kg": None}]

        await self._log(db, user_id, context, sets)

        cursor = await db.conn.execute(
            "SELECT total_volume_kg, total_reps FROM gym_logs WHERE user_id = ?",
            (user_id,),
        )
        header = dict(await cursor.fetchone())
        assert header["total_volume_kg"] is None
        assert header["total_reps"] == 35

    async def test_finishing_offers_another_exercise_or_done(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        context = _context(db)

        _state, update = await self._log(
            db, user_id, context, [{"reps": 10, "weight_kg": 50.0}]
        )

        labels = _labels(_last_markup(update.callback_query.message))
        assert "➕ Another exercise" in labels
        assert "🏁 Finish workout" in labels

    async def test_the_draft_is_cleared_after_saving(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        context = _context(db)

        await self._log(db, user_id, context, [{"reps": 10, "weight_kg": 50.0}])

        assert "gym_sets" not in context.user_data
        assert "gym_current_exercise" not in context.user_data
        assert len(context.user_data["gym_exercises"]) == 1


# ---------------------------------------------------------------------------
# Adding your own exercise
# ---------------------------------------------------------------------------
class TestCustomExercise:
    async def test_add_your_own_asks_for_a_name(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        context = _context(db)
        update = _callback(f"gx_add_{user_id}_arms")

        state = await gym.exercise_callback(update, context)

        assert state == gym.NEW_NAME
        assert context.user_data["gym_group"] == "arms"

    async def test_a_new_exercise_is_saved_and_logged_immediately(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        context = _context(db)
        context.user_data["gym_group"] = "arms"
        update = _update("Zottman curl")

        state = await gym.receive_new_exercise_name(update, context)

        assert state == gym.SET_INPUT
        rows = await db.list_exercises(user_id, "arms")
        assert "Zottman curl" in [str(r["name"]) for r in rows]
        # Straight into logging it, rather than back to the list.
        assert context.user_data["gym_current_exercise"] == "Zottman curl"

    async def test_it_belongs_to_that_user_only(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        await db.ensure_user(OTHER, None, None)
        await db.add_user_exercise(user_id, "arms", "Zottman curl")

        mine = [str(r["name"]) for r in await db.list_exercises(user_id, "arms")]
        theirs = [str(r["name"]) for r in await db.list_exercises(OTHER, "arms")]

        assert "Zottman curl" in mine
        assert "Zottman curl" not in theirs

    async def test_adding_the_same_name_twice_does_not_duplicate(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        first = await db.add_user_exercise(user_id, "arms", "Zottman curl")
        second = await db.add_user_exercise(user_id, "arms", "  zottman   CURL ")

        assert first["status"] == "added"
        assert second["status"] == "exists"


# ---------------------------------------------------------------------------
# The one-line shortcut still works
# ---------------------------------------------------------------------------
class TestShortcut:
    async def test_it_writes_one_set_per_set(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        context = _context(db)
        context.args = ["Squat", "3", "10", "60"]

        await gym.gym_command(_update(), context)

        cursor = await db.conn.execute(
            "SELECT id, sets, reps, weight_kg FROM gym_logs WHERE user_id = ?",
            (user_id,),
        )
        header = dict(await cursor.fetchone())
        assert (header["sets"], header["reps"], header["weight_kg"]) == (3, 10, 60.0)
        children = await db.get_gym_sets(user_id, header["id"])
        assert len(children) == 3
        assert {c["reps"] for c in children} == {10}

    async def test_an_absurd_set_count_is_refused(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        context = _context(db)
        context.args = ["Squat", str(gym.MAX_SETS_PER_EXERCISE + 1), "10", "60"]
        update = _update()

        await gym.gym_command(update, context)

        assert "more than" in _said(update.message)
        assert await db.get_gym_logs(
            user_id, __import__("datetime").date.today(),
            __import__("datetime").date.today()
        ) == []
