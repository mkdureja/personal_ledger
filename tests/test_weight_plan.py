"""The weight plan: a straight line, and the scale compared against it honestly.

The guarantees worth protecting, in the order they can break:

* **The line is exact.** 68.85 kg at −0.05 kg/day for 28 days ends on day 28 at
  67.50, not 67.50000000000001 and not on day 29.
* **"Ahead" depends on the plan's direction.** Under the line is ahead on a
  loss plan and behind on a gain plan.
* **Behind by less than the daily swing is not a warning.**
* **The average compares like with like** — the mean of the measured days
  against the mean target over *those same days* — so a perfectly on-plan week
  reads on target even though the line slopes. The chart's trend line is the
  same comparison, so the picture and the text agree.
* **A plan is a definition.** It survives a log wipe (tested in
  ``test_reset_everything``), a new plan retires rather than deletes the old one,
  and there is never more than one active plan.
"""

from __future__ import annotations

import ast
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import charts, keyboards, weight_plan
from bot.config import today_local
from bot.handlers import plan as plan_handler
from bot.weight_plan import (
    AHEAD,
    BEHIND,
    CLOSE,
    ON_TARGET,
    PlanInputError,
    PlanReading,
    make_plan,
    parse_plan_args,
    plan_status,
    trend_points,
    verdict,
)

UID = 123456789  # matches conftest ALLOWED_USER_IDS
OTHER = 987654321
START = date(2026, 11, 25)


def _his_plan() -> weight_plan.WeightPlan:
    return make_plan(START, 68.85, -0.05, 28)


def _on_line(plan, days: int, *, offset: float = 0.0):
    """Weigh-ins exactly on the line (plus ``offset``) for the first ``days`` days."""
    return [
        (plan.start_date + timedelta(days=i),
         plan.target_on(plan.start_date + timedelta(days=i)) + offset)
        for i in range(days)
    ]


# ---------------------------------------------------------------------------
# The line
# ---------------------------------------------------------------------------
class TestTheLine:
    def test_his_plan_ends_on_day_28_at_67_5(self):
        plan = _his_plan()

        assert plan.end_date == date(2026, 12, 22)
        assert plan.end_kg == 67.5
        assert plan.total_change_kg == pytest.approx(-1.35)

    def test_day_one_is_the_starting_weight(self):
        assert _his_plan().target_on(START) == 68.85

    def test_each_day_moves_by_the_rate(self):
        plan = _his_plan()

        assert plan.target_on(START + timedelta(days=2)) == 68.75
        assert plan.target_on(START + timedelta(days=10)) == 68.35

    def test_there_is_no_target_outside_the_plan(self):
        plan = _his_plan()

        assert plan.target_on(START - timedelta(days=1)) is None
        assert plan.target_on(plan.end_date + timedelta(days=1)) is None

    def test_day_numbers_count_from_one(self):
        plan = _his_plan()

        assert plan.day_number(START) == 1
        assert plan.day_number(plan.end_date) == 28
        assert plan.day_number(START - timedelta(days=1)) == 0

    @pytest.mark.parametrize(
        "args",
        [
            (START, 10.0, -0.05, 28),     # starting weight below the floor
            (START, 68.85, -0.05, 0),     # no days
            (START, 68.85, -0.05, 400),   # past a year
            (START, 68.85, -5.0, 28),     # a misplaced decimal point
            (START, 25.0, -0.5, 300),     # finishes below a plausible weight
        ],
    )
    def test_implausible_plans_are_refused(self, args):
        with pytest.raises(PlanInputError):
            make_plan(*args)


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------
class TestVerdict:
    def _reading(self, actual, target=68.75):
        return PlanReading(actual_kg=actual, target_kg=target, day=START)

    def test_under_the_line_on_a_loss_plan_is_ahead(self):
        assert verdict(_his_plan(), self._reading(68.6)) == AHEAD

    def test_exactly_on_the_line_is_on_target(self):
        assert verdict(_his_plan(), self._reading(68.75)) == ON_TARGET

    def test_a_little_over_on_a_loss_plan_is_close_not_a_warning(self):
        assert verdict(_his_plan(), self._reading(68.95)) == CLOSE

    def test_past_the_daily_swing_is_behind(self):
        assert verdict(_his_plan(), self._reading(69.2)) == BEHIND

    def test_direction_flips_for_a_gain_plan(self):
        gain = make_plan(START, 60.0, 0.05, 28)

        assert verdict(gain, self._reading(60.2, 60.0)) == AHEAD
        assert verdict(gain, self._reading(59.6, 60.0)) == BEHIND

    def test_a_hold_plan_has_no_ahead(self):
        hold = make_plan(START, 70.0, 0.0, 28)

        assert verdict(hold, self._reading(69.5, 70.0)) == BEHIND
        assert verdict(hold, self._reading(70.1, 70.0)) == CLOSE

    def test_the_phrase_carries_the_amount_and_direction(self):
        plan = _his_plan()

        assert weight_plan.verdict_phrase(plan, self._reading(68.7)) == (
            "✅ 0.05 kg ahead"
        )
        assert "⚠️ 0.45 kg behind" == weight_plan.verdict_phrase(
            plan, self._reading(69.2)
        )


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
class TestStatus:
    def test_before_day_one_it_is_upcoming_with_nothing_to_compare(self):
        status = plan_status(_his_plan(), [(START - timedelta(days=1), 69.0)],
                             START - timedelta(days=1))

        assert status.phase == "upcoming"
        assert status.latest is None
        assert status.day_number == 0
        assert status.days_to_start == 1

    def test_the_latest_weigh_in_inside_the_plan_is_compared(self):
        plan = _his_plan()
        today = START + timedelta(days=2)
        status = plan_status(plan, [(START, 68.9), (today, 68.7)], today)

        assert status.phase == "running"
        assert status.day_number == 3
        assert status.target_today == 68.75
        assert status.latest.actual_kg == 68.7
        assert status.latest.target_kg == 68.75

    def test_weigh_ins_before_the_plan_or_after_today_are_ignored(self):
        plan = _his_plan()
        today = START + timedelta(days=1)
        status = plan_status(
            plan,
            [(START - timedelta(days=3), 70.0), (today + timedelta(days=1), 60.0)],
            today,
        )

        assert status.latest is None
        assert status.measured_days == 0

    def test_the_average_needs_three_weigh_ins(self):
        plan = _his_plan()
        two = plan_status(plan, _on_line(plan, 2), START + timedelta(days=1))
        three = plan_status(plan, _on_line(plan, 3), START + timedelta(days=2))

        assert two.average is None
        assert three.average is not None
        assert three.average.measured_days == 3

    def test_a_perfectly_on_plan_week_averages_on_target_despite_the_slope(self):
        """The reason the average compares like with like."""
        plan = _his_plan()
        today = START + timedelta(days=6)
        status = plan_status(plan, _on_line(plan, 7), today)

        assert verdict(plan, status.average) == ON_TARGET

    def test_the_required_pace_rises_when_behind(self):
        plan = _his_plan()
        today = START + timedelta(days=9)
        on = plan_status(plan, _on_line(plan, 10), today)
        behind = plan_status(plan, _on_line(plan, 10, offset=0.4), today)

        assert on.required_daily_kg == pytest.approx(-0.05)
        assert behind.required_daily_kg < on.required_daily_kg

    def test_progress_is_measured_from_the_starting_weight(self):
        plan = _his_plan()
        today = START + timedelta(days=3)
        status = plan_status(plan, [(today, 68.6)], today)

        assert status.change_so_far_kg == pytest.approx(-0.25)

    def test_after_the_last_day_it_is_finished_and_asks_no_pace(self):
        plan = _his_plan()
        status = plan_status(plan, _on_line(plan, 28), plan.end_date + timedelta(days=2))

        assert status.phase == "finished"
        assert status.required_daily_kg is None
        assert status.latest.day == plan.end_date


class TestTrend:
    def test_on_line_readings_trend_along_the_line(self):
        plan = _his_plan()
        points = trend_points(plan, _on_line(plan, 7), START + timedelta(days=6))

        drawn = [(day, value) for day, value in points if value is not None]
        assert drawn, "a week on the line should draw a trend"
        for day, value in drawn:
            assert value == pytest.approx(plan.target_on(day))

    def test_the_trend_agrees_with_the_status_average(self):
        plan = _his_plan()
        entries = [
            (START + timedelta(days=i), kg)
            for i, kg in enumerate([68.9, 68.7, 68.8, 68.5, 68.6, 68.4])
        ]
        today = START + timedelta(days=5)
        status = plan_status(plan, entries, today)
        last_day, last_value = trend_points(plan, entries, today)[-1]

        assert last_day == status.latest.day
        assert last_value - plan.target_on(last_day) == pytest.approx(
            status.average.gap_kg, abs=0.001
        )

    def test_a_long_gap_breaks_the_line(self):
        plan = _his_plan()
        entries = _on_line(plan, 3) + [
            (START + timedelta(days=14), plan.target_on(START + timedelta(days=14)))
        ]
        points = dict(trend_points(plan, entries, START + timedelta(days=14)))

        assert points[START + timedelta(days=14)] is None


# ---------------------------------------------------------------------------
# Reading /plan new
# ---------------------------------------------------------------------------
class TestParsing:
    TODAY = date(2026, 11, 24)

    def test_his_exact_message(self):
        plan = parse_plan_args(["25", "nov", "68.85", "-0.05", "28"], self.TODAY)

        assert plan == make_plan(START, 68.85, -0.05, 28)

    def test_a_finish_weight_sets_the_constant_rate(self):
        plan = parse_plan_args(["tomorrow", "68.85", "67.5", "28"], self.TODAY)

        assert plan.start_date == START
        assert plan.daily_change_kg == pytest.approx(-0.05)
        assert plan.end_kg == 67.5

    @pytest.mark.parametrize(
        "day_tokens", [["2026-11-25"], ["nov", "25"], ["tomorrow"]]
    )
    def test_the_first_day_can_be_written_several_ways(self, day_tokens):
        plan = parse_plan_args(day_tokens + ["68.85", "-0.05", "28d"], self.TODAY)

        assert plan.start_date == START

    def test_a_day_and_month_means_the_nearest_one(self):
        plan = parse_plan_args(
            ["25", "nov", "68.85", "-0.05", "28"], date(2027, 1, 2)
        )

        assert plan.start_date == START

    def test_a_bare_rate_must_say_which_way(self):
        """Guessing a direction would draw the line the wrong way with confidence."""
        with pytest.raises(PlanInputError, match="loss or a gain"):
            parse_plan_args(["tomorrow", "68.85", "0.05", "28"], self.TODAY)

    def test_a_plus_rate_is_a_gain(self):
        plan = parse_plan_args(["tomorrow", "60", "+0.05", "28"], self.TODAY)

        assert plan.daily_change_kg == 0.05

    def test_too_little_is_answered_with_the_usage(self):
        with pytest.raises(PlanInputError, match="/plan new"):
            parse_plan_args(["68.85"], self.TODAY)

    def test_echoed_input_is_escaped(self):
        with pytest.raises(PlanInputError) as caught:
            parse_plan_args(["tomorrow", "<b>", "-0.05", "28"], self.TODAY)

        assert "<b><b>" not in str(caught.value)
        assert "&lt;b&gt;" in str(caught.value)


def test_formatting_carries_sign_and_drops_noise():
    assert weight_plan.format_rate(-0.05) == "−0.05 kg/day"
    assert weight_plan.format_rate(0.0475) == "+0.048 kg/day"
    assert weight_plan.format_rate(0) == "0 kg/day"
    assert weight_plan.format_signed_kg(-0.15) == "−0.15 kg"
    assert weight_plan.format_signed_kg(0.001) == "0 kg"


def test_the_plan_module_stays_pure():
    """Four screens share this arithmetic; it is only shared if it cannot drift."""
    tree = ast.parse(Path(weight_plan.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])

    forbidden = imported & {
        "telegram", "aiosqlite", "sqlite3", "matplotlib", "numpy",
        "config", "database", "handlers", "keyboards", "charts", "main",
    }
    assert not forbidden, f"weight_plan must stay pure; found {forbidden}"
    assert not any(
        isinstance(node, (ast.AsyncFunctionDef, ast.Await)) for node in ast.walk(tree)
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
class TestStorage:
    async def test_a_plan_round_trips(self, db_with_user, user_id):
        await db_with_user.start_weight_plan(user_id, _his_plan())

        assert await db_with_user.get_active_weight_plan(user_id) == _his_plan()

    async def test_a_new_plan_retires_the_old_and_reports_it(
        self, db_with_user, user_id
    ):
        first = _his_plan()
        second = make_plan(START + timedelta(days=28), 67.5, -0.05, 28)
        await db_with_user.start_weight_plan(user_id, first)

        result = await db_with_user.start_weight_plan(user_id, second)

        assert result["replaced"] == first
        assert await db_with_user.get_active_weight_plan(user_id) == second
        cursor = await db_with_user.conn.execute(
            "SELECT COUNT(*) AS n FROM weight_plans WHERE user_id = ?", (user_id,)
        )
        assert (await cursor.fetchone())["n"] == 2

    async def test_ending_keeps_the_row(self, db_with_user, user_id):
        await db_with_user.start_weight_plan(user_id, _his_plan())

        assert await db_with_user.end_weight_plan(user_id) is True
        assert await db_with_user.end_weight_plan(user_id) is False
        assert await db_with_user.get_active_weight_plan(user_id) is None
        cursor = await db_with_user.conn.execute(
            "SELECT ended_at FROM weight_plans WHERE user_id = ?", (user_id,)
        )
        assert (await cursor.fetchone())["ended_at"] is not None

    async def test_plans_are_per_user(self, db_with_user, user_id):
        await db_with_user.ensure_user(OTHER, "o", "Other")
        await db_with_user.start_weight_plan(user_id, _his_plan())

        assert await db_with_user.get_active_weight_plan(OTHER) is None

    async def test_two_active_plans_are_impossible_in_the_schema(
        self, db_with_user, user_id
    ):
        await db_with_user.start_weight_plan(user_id, _his_plan())

        with pytest.raises(sqlite3.IntegrityError):
            await db_with_user.conn.execute(
                "INSERT INTO weight_plans (user_id, start_date, start_weight_kg, "
                "daily_change_kg, length_days) VALUES (?, '2026-12-01', 70, -0.05, 28)",
                (user_id,),
            )

    async def test_the_storage_refuses_what_the_parser_refuses(
        self, db_with_user, user_id
    ):
        bad = weight_plan.WeightPlan(START, 68.85, -5.0, 28)

        with pytest.raises(PlanInputError):
            await db_with_user.start_weight_plan(user_id, bad)


async def test_migrating_a_populated_v18_database_adds_plans_and_keeps_rows(
    monkeypatch,
):
    """The exact step the deployed database takes on its next startup."""
    from bot import migrations
    from bot.database import DatabaseManager

    mgr = DatabaseManager(":memory:")
    await mgr.connect()
    try:
        with monkeypatch.context() as patched:
            patched.setattr(migrations, "LATEST_VERSION", 18)
            await migrations.run_migrations(mgr.conn)
        await mgr.ensure_user(UID, "t", "Test")
        await mgr.log_weight(UID, 71.1, date(2026, 8, 26))

        await migrations.run_migrations(mgr.conn)

        assert await migrations.get_user_version(mgr.conn) == 19
        assert (await mgr.get_latest_weight(UID))["weight_kg"] == 71.1
        assert await mgr.get_active_weight_plan(UID) is None
        await migrations.verify_current_schema(mgr.conn)
        cursor = await mgr.conn.execute("PRAGMA foreign_key_check")
        assert await cursor.fetchall() == []
    finally:
        await mgr.close()


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------
def _message():
    return SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock())


def _update(user_id: int = UID):
    message = _message()
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id, username="t", first_name="T"),
        effective_chat=SimpleNamespace(id=user_id, type="private"),
    )


def _context(db, args=None):
    return SimpleNamespace(bot_data={"db": db}, user_data={}, args=args or [])


class TestScreens:
    async def test_no_plan_explains_how_to_start_one(self, db, user_id):
        update = _update(user_id)

        await plan_handler.plan_command(update, _context(db))

        update.effective_message.reply_photo.assert_not_awaited()
        assert "No weight plan yet" in update.effective_message.reply_text.await_args.args[0]

    async def test_plan_new_saves_and_shows_the_chart(self, db, user_id):
        update = _update(user_id)

        await plan_handler.plan_command(
            update, _context(db, ["new", "today", "70", "-0.05", "28"])
        )

        plan = await db.get_active_weight_plan(user_id)
        assert plan.start_date == today_local()
        assert plan.end_kg == 68.65
        photo = update.effective_message.reply_photo.await_args
        assert "day 1 of 28" in photo.kwargs["caption"]
        assert photo.kwargs["parse_mode"] == "HTML"

    async def test_a_bad_plan_is_refused_and_nothing_is_stored(self, db, user_id):
        update = _update(user_id)

        await plan_handler.plan_command(
            update, _context(db, ["new", "today", "70", "0.05", "28"])
        )

        assert await db.get_active_weight_plan(user_id) is None
        assert "loss or a gain" in update.effective_message.reply_text.await_args.args[0]

    async def test_plan_end(self, db, user_id):
        await db.ensure_user(user_id, "t", "T")
        await db.start_weight_plan(user_id, make_plan(today_local(), 70, -0.05, 28))
        update = _update(user_id)

        await plan_handler.plan_command(update, _context(db, ["end"]))

        assert await db.get_active_weight_plan(user_id) is None

    async def test_the_status_names_the_target_and_the_verdict(self, db, user_id):
        await db.ensure_user(user_id, "t", "T")
        today = today_local()
        await db.start_weight_plan(
            user_id, make_plan(today - timedelta(days=2), 70, -0.05, 28)
        )
        await db.log_weight(user_id, 69.8, today)
        update = _update(user_id)

        await plan_handler.plan_command(update, _context(db))

        caption = update.effective_message.reply_photo.await_args.kwargs["caption"]
        assert "day 3 of 28" in caption
        assert "Target today: <b>69.9 kg</b>" in caption
        assert "0.1 kg ahead" in caption

    async def test_a_chart_failure_still_sends_the_numbers(
        self, db, user_id, monkeypatch
    ):
        await db.ensure_user(user_id, "t", "T")
        await db.start_weight_plan(user_id, make_plan(today_local(), 70, -0.05, 28))

        def _boom(*_args, **_kwargs):
            raise RuntimeError("no chart today")

        monkeypatch.setattr(charts, "plan_chart", _boom)
        update = _update(user_id)

        await plan_handler.plan_command(update, _context(db))

        update.effective_message.reply_photo.assert_not_awaited()
        assert "day 1 of 28" in update.effective_message.reply_text.await_args.args[0]


class TestElsewhere:
    async def test_the_weight_receipt_reports_against_todays_target(self, db, user_id):
        from bot.handlers import weight

        await db.ensure_user(user_id, "t", "T")
        today = today_local()
        await db.start_weight_plan(
            user_id, make_plan(today - timedelta(days=2), 70, -0.05, 28)
        )
        update = _update(user_id)

        await weight.weight_command(update, _context(db, ["70.2"]))

        call = update.effective_message.reply_text.await_args
        assert "Plan day 3/28: target <b>69.9 kg</b>" in call.args[0]
        assert "0.3 kg behind (within the daily swing)" in call.args[0]
        buttons = [b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard
                   for b in row]
        assert buttons == ["menu_plan"]

    async def test_an_upcoming_plan_is_announced_on_the_receipt(self, db, user_id):
        from bot.handlers import weight

        await db.ensure_user(user_id, "t", "T")
        await db.start_weight_plan(
            user_id, make_plan(today_local() + timedelta(days=1), 68.85, -0.05, 28)
        )
        update = _update(user_id)

        await weight.weight_command(update, _context(db, ["69"]))

        assert "plan starts tomorrow" in update.effective_message.reply_text.await_args.args[0]

    async def test_without_a_plan_the_receipt_is_unchanged(self, db, user_id):
        from bot.handlers import weight

        update = _update(user_id)

        await weight.weight_command(update, _context(db, ["70"]))

        call = update.effective_message.reply_text.await_args
        assert "Plan" not in call.args[0]
        assert call.kwargs.get("reply_markup") is None

    async def test_home_carries_a_plan_line_only_with_a_plan(self, db, user_id):
        from bot.handlers import home

        await db.ensure_user(user_id, "t", "T")
        update = _update(user_id)
        await home.show_home(update, _context(db))
        assert "📉 Plan" not in update.effective_message.reply_text.await_args_list[0].args[0]

        await db.start_weight_plan(
            user_id, make_plan(today_local() - timedelta(days=2), 70, -0.05, 28)
        )
        update = _update(user_id)
        await home.show_home(update, _context(db))
        assert "📉 Plan: day 3/28 · target 69.9 kg" in (
            update.effective_message.reply_text.await_args_list[0].args[0]
        )

    def test_home_offers_the_plan_through_menu_callback(self):
        from bot.handlers import start

        data = [
            button.callback_data
            for row in keyboards.main_menu_keyboard().inline_keyboard
            for button in row
        ]
        assert "menu_plan" in data
        assert "menu_plan" not in start._CONVERSATION_MENU_ACTIONS

    def test_the_real_handler_table_routes_plan_taps_and_commands(self):
        from tests.test_menu_routing import (
            _callback_update,
            _command_update,
            _first_handler,
            _handler_name,
        )
        from bot.main import COMMAND_MENU, build_application

        app = build_application()
        assert _handler_name(_first_handler(app, _callback_update(UID, "menu_plan"))) == (
            "menu_callback"
        )
        assert _handler_name(_first_handler(app, _command_update(UID, "/plan"))) == (
            "plan_command"
        )
        assert "plan" in dict(COMMAND_MENU)


class TestChart:
    def test_it_renders_before_during_and_after(self):
        plan = _his_plan()
        entries = _on_line(plan, 10, offset=0.1)
        for today in (START - timedelta(days=1), START + timedelta(days=9),
                      plan.end_date + timedelta(days=3)):
            assert charts.plan_chart(plan, entries, today).getbuffer().nbytes > 0

    def test_it_renders_with_no_weigh_ins(self):
        assert charts.plan_chart(_his_plan(), [], START).getbuffer().nbytes > 0

    def test_a_gain_plan_renders(self):
        gain = make_plan(START, 60.0, 0.05, 28)
        assert charts.plan_chart(gain, _on_line(gain, 5), START + timedelta(days=4))
