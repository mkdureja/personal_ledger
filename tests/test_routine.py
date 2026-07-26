"""Tests for routine anchors: config validation, quote rotation, log-aware
message composition, the scheduled job, and startup wiring."""

from __future__ import annotations

import logging
from datetime import date, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import ApplicationBuilder

from bot import main as main_module
from bot.config import today_local
from bot.handlers.reminders import anchor_job, build_anchor_message
from bot.routine import (
    WEEKDAYS,
    Anchor,
    RoutineConfigError,
    Targets,
    load_routine,
    pick_quote,
)

VALID_YAML = """\
quotes:
  - "Discipline equals freedom."
  - "Stay consistent."
targets:
  study_min: 60
  gym_days: [Mon, Wed, Fri]
anchors:
  - id: morning
    time: "08:00"
    emoji: "☀️"
    title: "Morning kickoff"
    checks: []
    quote: true
  - id: evening
    time: "21:30"
    emoji: "🌙"
    title: "Evening review"
    checks: [habits, study, gym, diet]
    quote: true
"""


def _write(tmp_path, text):
    path = tmp_path / "routine.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Config loading & validation
# ---------------------------------------------------------------------------
def test_missing_file_returns_none(tmp_path):
    assert load_routine(tmp_path / "does-not-exist.yaml") is None


def test_valid_file_parses(tmp_path):
    routine = load_routine(_write(tmp_path, VALID_YAML))
    assert routine is not None
    assert routine.quotes == ("Discipline equals freedom.", "Stay consistent.")
    assert routine.targets.study_min == 60
    assert routine.targets.gym_days == frozenset({"Mon", "Wed", "Fri"})
    assert [a.id for a in routine.anchors] == ["morning", "evening"]
    morning, evening = routine.anchors
    assert morning.at == time(8, 0)
    assert morning.checks == ()
    assert morning.quote is True
    assert evening.at == time(21, 30)
    assert evening.checks == ("habits", "study", "gym", "diet")


def test_defaults_when_optional_sections_absent(tmp_path):
    text = 'anchors:\n  - id: solo\n    time: "07:00"\n'
    routine = load_routine(_write(tmp_path, text))
    assert routine.quotes == ()
    assert routine.targets == Targets()
    assert routine.anchors[0].checks == ()
    assert routine.anchors[0].quote is False
    assert routine.anchors[0].title == "solo"  # falls back to id


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty file
        "[]",  # top level not a mapping
        "quotes: [ok]\n",  # no anchors section
        "anchors: []\n",  # empty anchors list
        "anchors: [unclosed\n",  # not valid YAML
    ],
)
def test_structural_errors(tmp_path, text):
    with pytest.raises(RoutineConfigError):
        load_routine(_write(tmp_path, text))


@pytest.mark.parametrize(
    "anchors_block",
    [
        'anchors:\n  - {id: a, time: "25:00"}\n',  # hour out of range
        'anchors:\n  - {id: a, time: "12:60"}\n',  # minute out of range
        'anchors:\n  - {id: a, time: "0800"}\n',  # not HH:MM
        "anchors:\n  - {id: a, time: 800}\n",  # not a string
        'anchors:\n  - {time: "08:00"}\n',  # missing id
        'anchors:\n  - {id: a, time: "08:00", checks: [sleep]}\n',  # unknown check
        'anchors:\n  - {id: a, time: "08:00", checks: [study, study]}\n',  # dup check
        'anchors:\n  - {id: a, time: "08:00", quote: maybe}\n',  # non-bool quote
        'anchors:\n  - {id: dup, time: "08:00"}\n  - {id: dup, time: "09:00"}\n',  # dup id
    ],
)
def test_anchor_validation_errors(tmp_path, anchors_block):
    with pytest.raises(RoutineConfigError):
        load_routine(_write(tmp_path, anchors_block))


@pytest.mark.parametrize(
    "targets_block",
    [
        "targets:\n  study_min: -5\n",
        "targets:\n  study_min: true\n",
        "targets:\n  study_min: 1.5\n",
        "targets:\n  gym_days: [Funday]\n",
        "targets:\n  gym_days: Mon\n",  # not a list
        "targets: []\n",  # not a mapping
    ],
)
def test_targets_validation_errors(tmp_path, targets_block):
    text = targets_block + 'anchors:\n  - {id: a, time: "08:00"}\n'
    with pytest.raises(RoutineConfigError):
        load_routine(_write(tmp_path, text))


def test_quote_true_without_quotes_warns(tmp_path, caplog):
    """A quote:true anchor with no quotes loads but warns the user."""
    text = 'anchors:\n  - {id: a, time: "08:00", quote: true}\n'
    with caplog.at_level(logging.WARNING, logger="bot.routine"):
        routine = load_routine(_write(tmp_path, text))
    assert routine is not None
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "quote" in caplog.text.lower()


@pytest.mark.parametrize(
    "quotes_block",
    [
        "quotes: not-a-list\n",
        "quotes:\n  - ''\n",  # empty string
        "quotes:\n  - 42\n",  # not a string
    ],
)
def test_quotes_validation_errors(tmp_path, quotes_block):
    text = quotes_block + 'anchors:\n  - {id: a, time: "08:00"}\n'
    with pytest.raises(RoutineConfigError):
        load_routine(_write(tmp_path, text))


# ---------------------------------------------------------------------------
# Targets & quote rotation
# ---------------------------------------------------------------------------
def test_is_gym_day():
    monday = date.fromisocalendar(2026, 30, 1)
    tuesday = date.fromisocalendar(2026, 30, 2)
    assert monday.weekday() == 0 and tuesday.weekday() == 1
    only_mon = Targets(gym_days=frozenset({"Mon"}))
    assert only_mon.is_gym_day(monday) is True
    assert only_mon.is_gym_day(tuesday) is False
    assert Targets().is_gym_day(tuesday) is True  # empty means every day


def test_pick_quote_empty_returns_none():
    assert pick_quote((), "morning", date(2026, 7, 25)) is None


def test_pick_quote_deterministic_and_rotates():
    quotes = ("a", "b", "c", "d", "e")
    day = date(2026, 7, 25)
    # Stable within a day
    assert pick_quote(quotes, "morning", day) == pick_quote(quotes, "morning", day)
    # Rotates across days (consecutive ordinals, len > 1 -> different)
    assert pick_quote(quotes, "morning", day) != pick_quote(
        quotes, "morning", day + timedelta(days=1)
    )
    # Per-anchor offset ("a"/"b" differ by one -> different index)
    assert pick_quote(quotes, "a", day) != pick_quote(quotes, "b", day)
    # Always a member
    assert pick_quote(quotes, "x", day) in quotes


# ---------------------------------------------------------------------------
# Log-aware message composition
# ---------------------------------------------------------------------------
def _anchor(checks=(), quote=False, anchor_id="test", emoji="🔔", title="Test"):
    return Anchor(
        id=anchor_id, at=time(8, 0), emoji=emoji, title=title,
        checks=tuple(checks), quote=quote,
    )


@pytest.mark.asyncio
async def test_pure_motivation_anchor(db_with_user, user_id):
    anchor = _anchor(anchor_id="morning", title="Morning kickoff", quote=True)
    msg, unchecked = await build_anchor_message(
        anchor, db_with_user, user_id, today_local(), Targets(), ("Keep going",)
    )
    assert "Morning kickoff" in msg
    assert "<i>Keep going</i>" in msg
    assert unchecked == []


@pytest.mark.asyncio
async def test_study_line_target_states(db_with_user, user_id):
    today = today_local()
    anchor = _anchor(checks=["study"])
    targets = Targets(study_min=60)

    msg, _ = await build_anchor_message(anchor, db_with_user, user_id, today, targets, ())
    assert "0/60 min" in msg

    await db_with_user.log_study(user_id, "Math", 20)
    msg, _ = await build_anchor_message(anchor, db_with_user, user_id, today, targets, ())
    assert "20/60 min" in msg and "40 to go" in msg

    await db_with_user.log_study(user_id, "Math", 45)  # total 65
    msg, _ = await build_anchor_message(anchor, db_with_user, user_id, today, targets, ())
    assert "65/60 min" in msg and "on track" in msg


@pytest.mark.asyncio
async def test_study_line_without_target(db_with_user, user_id):
    anchor = _anchor(checks=["study"])
    await db_with_user.log_study(user_id, "Reading", 30)
    msg, _ = await build_anchor_message(
        anchor, db_with_user, user_id, today_local(), Targets(), ()
    )
    assert "30 min logged" in msg


@pytest.mark.asyncio
async def test_gym_line_states(db_with_user, user_id):
    today = today_local()
    anchor = _anchor(checks=["gym"])

    other = WEEKDAYS[(today.weekday() + 1) % 7]  # a weekday that isn't today
    rest = Targets(gym_days=frozenset({other}))
    msg, _ = await build_anchor_message(anchor, db_with_user, user_id, today, rest, ())
    assert "rest day" in msg

    msg, _ = await build_anchor_message(anchor, db_with_user, user_id, today, Targets(), ())
    assert "no workout logged" in msg

    await db_with_user.log_gym(user_id, "Squat", 3, 5, 100.0)
    msg, _ = await build_anchor_message(anchor, db_with_user, user_id, today, Targets(), ())
    assert "workout logged" in msg


@pytest.mark.asyncio
async def test_diet_line_states(db_with_user, user_id):
    today = today_local()
    anchor = _anchor(checks=["diet"])

    msg, _ = await build_anchor_message(anchor, db_with_user, user_id, today, Targets(), ())
    assert "nothing logged" in msg

    await db_with_user.log_diet(user_id, "lunch", "Dal rice", calories=500)
    await db_with_user.log_diet(user_id, "snack", "Apple", calories=95)
    msg, _ = await build_anchor_message(anchor, db_with_user, user_id, today, Targets(), ())
    assert "2 meals" in msg and "595 kcal" in msg


@pytest.mark.asyncio
async def test_diet_line_incomplete_calories(db_with_user, user_id):
    anchor = _anchor(checks=["diet"])
    await db_with_user.log_diet(user_id, "lunch", "Mystery", calories=None)
    msg, _ = await build_anchor_message(
        anchor, db_with_user, user_id, today_local(), Targets(), ()
    )
    assert "1 meal" in msg and "some missing calories" in msg


@pytest.mark.asyncio
async def test_habit_line_all_done(db_with_user, user_id):
    today = today_local()
    h1, _ = await db_with_user.add_habit(user_id, "Read")
    h2, _ = await db_with_user.add_habit(user_id, "Meditate")
    await db_with_user.check_habit(user_id, h1, today)
    await db_with_user.check_habit(user_id, h2, today)
    anchor = _anchor(checks=["habits"])
    msg, unchecked = await build_anchor_message(
        anchor, db_with_user, user_id, today, Targets(), ()
    )
    assert "all 2 done" in msg
    assert unchecked == []


@pytest.mark.asyncio
async def test_habit_line_some_left(db_with_user, user_id):
    today = today_local()
    h1, _ = await db_with_user.add_habit(user_id, "Read")
    await db_with_user.add_habit(user_id, "Meditate")
    await db_with_user.add_habit(user_id, "Walk")
    await db_with_user.check_habit(user_id, h1, today)
    anchor = _anchor(checks=["habits"])
    msg, unchecked = await build_anchor_message(
        anchor, db_with_user, user_id, today, Targets(), ()
    )
    assert "1/3 done" in msg and "2 left" in msg
    assert set(unchecked) == {"Meditate", "Walk"}


@pytest.mark.asyncio
async def test_no_habits_configured_omits_line(db_with_user, user_id):
    anchor = _anchor(checks=["habits"], title="Review")
    msg, unchecked = await build_anchor_message(
        anchor, db_with_user, user_id, today_local(), Targets(), ()
    )
    assert "Habits" not in msg  # line omitted entirely
    assert "Review" in msg  # header still present
    assert unchecked == []


# ---------------------------------------------------------------------------
# Scheduled job
# ---------------------------------------------------------------------------
def _job_context(db, anchor, targets=None, quotes=()):
    return SimpleNamespace(
        job=SimpleNamespace(data=anchor),
        bot_data={
            "db": db,
            "routine_targets": targets or Targets(),
            "routine_quotes": quotes,
        },
        bot=SimpleNamespace(send_message=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_anchor_job_sends_status(db_with_user, user_id):
    await db_with_user.set_reminders_enabled(user_id, True)
    anchor = _anchor(anchor_id="morning", title="Morning kickoff", quote=True)
    ctx = _job_context(db_with_user, anchor, quotes=("Go",))
    await anchor_job(ctx)

    calls = ctx.bot.send_message.await_args_list
    assert len(calls) == 1
    assert calls[0].kwargs["chat_id"] == user_id
    assert calls[0].kwargs["parse_mode"] == "HTML"
    assert "Morning kickoff" in calls[0].kwargs["text"]


@pytest.mark.asyncio
async def test_anchor_job_follows_up_with_unchecked_habits(db_with_user, user_id):
    await db_with_user.set_reminders_enabled(user_id, True)
    for name in ["Read", "Meditate", "Walk"]:
        await db_with_user.add_habit(user_id, name)
    anchor = _anchor(anchor_id="evening", title="Evening review", checks=["habits"])
    ctx = _job_context(db_with_user, anchor)
    await anchor_job(ctx)

    calls = ctx.bot.send_message.await_args_list
    assert len(calls) >= 2  # status message + habit-list follow-up
    assert "Evening review" in calls[0].kwargs["text"]
    assert all(c.kwargs["parse_mode"] == "HTML" for c in calls)
    # Follow-up is branded to this anchor, not the legacy evening reminder.
    assert "Evening review" in calls[1].kwargs["text"]
    assert "Evening Reminder" not in calls[1].kwargs["text"]
    combined = "\n".join(c.kwargs["text"] for c in calls[1:])
    for name in ["Read", "Meditate", "Walk"]:
        assert name in combined


@pytest.mark.asyncio
async def test_non_evening_anchor_habit_followup_is_branded(db_with_user, user_id):
    """A midday anchor that checks habits must not label itself 'Evening Reminder'."""
    await db_with_user.set_reminders_enabled(user_id, True)
    await db_with_user.add_habit(user_id, "Stretch")
    anchor = _anchor(anchor_id="midday", title="Midday check-in", checks=["habits"])
    ctx = _job_context(db_with_user, anchor)
    await anchor_job(ctx)

    calls = ctx.bot.send_message.await_args_list
    assert len(calls) >= 2
    followup = calls[1].kwargs["text"]
    assert "Midday check-in" in followup
    assert "Evening Reminder" not in followup
    assert "Stretch" in followup


# ---------------------------------------------------------------------------
# Startup wiring
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_post_init_schedules_anchors(tmp_path, monkeypatch):
    routine_file = tmp_path / "routine.yaml"
    routine_file.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.setattr(main_module, "DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(main_module, "ROUTINE_PATH", str(routine_file))

    application = ApplicationBuilder().token("123456:TEST_TOKEN").build()
    await main_module.post_init(application)
    try:
        names = sorted(job.name for job in application.job_queue.jobs())
        assert names == ["anchor_evening", "anchor_morning"]
        assert application.bot_data["routine_quotes"] == (
            "Discipline equals freedom.",
            "Stay consistent.",
        )
        assert application.bot_data["routine_targets"].study_min == 60
    finally:
        await main_module.post_shutdown(application)


@pytest.mark.asyncio
async def test_post_init_falls_back_without_routine(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(main_module, "ROUTINE_PATH", str(tmp_path / "none.yaml"))

    application = ApplicationBuilder().token("123456:TEST_TOKEN").build()
    await main_module.post_init(application)
    try:
        names = [job.name for job in application.job_queue.jobs()]
        assert names == ["daily_habit_reminder"]
    finally:
        await main_module.post_shutdown(application)
