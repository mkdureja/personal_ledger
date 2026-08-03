"""Daily weight: one number a day, gaps carried forward, a trend you can read.

The guarantees worth protecting, in the order they can break:

* **A day holds one weight.** Weighing twice corrects the day; it never leaves
  two answers for a chart to choose between.
* **A gap is carried, but only so far.** Ten days of fill is a convenience;
  filling forever would draw a flat line across a silence, and a flat line reads
  as "weight held steady" — a claim about a body, not about a record.
* **A carried day is never mistaken for a measured one.** Every consumer can
  tell them apart, and the chart draws them differently.
* **The trend compares averages, never two individual days**, because
  day-to-day weight moves on water and timing.
"""

from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import ConversationHandler

from bot import charts, keyboards, weight_series
from bot.config import today_local
from bot.handlers import weight

UID = 123456789
OTHER = 987654321
DAY = date(2026, 7, 1)


def _days(start: date, count: int) -> list[date]:
    return [start + timedelta(days=index) for index in range(count)]


# ---------------------------------------------------------------------------
# The fill rule (pure)
# ---------------------------------------------------------------------------
class TestSeries:
    def test_a_measured_day_is_marked_measured(self):
        series = weight_series.daily_series([(DAY, 72.4)], DAY, DAY)

        assert len(series) == 1
        assert series[0].weight_kg == 72.4
        assert series[0].measured is True
        assert series[0].carried is False
        assert series[0].carried_from is None

    def test_a_gap_carries_the_last_measurement_and_says_so(self):
        series = weight_series.daily_series(
            [(DAY, 72.4)], DAY, DAY + timedelta(days=2)
        )

        assert [point.weight_kg for point in series] == [72.4, 72.4, 72.4]
        assert [point.measured for point in series] == [True, False, False]
        assert series[2].carried_from == DAY

    def test_the_carry_stops_exactly_at_the_limit(self):
        """Day+10 is still filled; day+11 is not. The boundary is the feature."""
        limit = weight_series.CARRY_LIMIT_DAYS
        series = weight_series.daily_series(
            [(DAY, 72.4)], DAY, DAY + timedelta(days=limit + 1)
        )

        assert series[limit].weight_kg == 72.4
        assert series[limit].carried is True
        assert series[limit + 1].weight_kg is None

    def test_a_long_silence_does_not_render_as_a_plateau(self):
        """The reason the cap exists, asserted as the behaviour it prevents."""
        start = DAY + timedelta(days=20)
        series = weight_series.daily_series(
            [(DAY, 72.4)], start, start + timedelta(days=6)
        )

        assert all(point.weight_kg is None for point in series)

    def test_a_measurement_before_the_window_still_seeds_it(self):
        """Otherwise the left edge would depend on where the window was cut."""
        start = DAY + timedelta(days=2)
        series = weight_series.daily_series(
            [(DAY, 72.4)], start, start + timedelta(days=1)
        )

        assert [point.weight_kg for point in series] == [72.4, 72.4]
        assert all(point.carried_from == DAY for point in series)

    def test_days_before_the_first_weigh_in_hold_nothing(self):
        series = weight_series.daily_series(
            [(DAY + timedelta(days=2), 72.4)], DAY, DAY + timedelta(days=2)
        )

        assert [point.weight_kg for point in series] == [None, None, 72.4]

    def test_a_new_measurement_replaces_the_carried_value(self):
        series = weight_series.daily_series(
            [(DAY, 72.4), (DAY + timedelta(days=2), 71.0)],
            DAY,
            DAY + timedelta(days=3),
        )

        assert [point.weight_kg for point in series] == [72.4, 72.4, 71.0, 71.0]
        assert series[3].carried_from == DAY + timedelta(days=2)

    def test_entries_need_not_be_sorted(self):
        days = _days(DAY, 3)
        series = weight_series.daily_series(
            [(days[2], 71.0), (days[0], 72.4)], days[0], days[2]
        )

        assert [point.weight_kg for point in series] == [72.4, 72.4, 71.0]

    def test_an_inverted_window_is_empty_rather_than_an_error(self):
        assert weight_series.daily_series([(DAY, 72.4)], DAY, DAY - timedelta(days=1)) == []

    @pytest.mark.parametrize("bad", [0, -5])
    def test_a_nonsense_stored_value_is_ignored_not_plotted(self, bad):
        series = weight_series.daily_series([(DAY, bad)], DAY, DAY)

        assert series[0].weight_kg is None


class TestRollingAverage:
    def test_it_averages_the_trailing_window(self):
        days = _days(DAY, 3)
        series = weight_series.daily_series(
            [(days[0], 70.0), (days[1], 71.0), (days[2], 72.0)], days[0], days[2]
        )

        assert weight_series.rolling_average(series, window=3) == [
            70.0,
            70.5,
            71.0,
        ]

    def test_early_days_average_what_exists_rather_than_waiting(self):
        """A line that starts a week late looks like missing data."""
        days = _days(DAY, 2)
        series = weight_series.daily_series(
            [(days[0], 70.0), (days[1], 72.0)], days[0], days[1]
        )

        assert weight_series.rolling_average(series, window=7) == [70.0, 71.0]

    def test_the_line_breaks_where_the_record_breaks(self):
        limit = weight_series.CARRY_LIMIT_DAYS
        series = weight_series.daily_series(
            [(DAY, 72.0)], DAY, DAY + timedelta(days=limit + 2)
        )

        averages = weight_series.rolling_average(series)
        assert averages[limit] is not None
        assert averages[limit + 1] is None
        assert averages[limit + 2] is None

    def test_a_zero_window_is_refused(self):
        with pytest.raises(ValueError):
            weight_series.rolling_average([], window=0)


class TestSummary:
    def test_the_change_compares_windows_not_days(self):
        """Two weeks flat then a jump: the day-to-day delta would say +2.0."""
        days = _days(DAY, 14)
        entries = [(day, 70.0) for day in days[:7]] + [(day, 72.0) for day in days[7:]]
        series = weight_series.daily_series(entries, days[0], days[13])

        trend = weight_series.summarize(series, window=7)

        assert trend.average_kg == 72.0
        assert trend.previous_average_kg == 70.0
        assert trend.change_kg == 2.0

    def test_measured_days_counts_only_real_weigh_ins(self):
        series = weight_series.daily_series(
            [(DAY, 72.0)], DAY, DAY + timedelta(days=4)
        )

        trend = weight_series.summarize(series)

        assert trend.measured_days == 1
        assert trend.latest_kg == 72.0
        assert trend.latest_measured is False  # the latest point is carried

    def test_a_single_weigh_in_has_no_change(self):
        series = weight_series.daily_series([(DAY, 72.0)], DAY, DAY)

        trend = weight_series.summarize(series)

        assert trend.change_kg is None

    def test_an_empty_record_summarizes_to_nothing(self):
        trend = weight_series.summarize(
            weight_series.daily_series([], DAY, DAY + timedelta(days=6))
        )

        assert trend.latest_kg is None
        assert trend.average_kg is None
        assert trend.change_kg is None


class TestFormatting:
    @pytest.mark.parametrize(
        "value,expected",
        [(72.0, "72"), (72.4, "72.4"), (72.35, "72.35"), (None, "—")],
    )
    def test_kg_drops_trailing_zeros(self, value, expected):
        assert weight_series.format_kg(value) == expected

    def test_a_change_always_carries_its_direction(self):
        assert weight_series.format_change(0.3).startswith("+")
        assert weight_series.format_change(-0.3).startswith("−")
        assert weight_series.format_change(0.0) == "no change"
        assert weight_series.format_change(None) == "—"


class TestNudgeGrid:
    def test_it_is_anchored_on_the_step_not_the_raw_value(self):
        """A typed 72.35 still offers the clean row a scale would show."""
        assert weight_series.nudge_values(72.35) == [72.1, 72.2, 72.3, 72.4, 72.5]

    def test_it_centres_on_the_last_weight(self):
        assert weight_series.nudge_values(72.4)[2] == 72.4

    def test_there_is_nothing_to_nudge_from_without_a_last_weight(self):
        assert weight_series.nudge_values(None) == []

    def test_values_outside_the_accepted_range_are_dropped_not_clamped(self):
        """Two buttons logging the same weight would be a tap that lies."""
        values = weight_series.nudge_values(weight_series.MIN_WEIGHT_KG)

        assert all(value >= weight_series.MIN_WEIGHT_KG for value in values)
        assert len(values) == len(set(values))


def test_the_series_module_stays_pure():
    """The fill rule is only useful if it cannot drift into a handler."""
    tree = ast.parse(Path(weight_series.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
            if node.level and not node.module:
                imported.update(alias.name for alias in node.names)

    forbidden = imported & {
        "telegram", "aiosqlite", "sqlite3", "matplotlib", "numpy",
        "config", "database", "handlers", "keyboards", "charts", "main",
    }
    assert not forbidden, f"weight_series must stay pure; found {forbidden}"
    assert not any(
        isinstance(node, (ast.AsyncFunctionDef, ast.Await))
        for node in ast.walk(tree)
    ), "weight_series must stay synchronous"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
class TestStorage:
    async def test_a_day_holds_one_weight(self, db, user_id):
        await db.ensure_user(user_id, None, None)

        first = await db.log_weight(user_id, 72.4, DAY)
        second = await db.log_weight(user_id, 71.9, DAY)

        assert first.replaced is False and first.previous_kg is None
        assert second.replaced is True and second.previous_kg == 72.4
        assert second.change_kg == -0.5
        assert await db.get_weight_on(user_id, DAY) == 71.9
        assert len(await db.get_weight_logs(user_id, DAY, DAY)) == 1

    async def test_a_reading_is_stored_at_scale_precision(self, db, user_id):
        await db.ensure_user(user_id, None, None)

        result = await db.log_weight(user_id, 72.3456, DAY)

        assert result.weight_kg == 72.35

    @pytest.mark.parametrize("bad", [0, -5, 1000, float("nan"), float("inf")])
    async def test_an_implausible_weight_is_refused(self, db, user_id, bad):
        await db.ensure_user(user_id, None, None)

        with pytest.raises(ValueError):
            await db.log_weight(user_id, bad, DAY)

        assert await db.get_weight_on(user_id, DAY) is None

    async def test_weights_are_per_user(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        await db.ensure_user(OTHER, None, None)
        await db.log_weight(user_id, 72.4, DAY)

        assert await db.get_weight_on(OTHER, DAY) is None
        assert await db.get_latest_weight(OTHER) is None

    async def test_the_latest_weigh_in_may_be_long_past(self, db, user_id):
        """The nudge row is built from it, so "last" must not mean "recent"."""
        await db.ensure_user(user_id, None, None)
        await db.log_weight(user_id, 72.4, DAY)

        latest = await db.get_latest_weight(user_id)

        assert latest == {"log_date": DAY, "weight_kg": 72.4}

    async def test_a_range_read_is_ordered_and_bounded(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        days = _days(DAY, 4)
        for index, day in enumerate(days):
            await db.log_weight(user_id, 70.0 + index, day)

        rows = await db.get_weight_logs(user_id, days[1], days[2])

        assert [row["log_date"] for row in rows] == [days[1], days[2]]
        assert [row["weight_kg"] for row in rows] == [71.0, 72.0]

    async def test_clearing_a_day_leaves_nothing_to_carry_a_wrong_number(
        self, db, user_id
    ):
        await db.ensure_user(user_id, None, None)
        await db.log_weight(user_id, 72.4, DAY)

        assert await db.delete_weight(user_id, DAY) is True
        assert await db.delete_weight(user_id, DAY) is False
        assert await db.get_weight_on(user_id, DAY) is None


async def test_migrating_a_populated_v12_database_adds_weight_and_keeps_rows(
    monkeypatch,
):
    """The exact step the deployed database will take on next startup."""
    from bot import migrations
    from bot.database import DatabaseManager

    mgr = DatabaseManager(":memory:")
    await mgr.connect()
    try:
        with monkeypatch.context() as patched:
            patched.setattr(migrations, "LATEST_VERSION", 12)
            await migrations.run_migrations(mgr.conn)
        assert await migrations.get_user_version(mgr.conn) == 12

        await mgr.ensure_user(UID, "t", "Test")
        habit_id, _ = await mgr.add_habit(UID, "Read")
        await mgr.check_habit(UID, habit_id, today_local())

        with monkeypatch.context() as patched:
            patched.setattr(migrations, "LATEST_VERSION", 13)
            await migrations.run_migrations(mgr.conn)

        assert await migrations.get_user_version(mgr.conn) == 13
        cursor = await mgr.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        assert "weight_logs" in {row["name"] for row in await cursor.fetchall()}

        # Pre-existing data is untouched and the new section starts empty.
        assert await mgr.get_checked_habits(UID, today_local()) == {habit_id}
        assert await mgr.get_latest_weight(UID) is None

        cursor = await mgr.conn.execute("PRAGMA foreign_key_check")
        assert await cursor.fetchall() == []
    finally:
        await mgr.close()


async def test_one_row_per_day_is_enforced_by_the_schema_not_the_handler(db, user_id):
    """A second row for the same day must be impossible, not merely unwritten."""
    import aiosqlite

    await db.ensure_user(user_id, None, None)
    await db.log_weight(user_id, 72.4, DAY)

    with pytest.raises(aiosqlite.IntegrityError):
        await db.conn.execute(
            "INSERT INTO weight_logs (user_id, log_date, weight_kg) VALUES (?, ?, ?)",
            (user_id, DAY.isoformat(), 71.0),
        )


# ---------------------------------------------------------------------------
# Typed input
# ---------------------------------------------------------------------------
class TestParsing:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("72.4", 72.4),
            ("72", 72.0),
            ("72,4", 72.4),
            ("72.4 kg", 72.4),
            ("72.4kg", 72.4),
            ("  72.4  ", 72.4),
            ("72.4 KGS", 72.4),
            ("72.4 kilograms", 72.4),
        ],
    )
    def test_it_reads_what_people_actually_send(self, text, expected):
        assert weight.parse_weight_text(text) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "text", ["", "   ", "seventy two", "72.4.5", "abc", "kg", "1,234.5", "nan", "inf"]
    )
    def test_anything_that_is_not_a_number_is_refused(self, text):
        assert weight.parse_weight_text(text) is None


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------
def _message():
    return SimpleNamespace(
        text="", reply_text=AsyncMock(return_value=SimpleNamespace(message_id=1))
    )


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


def _context(db, args=None):
    return SimpleNamespace(bot_data={"db": db}, user_data={}, args=args or [])


def _texts(message):
    return [call.args[0] for call in message.reply_text.call_args_list]


def _last_markup(message):
    for call in reversed(message.reply_text.call_args_list):
        if call.kwargs.get("reply_markup") is not None:
            return call.kwargs["reply_markup"]
    return None


class TestFlow:
    async def test_the_quick_form_logs_without_opening_a_flow(self, db, user_id):
        """"/weight 72.4" is a complete instruction; state would only get stuck."""
        update = _update(user_id=user_id)
        context = _context(db, ["72.4"])

        state = await weight.weight_command(update, context)

        assert state == ConversationHandler.END
        assert await db.get_weight_on(user_id, today_local()) == 72.4
        assert "_ledger_active_conversation" not in context.user_data

    async def test_an_unreadable_quick_form_ends_rather_than_half_starting(
        self, db, user_id
    ):
        """Returning ASK here would leave PTB in a state nothing else believes in."""
        update = _update(user_id=user_id)
        context = _context(db, ["heavy"])

        state = await weight.weight_command(update, context)

        assert state == ConversationHandler.END
        assert "_ledger_active_conversation" not in context.user_data
        assert "couldn't read" in _texts(update.effective_message)[0]

    async def test_the_prompt_offers_nudges_around_the_last_weigh_in(
        self, db, user_id
    ):
        await db.ensure_user(user_id, None, None)
        await db.log_weight(user_id, 72.4, today_local() - timedelta(days=1))
        update = _update(user_id=user_id)
        context = _context(db)

        state = await weight.weight_command(update, context)

        assert state == weight.ASK
        labels = [
            button.text
            for row in _last_markup(update.effective_message).inline_keyboard
            for button in row
        ]
        assert labels == ["72.2", "72.3", "72.4", "72.5", "72.6"]
        assert "yesterday" in _texts(update.effective_message)[0]

    async def test_a_first_ever_weigh_in_gets_no_grid_to_guess_from(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        update = _update(user_id=user_id)

        state = await weight.weight_command(update, _context(db))

        assert state == weight.ASK
        assert _last_markup(update.effective_message) is None

    async def test_tapping_a_nudge_logs_that_exact_weight(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        update = _callback(keyboards.weight_tap_data(user_id, 72.3), user_id)
        context = _context(db)

        state = await weight.weight_tap(update, context)

        assert state == ConversationHandler.END
        assert await db.get_weight_on(user_id, today_local()) == 72.3

    async def test_a_button_stamped_with_another_owner_writes_nothing(
        self, db, user_id
    ):
        await db.ensure_user(user_id, None, None)
        update = _callback(keyboards.weight_tap_data(OTHER, 72.3), user_id)

        state = await weight.weight_tap(update, _context(db))

        assert state == weight.ASK
        assert update.callback_query.answer.await_args.kwargs["show_alert"] is True
        assert await db.get_weight_on(user_id, today_local()) is None

    async def test_a_typed_weight_is_saved(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        update = _update("71,8 kg", user_id)

        state = await weight.receive_weight(update, _context(db))

        assert state == ConversationHandler.END
        assert await db.get_weight_on(user_id, today_local()) == 71.8

    async def test_unreadable_text_keeps_the_prompt_alive(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        update = _update("about the same", user_id)

        state = await weight.receive_weight(update, _context(db))

        assert state == weight.ASK
        assert await db.get_weight_on(user_id, today_local()) is None

    async def test_an_out_of_range_number_is_told_apart_from_a_typo(
        self, db, user_id
    ):
        """"724" parses fine as a number; it is not a weight."""
        await db.ensure_user(user_id, None, None)
        update = _update("724", user_id)

        state = await weight.receive_weight(update, _context(db))

        assert state == weight.ASK
        assert "between" in _texts(update.effective_message)[0]
        assert await db.get_weight_on(user_id, today_local()) is None

    async def test_a_correction_reports_what_it_replaced(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        await db.log_weight(user_id, 72.4, today_local())
        update = _update("71.9", user_id)

        await weight.receive_weight(update, _context(db))

        assert "72.4 → <b>71.9 kg</b>" in _texts(update.effective_message)[0]

    async def test_a_single_weigh_in_gets_no_trend_it_cannot_support(
        self, db, user_id
    ):
        """A "7-day average" from one day is that day wearing a better label."""
        await db.ensure_user(user_id, None, None)
        update = _update("72.4", user_id)

        await weight.receive_weight(update, _context(db))

        text = _texts(update.effective_message)[0]
        assert "7-day average" not in text
        assert "a few more days" in text

    async def test_the_trend_appears_once_there_is_something_to_average(
        self, db, user_id
    ):
        await db.ensure_user(user_id, None, None)
        today = today_local()
        for offset in range(1, 4):
            await db.log_weight(user_id, 72.0, today - timedelta(days=offset))
        update = _update("71.6", user_id)

        await weight.receive_weight(update, _context(db))

        assert "7-day average" in _texts(update.effective_message)[0]

    async def test_clearing_today_removes_the_row(self, db, user_id):
        await db.ensure_user(user_id, None, None)
        await db.log_weight(user_id, 72.4, today_local())
        update = _callback(keyboards.weight_clear_data(user_id), user_id)

        state = await weight.weight_tap(update, _context(db))

        assert state == ConversationHandler.END
        assert await db.get_weight_on(user_id, today_local()) is None

    async def test_an_expired_prompt_writes_nothing(self, db, user_id):
        """A weight tapped from old scrollback is a reading from an unknown day."""
        await db.ensure_user(user_id, None, None)
        update = _callback(keyboards.weight_tap_data(user_id, 72.3), user_id)

        await weight.stale_weight_callback(update, _context(db))

        assert await db.get_weight_on(user_id, today_local()) is None
        assert "expired" in update.callback_query.answer.await_args.args[0]

    async def test_the_menu_tap_opens_the_prompt_and_owns_the_flow(self, db, user_id):
        """The tap must mark the flow active, or Home's guards can't see it."""
        await db.ensure_user(user_id, None, None)
        update = _callback("menu_weight", user_id)
        context = _context(db)

        state = await weight.weight_menu_entry(update, context)

        assert state == weight.ASK
        assert context.user_data["_ledger_active_conversation"] == ("weight", user_id)
        assert "Today's weight" in _texts(update.callback_query.message)[0]

    async def test_saving_releases_the_flow_marker(self, db, user_id):
        """A marker left behind would make the next ordinary message look stuck."""
        await db.ensure_user(user_id, None, None)
        context = _context(db)
        await weight.weight_menu_entry(_callback("menu_weight", user_id), context)

        await weight.receive_weight(_update("72.4", user_id), context)

        assert "_ledger_active_conversation" not in context.user_data


class TestCallbackEncoding:
    def test_a_weight_survives_the_round_trip(self):
        data = keyboards.weight_tap_data(UID, 72.35)

        assert keyboards.parse_weight_tap(data, UID) == ("v", 72.35)

    def test_another_users_button_does_not_decode(self):
        data = keyboards.weight_tap_data(UID, 72.35)

        assert keyboards.parse_weight_tap(data, OTHER) is None

    @pytest.mark.parametrize(
        "data", ["", "wt", "wt_v_abc_100", "wt_v_1_notanumber", "sc_m_1_snack"]
    )
    def test_a_malformed_payload_is_rejected(self, data):
        assert keyboards.parse_weight_tap(data, UID) is None

    def test_the_registered_pattern_matches_what_the_keyboard_emits(self):
        """A pattern that misses its own buttons would make the flow dead."""
        import re

        pattern = re.compile(weight._PATTERN)
        assert pattern.match(keyboards.weight_tap_data(UID, 72.3))
        assert pattern.match(keyboards.weight_clear_data(UID))


# ---------------------------------------------------------------------------
# Home and analytics
# ---------------------------------------------------------------------------
class TestSurfaces:
    async def test_home_carries_a_weight_line(self, db, user_id):
        from bot.handlers import home

        await db.ensure_user(user_id, "t", "T")
        await db.log_weight(user_id, 72.4, today_local())
        update = _update("hi", user_id)

        await home.show_home(update, _context(db))

        assert "⚖️ Weight: 72.4 kg" in _texts(update.effective_message)[0]

    async def test_home_names_the_last_weigh_in_rather_than_a_bare_dash(
        self, db, user_id
    ):
        from bot.handlers import home

        await db.ensure_user(user_id, "t", "T")
        await db.log_weight(user_id, 72.4, today_local() - timedelta(days=2))
        update = _update("hi", user_id)

        await home.show_home(update, _context(db))

        assert "last 72.4 kg 2 days ago" in _texts(update.effective_message)[0]

    async def test_the_home_grid_offers_weight(self):
        labels = [
            button.text
            for row in keyboards.main_menu_keyboard().inline_keyboard
            for button in row
        ]
        assert "⚖️ Weight" in labels

    async def test_a_weight_chart_request_without_data_explains_itself(
        self, db, user_id
    ):
        from bot.handlers.analytics import _send_weight_chart

        await db.ensure_user(user_id, None, None)
        message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock())

        await _send_weight_chart(message, db, user_id, today_local())

        message.reply_photo.assert_not_awaited()
        assert "No weight logged yet" in message.reply_text.await_args.args[0]

    async def test_the_chart_caption_carries_the_average(self, db, user_id):
        from bot.handlers.analytics import _send_weight_chart

        await db.ensure_user(user_id, None, None)
        today = today_local()
        for offset in range(3):
            await db.log_weight(user_id, 72.0, today - timedelta(days=offset))
        message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock())

        await _send_weight_chart(message, db, user_id, today)

        caption = message.reply_photo.await_args.kwargs["caption"]
        assert "7-day avg 72 kg" in caption

    def test_the_chart_renders_measured_and_carried_days(self):
        days = _days(DAY, 5)
        buf = charts.weight_chart(
            [(days[0], 72.4), (days[3], 71.8)], days=5, end_date=days[4]
        )

        assert buf.getbuffer().nbytes > 0

    def test_an_empty_window_renders_a_panel_rather_than_failing(self):
        buf = charts.weight_chart([], days=30, end_date=DAY)

        assert buf.getbuffer().nbytes > 0
