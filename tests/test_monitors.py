"""Monitors — counted behaviours measured against a target.

A monitor is the one check-off surface in this bot that is deliberately *not*
idempotent, so the tests are organised around the consequences of that:

1. **Counting is real.** Two taps are two occurrences, an undo removes exactly
   one, and the count that a status line reports is the count inside the
   target's own window — not the day's, and not a rolling one.
2. **The target arithmetic lives in one pure place.** ``bot.monitor_targets``
   is asserted directly, including the distinction between "over the ceiling"
   and "not yet at the floor", which is the difference between a warning and a
   Tuesday.
3. **The habit-family guarantees still hold.** Ownership is embedded in every
   callback, a stale board is refused rather than re-dated, and removal is a
   soft archive that keeps history.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType

from bot import keyboards, monitor_targets
from bot.database import MAX_ACTIVE_MONITORS
from bot.handlers import monitors
from bot.handlers.monitors import (
    MonitorInputError,
    parse_monitor_input,
    parse_occurrence_detail,
)
from bot.monitor_targets import (
    Target,
    TargetParseError,
    evaluate,
    format_progress,
    parse_target,
    period_bounds,
    split_emoji_prefix,
)
from ledger_schema import TABLE_INTRODUCED, required_tables_for

UID = 123456789  # matches conftest ALLOWED_USER_IDS
OTHER = 987654321


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _context(db=None, user_data=None, args=None):
    return SimpleNamespace(
        bot_data={"db": db},
        user_data={} if user_data is None else user_data,
        args=args or [],
        bot=SimpleNamespace(edit_message_reply_markup=AsyncMock()),
    )


def _message(user_id: int = UID, message_id: int = 1):
    return SimpleNamespace(
        chat_id=user_id,
        message_id=message_id,
        reply_text=AsyncMock(
            return_value=SimpleNamespace(chat_id=user_id, message_id=message_id)
        ),
        edit_text=AsyncMock(),
    )


def _query(data: str, message=None):
    return SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=message or _message(),
        edit_message_reply_markup=AsyncMock(),
        edit_message_text=AsyncMock(),
    )


def _callback_update(query, user_id: int = UID):
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id, first_name="Test", username="t"),
        effective_chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
        effective_message=query.message,
        update_id=1,
    )


def _message_update(text: str, user_id: int = UID):
    message = SimpleNamespace(
        text=text,
        chat_id=user_id,
        message_id=100,
        reply_text=AsyncMock(
            return_value=SimpleNamespace(chat_id=user_id, message_id=100)
        ),
    )
    return SimpleNamespace(
        effective_message=message,
        message=message,
        effective_user=SimpleNamespace(id=user_id, first_name="Test", username="t"),
        effective_chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
        callback_query=None,
        update_id=1,
    )


async def _seed(db, user_id: int = UID):
    """The four monitors this household actually runs."""
    await db.ensure_user(user_id, "t", "Test")
    smoking, _ = await db.add_monitor(
        user_id, "Smoking", emoji="🚬", target_period="day", target_max=0
    )
    drinking, _ = await db.add_monitor(
        user_id, "Drinking", emoji="🍺", target_period="month", target_max=1
    )
    cannabis, _ = await db.add_monitor(
        user_id,
        "Cannabis",
        emoji="🌿",
        target_period="week",
        target_min=2,
        target_max=4,
        tracks_quantity=True,
        quantity_unit="g",
        tracks_variant=True,
    )
    return smoking, drinking, cannabis


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------
def test_monitor_tables_are_introduced_together_at_v17():
    assert TABLE_INTRODUCED["monitors"] == 17
    assert TABLE_INTRODUCED["monitor_logs"] == 17
    required = required_tables_for(17)
    assert {"monitors", "monitor_logs"} <= required
    # v16 predates them, so a v16 rollback copy is never asked for these tables.
    assert not ({"monitors", "monitor_logs"} & required_tables_for(16))


async def test_migrating_a_populated_v16_database_adds_monitors_and_keeps_rows(
    monkeypatch,
):
    """The exact step the deployed v16 database takes on next startup."""
    from bot import migrations
    from bot.database import DatabaseManager

    mgr = DatabaseManager(":memory:")
    await mgr.connect()
    try:
        with monkeypatch.context() as patched:
            patched.setattr(migrations, "LATEST_VERSION", 16)
            await migrations.run_migrations(mgr.conn)
        assert await migrations.get_user_version(mgr.conn) == 16

        await mgr.ensure_user(UID, "t", "Test")
        habit_id, _ = await mgr.add_habit(UID, "Meditate")
        await mgr.check_habit(UID, habit_id, date(2026, 8, 9))

        await migrations.run_migrations(mgr.conn)
        assert await migrations.get_user_version(mgr.conn) == 17

        # The pre-existing ledger is untouched...
        assert await mgr.get_checked_habits(UID, date(2026, 8, 9)) == {habit_id}
        # ...and the new section works on it immediately.
        monitor_id, status = await mgr.add_monitor(UID, "Smoking", target_max=0)
        assert status == "added"
        assert await mgr.log_monitor_occurrence(
            UID, monitor_id, date(2026, 8, 10)
        ) is not None
    finally:
        await mgr.close()


async def test_monitor_logs_allow_many_rows_per_day_unlike_habit_logs(db):
    """The schema itself must permit a second occurrence on the same date.

    ``habit_logs`` and ``supplement_logs`` are uniquely keyed per day on purpose.
    If a well-meaning future migration added the same constraint here, counting
    would silently collapse to a checkbox — so the difference is asserted
    against the schema, not only against the handler.
    """
    smoking, _drinking, _cannabis = await _seed(db)
    today = date(2026, 8, 10)
    for _ in range(3):
        assert await db.log_monitor_occurrence(UID, smoking, today) is not None
    entries = await db.get_monitor_day_entries(UID, smoking, today)
    assert len(entries) == 3


# ---------------------------------------------------------------------------
# The pure target module
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "period", "minimum", "maximum"),
    [
        ("zero", "day", None, 0),
        ("none", "day", None, 0),
        ("<=5/day", "day", None, 5),
        ("≤5 per day", "day", None, 5),
        ("max 5/day", "day", None, 5),
        ("1/month", "month", None, 1),
        ("2-4/week", "week", 2, 4),
        ("2 – 4 per week", "week", 2, 4),
        (">=2/week", "week", 2, None),
        ("at least 2 per week", "week", 2, None),
    ],
)
def test_parse_target_accepts_the_shapes_people_write(
    text, period, minimum, maximum
):
    target = parse_target(text)
    assert (target.period, target.minimum, target.maximum) == (
        period,
        minimum,
        maximum,
    )


@pytest.mark.parametrize(
    "text", ["", "sometimes", "5", "/week", "4-2/week", "1000/day", "2-4/fortnight"]
)
def test_parse_target_refuses_what_it_cannot_mean(text):
    with pytest.raises(TargetParseError):
        parse_target(text)


def test_a_backwards_range_names_the_fix():
    with pytest.raises(TargetParseError, match="2-4"):
        parse_target("4-2/week")


@pytest.mark.parametrize(
    ("count", "target", "expected"),
    [
        (0, Target(period="day", maximum=0), "clear"),
        (1, Target(period="day", maximum=0), "over"),
        (1, Target(period="week", minimum=2, maximum=4), "under"),
        (3, Target(period="week", minimum=2, maximum=4), "clear"),
        (5, Target(period="week", minimum=2, maximum=4), "over"),
        (9, Target(), "untargeted"),
    ],
)
def test_evaluate_separates_a_breach_from_a_not_yet(count, target, expected):
    assert evaluate(count, target) == expected


def test_under_a_floor_is_not_flagged_as_a_warning():
    """A range reads "under" for most of its period simply because it is early.

    Sharing the ⚠️ glyph with a real breach would train the reader to ignore it.
    """
    early = format_progress(1, Target(period="week", minimum=2, maximum=4))
    breach = format_progress(5, Target(period="week", minimum=2, maximum=4))
    assert "⚠️" not in early
    assert "⚠️" in breach


def test_progress_names_the_window_not_the_target_twice():
    assert format_progress(3, parse_target("2-4/week")) == "3 this week (2–4) ✅"
    assert format_progress(0, parse_target("zero")) == "0 today (target zero) ✅"
    assert format_progress(2, parse_target("1/month")) == "2 this month (≤1) ⚠️"
    assert format_progress(7, Target()) == "7 today"


def test_period_bounds_end_on_the_day_asked_about():
    """A window never runs into the future: a partial count against a full
    allowance would report Monday as "on track for the week" every week."""
    tuesday = date(2026, 8, 11)
    assert period_bounds("day", tuesday) == (tuesday, tuesday)
    assert period_bounds("week", tuesday) == (date(2026, 8, 10), tuesday)
    assert period_bounds("month", tuesday) == (date(2026, 8, 1), tuesday)


def test_a_week_can_open_in_the_previous_month():
    """1 Aug 2026 is a Saturday, so its week starts in July."""
    start, end = period_bounds("week", date(2026, 8, 1))
    assert start == date(2026, 7, 27)
    assert end == date(2026, 8, 1)


@pytest.mark.parametrize(
    ("text", "emoji", "name"),
    [
        ("🌿 Cannabis", "🌿", "Cannabis"),
        ("Tea", None, "Tea"),
        ("  🚬Smoking ", "🚬", "Smoking"),
        ("(unlabelled)", None, "(unlabelled)"),
        ("3rd coffee", None, "3rd coffee"),
    ],
)
def test_split_emoji_prefix_only_takes_decoration(text, emoji, name):
    assert split_emoji_prefix(text) == (emoji, name)


def test_target_rejects_incoherent_bounds():
    with pytest.raises(ValueError):
        Target(period="week", minimum=4, maximum=2)
    with pytest.raises(ValueError):
        Target(period="fortnight")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Target(period="day", maximum=-1)


def test_monitor_targets_stays_pure():
    """No I/O, no Telegram, no database, no config — enforced, like weight_series.

    The rule is only useful if it cannot quietly break, and the way it breaks is
    somebody importing the database "just to read the target".
    """
    import ast
    import pathlib

    source = pathlib.Path(monitor_targets.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
            if node.level:  # a relative import is by definition in-package
                imported.add("bot")
    assert imported <= {"__future__", "re", "unicodedata", "dataclasses", "datetime", "typing"}


# ---------------------------------------------------------------------------
# Counting, undo, and the period window
# ---------------------------------------------------------------------------
async def test_two_taps_are_two_occurrences_and_undo_removes_one(db):
    smoking, _d, _c = await _seed(db)
    today = date(2026, 8, 10)

    await db.log_monitor_occurrence(UID, smoking, today)
    await db.log_monitor_occurrence(UID, smoking, today)
    assert len(await db.get_monitor_day_entries(UID, smoking, today)) == 2

    assert await db.delete_last_monitor_occurrence(UID, smoking, today) is True
    assert len(await db.get_monitor_day_entries(UID, smoking, today)) == 1
    assert await db.delete_last_monitor_occurrence(UID, smoking, today) is True
    assert await db.delete_last_monitor_occurrence(UID, smoking, today) is False


async def test_undo_cannot_reach_into_an_earlier_day(db):
    smoking, _d, _c = await _seed(db)
    yesterday = date(2026, 8, 9)
    today = date(2026, 8, 10)
    await db.log_monitor_occurrence(UID, smoking, yesterday)

    assert await db.delete_last_monitor_occurrence(UID, smoking, today) is False
    assert len(await db.get_monitor_day_entries(UID, smoking, yesterday)) == 1


async def test_undo_removes_the_newest_row_not_the_oldest(db):
    _s, _d, cannabis = await _seed(db)
    today = date(2026, 8, 10)
    await db.log_monitor_occurrence(UID, cannabis, today, variant="first")
    await db.log_monitor_occurrence(UID, cannabis, today, variant="second")

    await db.delete_last_monitor_occurrence(UID, cannabis, today)
    remaining = await db.get_monitor_day_entries(UID, cannabis, today)
    assert [row["variant"] for row in remaining] == ["first"]


async def test_daily_counts_are_grouped_per_monitor_and_day(db):
    smoking, drinking, _c = await _seed(db)
    await db.log_monitor_occurrence(UID, smoking, date(2026, 8, 9))
    await db.log_monitor_occurrence(UID, smoking, date(2026, 8, 10))
    await db.log_monitor_occurrence(UID, smoking, date(2026, 8, 10))
    await db.log_monitor_occurrence(UID, drinking, date(2026, 8, 3))

    counts = await db.get_monitor_daily_counts(
        UID, date(2026, 8, 1), date(2026, 8, 10)
    )
    assert counts[smoking] == {"2026-08-09": 1, "2026-08-10": 2}
    assert counts[drinking] == {"2026-08-03": 1}


async def test_a_weekly_count_ignores_occurrences_from_last_week(db):
    """The single reason the period lives on the target: a drink on the 3rd is
    inside August but outside the week of the 10th."""
    _s, _d, cannabis = await _seed(db)
    await db.log_monitor_occurrence(UID, cannabis, date(2026, 8, 7))  # prev week
    await db.log_monitor_occurrence(UID, cannabis, date(2026, 8, 10))
    await db.log_monitor_occurrence(UID, cannabis, date(2026, 8, 11))

    counts = await db.get_monitor_daily_counts(
        UID, date(2026, 8, 1), date(2026, 8, 11)
    )
    start, end = period_bounds("week", date(2026, 8, 11))
    in_week = sum(
        value
        for day, value in counts[cannabis].items()
        if start.isoformat() <= day <= end.isoformat()
    )
    assert in_week == 2


async def test_quantity_and_variant_round_trip(db):
    _s, _d, cannabis = await _seed(db)
    today = date(2026, 8, 10)
    await db.log_monitor_occurrence(
        UID, cannabis, today, quantity=0.3, variant="hybrid"
    )
    entry = (await db.get_monitor_day_entries(UID, cannabis, today))[0]
    assert entry["quantity"] == pytest.approx(0.3)
    assert entry["variant"] == "hybrid"
    # The unit defaults from the monitor, so a past entry stays readable after
    # the monitor's unit is later changed.
    assert entry["quantity_unit"] == "g"


async def test_an_explicit_unit_beats_the_monitor_default(db):
    _s, _d, cannabis = await _seed(db)
    today = date(2026, 8, 10)
    await db.log_monitor_occurrence(
        UID, cannabis, today, quantity=2, quantity_unit="joints"
    )
    entry = (await db.get_monitor_day_entries(UID, cannabis, today))[0]
    assert entry["quantity_unit"] == "joints"


async def test_recent_variants_are_distinct_and_newest_first(db):
    _s, _d, cannabis = await _seed(db)
    today = date(2026, 8, 10)
    for variant in ("indica", "hybrid", "indica", "sativa"):
        await db.log_monitor_occurrence(UID, cannabis, today, variant=variant)
    assert await db.get_recent_monitor_variants(UID, cannabis, 3) == [
        "sativa",
        "indica",
        "hybrid",
    ]


# ---------------------------------------------------------------------------
# Ownership and lifecycle
# ---------------------------------------------------------------------------
async def test_logging_against_another_users_monitor_writes_nothing(db):
    smoking, _d, _c = await _seed(db)
    await db.ensure_user(OTHER, "o", "Other")
    today = date(2026, 8, 10)

    assert await db.log_monitor_occurrence(OTHER, smoking, today) is None
    assert await db.get_monitor_day_entries(UID, smoking, today) == []


async def test_logging_against_a_removed_monitor_writes_nothing(db):
    smoking, _d, _c = await _seed(db)
    assert await db.deactivate_monitor(UID, smoking) is True
    assert (
        await db.log_monitor_occurrence(UID, smoking, date(2026, 8, 10)) is None
    )


async def test_removal_is_a_soft_archive_that_keeps_history(db):
    smoking, _d, _c = await _seed(db)
    today = date(2026, 8, 10)
    await db.log_monitor_occurrence(UID, smoking, today)
    await db.deactivate_monitor(UID, smoking)

    assert all(m["id"] != smoking for m in await db.get_active_monitors(UID))
    assert len(await db.get_monitor_day_entries(UID, smoking, today)) == 1

    # The name is free again, and coming back rewrites the target rather than
    # inheriting a limit the user may no longer mean.
    reborn, status = await db.add_monitor(
        UID, "Smoking", target_period="week", target_max=3
    )
    assert (reborn, status) == (smoking, "reactivated")
    row = next(m for m in await db.get_active_monitors(UID) if m["id"] == smoking)
    assert (row["target_period"], row["target_max"]) == ("week", 3)


async def test_adding_an_active_name_twice_is_reported_not_duplicated(db):
    smoking, _d, _c = await _seed(db)
    again, status = await db.add_monitor(UID, "smoking", target_max=0)
    assert (again, status) == (smoking, "already_active")


async def test_set_monitor_target_replaces_intention_without_touching_history(db):
    smoking, _d, _c = await _seed(db)
    today = date(2026, 8, 10)
    await db.log_monitor_occurrence(UID, smoking, today)

    assert await db.set_monitor_target(
        UID, smoking, target_period="week", target_max=2
    )
    row = next(m for m in await db.get_active_monitors(UID) if m["id"] == smoking)
    assert (row["target_period"], row["target_max"]) == ("week", 2)
    assert len(await db.get_monitor_day_entries(UID, smoking, today)) == 1


async def test_another_users_target_change_is_refused(db):
    smoking, _d, _c = await _seed(db)
    await db.ensure_user(OTHER, "o", "Other")
    assert await db.set_monitor_target(OTHER, smoking, target_max=9) is False


# ---------------------------------------------------------------------------
# Typed setup grammar
# ---------------------------------------------------------------------------
def test_parse_monitor_input_reads_the_four_real_monitors():
    smoking = parse_monitor_input("Smoking, zero")
    assert smoking["name"] == "Smoking"
    assert smoking["target"] == Target(period="day", maximum=0)

    drinking = parse_monitor_input("🍺 Drinking, 1/month")
    assert (drinking["emoji"], drinking["name"]) == ("🍺", "Drinking")
    assert drinking["target"] == Target(period="month", maximum=1)

    cannabis = parse_monitor_input("🌿 Cannabis, 2-4/week, q:g, variant")
    assert cannabis["target"] == Target(period="week", minimum=2, maximum=4)
    assert cannabis["tracks_quantity"] is True
    assert cannabis["quantity_unit"] == "g"
    assert cannabis["tracks_variant"] is True

    tea = parse_monitor_input("🍵 Tea, <=5/day")
    assert tea["target"] == Target(period="day", maximum=5)
    assert tea["tracks_quantity"] is False


def test_a_monitor_needs_no_target_at_all():
    parsed = parse_monitor_input("Screens")
    assert parsed["target"] == Target()
    assert parsed["target"].is_open is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Smoking, zero, 2-4/week",
        "Smoking, sometimes",
        "Smoking, zero, q:g, variant, extra",
    ],
)
def test_parse_monitor_input_refuses_what_it_cannot_mean(text):
    with pytest.raises(MonitorInputError):
        parse_monitor_input(text)


def test_an_unreadable_field_is_quoted_back_safely():
    with pytest.raises(MonitorInputError, match="&lt;b&gt;"):
        parse_monitor_input("Smoking, <b>bold</b>")


@pytest.mark.parametrize(
    ("text", "quantity", "unit", "variant"),
    [
        ("0.3 g, hybrid", 0.3, "g", "hybrid"),
        ("hybrid, 0.3 g", 0.3, "g", "hybrid"),
        ("0.3", 0.3, None, None),
        ("hybrid", None, None, "hybrid"),
        ("2 joints", 2.0, "joints", None),
    ],
)
def test_parse_occurrence_detail_takes_the_parts_in_any_order(
    text, quantity, unit, variant
):
    parsed = parse_occurrence_detail(text)
    assert parsed["quantity"] == (None if quantity is None else pytest.approx(quantity))
    assert parsed["quantity_unit"] == unit
    assert parsed["variant"] == variant


@pytest.mark.parametrize("text", ["", "   ", "0 g", "-1 g", "a, b, c", "99999 g"])
def test_parse_occurrence_detail_refuses_nonsense(text):
    with pytest.raises(MonitorInputError):
        parse_occurrence_detail(text)


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------
async def test_the_board_offers_undo_only_once_there_is_something_to_undo(db):
    smoking, _d, _c = await _seed(db)
    active = await db.get_active_monitors(UID)

    quiet = keyboards.monitor_board_keyboard(active, UID, {})
    assert all(
        not any("↩️" in button.text for button in row)
        for row in quiet.inline_keyboard
    )

    busy = keyboards.monitor_board_keyboard(active, UID, {smoking: 1})
    smoking_row = busy.inline_keyboard[0]
    assert any("↩️" in button.text for button in smoking_row)


async def test_only_a_detail_tracking_monitor_gets_the_pencil(db):
    smoking, _drinking, cannabis = await _seed(db)
    active = await db.get_active_monitors(UID)
    board = keyboards.monitor_board_keyboard(active, UID, {})
    by_id = {
        int(row[0].callback_data.split("_")[3]): row for row in board.inline_keyboard
    }
    assert not any("📝" in b.text for b in by_id[smoking])
    assert any("📝" in b.text for b in by_id[cannabis])


def test_a_monitor_tap_rendered_for_one_user_is_inert_for_the_other():
    data = keyboards.monitor_tap_data(UID, "a", 7)
    assert keyboards.parse_monitor_tap(data, UID) == ("a", 7, None)
    assert keyboards.parse_monitor_tap(data, OTHER) is None


def test_variant_shortcuts_travel_as_an_index_never_as_text():
    """Callback data is 64 bytes and a strain name is arbitrary user text."""
    keyboard = keyboards.monitor_variant_keyboard(UID, 7, ["Blue Dream 🌙", "OG"])
    first = keyboard.inline_keyboard[0][0]
    assert "Blue Dream" in first.text
    assert "Blue Dream" not in first.callback_data
    assert keyboards.parse_monitor_tap(first.callback_data, UID) == ("v", 7, 0)
    assert len(first.callback_data.encode()) <= 64


def test_monitor_callbacks_cannot_route_into_a_habit_or_supplement_handler():
    """The whole point of a separate prefix: a counted tap must never reach an
    idempotent checklist writer, or "already ticked" becomes a second cigarette.
    """
    data = keyboards.monitor_tap_data(UID, "a", 7)
    assert not data.startswith("habit_")
    assert not data.startswith("supp_")
    from bot.handlers.habits import _HABIT_ACTION_RE
    from bot.handlers.supplements import _SUPP_ACTION_RE

    assert _HABIT_ACTION_RE.fullmatch(data) is None
    assert _SUPP_ACTION_RE.fullmatch(data) is None


def test_the_setup_done_button_survives_the_tap_parser():
    data = keyboards.monitor_tap_data(UID, "done", 0)
    assert keyboards.parse_monitor_tap(data, UID) == ("done", 0, None)
    # ``^mon_d_`` (open detail) must not also claim ``mon_done_``.
    import re

    assert re.match(r"^mon_d_", data) is None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def test_plus_one_logs_and_refreshes_the_board(db, monkeypatch):
    smoking, _d, _c = await _seed(db)
    today = date(2026, 8, 10)
    monkeypatch.setattr(monitors, "today_local", lambda: today)

    query = _query(keyboards.monitor_tap_data(UID, "a", smoking))
    await monitors.monitor_add_callback(_callback_update(query), _context(db))

    assert len(await db.get_monitor_day_entries(UID, smoking, today)) == 1
    query.edit_message_text.assert_awaited()


async def test_plus_one_from_the_other_user_writes_nothing(db, monkeypatch):
    smoking, _d, _c = await _seed(db)
    today = date(2026, 8, 10)
    monkeypatch.setattr(monitors, "today_local", lambda: today)

    # A board rendered for UID, tapped by OTHER in a shared chat.
    query = _query(keyboards.monitor_tap_data(UID, "a", smoking))
    await monitors.monitor_add_callback(
        _callback_update(query, user_id=OTHER), _context(db)
    )
    assert await db.get_monitor_day_entries(UID, smoking, today) == []


async def test_undo_on_an_already_empty_day_says_so_without_erroring(
    db, monkeypatch
):
    smoking, _d, _c = await _seed(db)
    today = date(2026, 8, 10)
    monkeypatch.setattr(monitors, "today_local", lambda: today)

    query = _query(keyboards.monitor_tap_data(UID, "z", smoking))
    await monitors.monitor_undo_callback(_callback_update(query), _context(db))

    answer = query.answer.await_args.args[0]
    assert "Nothing logged today" in answer


async def test_the_board_reports_each_monitor_against_its_own_window(
    db, monkeypatch
):
    smoking, drinking, cannabis = await _seed(db)
    today = date(2026, 8, 11)  # a Tuesday
    monkeypatch.setattr(monitors, "today_local", lambda: today)

    await db.log_monitor_occurrence(UID, drinking, date(2026, 8, 2))  # this month
    await db.log_monitor_occurrence(UID, cannabis, date(2026, 8, 7))  # last week
    await db.log_monitor_occurrence(UID, cannabis, date(2026, 8, 10))
    await db.log_monitor_occurrence(UID, cannabis, date(2026, 8, 11))

    text, _keyboard = await monitors._board_view(db, UID, today)
    assert "0 today (target zero) ✅" in text        # smoking, day window
    assert "1 this month (≤1) ✅" in text            # drinking, month window
    assert "2 this week (2–4) ✅" in text            # cannabis, week window only


async def test_the_board_shows_the_days_quantity_and_variant(db, monkeypatch):
    _s, _d, cannabis = await _seed(db)
    today = date(2026, 8, 10)
    monkeypatch.setattr(monitors, "today_local", lambda: today)
    await db.log_monitor_occurrence(
        UID, cannabis, today, quantity=0.3, variant="hybrid"
    )

    text, _keyboard = await monitors._board_view(db, UID, today)
    assert "0.3 g · hybrid" in text


async def test_an_empty_board_explains_what_a_monitor_is(db):
    await db.ensure_user(UID, "t", "Test")
    text, keyboard = await monitors._board_view(db, UID, date(2026, 8, 10))
    assert "/monitor setup" in text
    assert keyboard is None


async def test_the_schema_itself_refuses_an_impossible_target(db):
    """The CHECK constraints are the first line, not the handler's guard."""
    import sqlite3

    smoking, _d, _c = await _seed(db)
    for column, value in (("target_period", "fortnight"), ("target_min", -1)):
        with pytest.raises(sqlite3.IntegrityError):
            async with db._write_operation():
                await db.conn.execute(
                    f"UPDATE monitors SET {column} = ? WHERE id = ?",  # noqa: S608
                    (value, smoking),
                )
    with pytest.raises(sqlite3.IntegrityError):
        async with db._write_operation():
            await db.conn.execute(
                "UPDATE monitors SET target_min = 4, target_max = 2 WHERE id = ?",
                (smoking,),
            )


def test_a_row_whose_target_cannot_be_read_degrades_to_merely_counted():
    """Belt to the schema's braces.

    The CHECK constraints make this unreachable through the database — but they
    can be suspended (``PRAGMA ignore_check_constraints``) and a future migration
    could rebuild the table without them. A board that raises is worse than a
    board that reports the count and claims nothing about a limit it can no
    longer interpret.
    """
    degraded = monitors._target_of(
        {"id": 1, "target_period": "fortnight", "target_min": None, "target_max": 0}
    )
    assert degraded.is_open is True
    assert format_progress(2, degraded) == "2 today"


async def test_setup_adds_a_monitor_from_a_typed_line(db, monkeypatch):
    await db.ensure_user(UID, "t", "Test")
    context = _context(db)
    update = _message_update("🌿 Cannabis, 2-4/week, q:g, variant")

    await monitors.add_monitor_text(update, context)

    active = await db.get_active_monitors(UID)
    assert [m["name"] for m in active] == ["Cannabis"]
    assert active[0]["emoji"] == "🌿"
    assert (active[0]["target_min"], active[0]["target_max"]) == (2, 4)
    assert active[0]["tracks_quantity"] == 1
    assert active[0]["tracks_variant"] == 1


async def test_setup_refuses_to_pass_the_active_limit(db, monkeypatch):
    await db.ensure_user(UID, "t", "Test")
    for index in range(MAX_ACTIVE_MONITORS):
        await db.add_monitor(UID, f"Thing {index}")

    context = _context(db)
    update = _message_update("One too many")
    await monitors.add_monitor_text(update, context)

    assert len(await db.get_active_monitors(UID)) == MAX_ACTIVE_MONITORS
    said = update.message.reply_text.await_args.args[0]
    assert str(MAX_ACTIVE_MONITORS) in said


async def test_the_detail_prompt_logs_a_typed_occurrence(db, monkeypatch):
    _s, _d, cannabis = await _seed(db)
    today = date(2026, 8, 10)
    monkeypatch.setattr(monitors, "today_local", lambda: today)

    context = _context(db, user_data={"monitor_detail_id": cannabis})
    await monitors.log_detail_text(_message_update("0.3 g, hybrid"), context)

    entry = (await db.get_monitor_day_entries(UID, cannabis, today))[0]
    assert (entry["quantity"], entry["variant"]) == (pytest.approx(0.3), "hybrid")
    assert "monitor_detail_id" not in context.user_data


async def test_an_expired_detail_prompt_logs_nothing(db):
    _s, _d, cannabis = await _seed(db)
    context = _context(db)  # no monitor_detail_id
    update = _message_update("0.3 g, hybrid")

    await monitors.log_detail_text(update, context)

    assert await db.get_monitor_day_entries(UID, cannabis, date(2026, 8, 10)) == []
    assert "expired" in update.message.reply_text.await_args.args[0]


async def test_a_stale_variant_shortcut_is_refused_rather_than_guessed(
    db, monkeypatch
):
    _s, _d, cannabis = await _seed(db)
    today = date(2026, 8, 10)
    monkeypatch.setattr(monitors, "today_local", lambda: today)

    query = _query(keyboards.monitor_tap_data(UID, "v", cannabis, 2))
    await monitors.monitor_variant_callback(_callback_update(query), _context(db))

    assert await db.get_monitor_day_entries(UID, cannabis, today) == []
    assert "out of date" in query.answer.await_args.args[0]


async def test_home_offers_monitors_and_routes_to_the_board():
    from bot.handlers import start

    labels = [
        button.callback_data
        for row in keyboards.main_menu_keyboard().inline_keyboard
        for button in row
    ]
    assert "menu_monitors" in labels
    # Routed by menu_callback rather than by a conversation entry point.
    assert "menu_monitors" not in start._CONVERSATION_MENU_ACTIONS
