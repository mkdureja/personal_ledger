"""Release 2 — supplements as their own section.

Three properties carry most of the weight:

1. **Adherence is not nutrition.** A supplement has no calorie or macro path. The
   strongest form of that claim is structural, so it is asserted against the
   schema itself rather than against one handler's output.
2. **The habit patterns transfer.** Ownership is embedded in every callback, the
   writable window is today/yesterday, and a stale keyboard is inert. These are
   the same guarantees habits already prove, restated for the new prefix family
   so a regression here cannot hide behind the habit tests.
3. **Dose and timing survive the round trip** — through parsing, storage, and the
   rendered button label — because carrying them is the entire reason this is a
   separate section instead of a habit.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType

from bot import keyboards
from bot.config import today_local
from bot.database import MAX_ACTIVE_SUPPLEMENTS, MAX_DOSE_AMOUNT
from bot.handlers import supplements
from bot.handlers.supplements import (
    SupplementInputError,
    parse_supplement_input,
)
from ledger_schema import TABLE_INTRODUCED, required_tables_for

# No module-level asyncio mark: pytest.ini sets asyncio_mode = auto, and an
# explicit mark would be applied to this file's synchronous tests as well.

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
    )


def _query(data: str, message=None):
    return SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=message
        or SimpleNamespace(
            chat_id=UID,
            message_id=1,
            reply_text=AsyncMock(),
            edit_text=AsyncMock(),
        ),
        edit_message_reply_markup=AsyncMock(),
        edit_message_text=AsyncMock(),
    )


def _callback_update(query, user_id: int = UID):
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id, first_name="Test", username="t"),
        effective_chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
        update_id=1,
    )


def _message_update(text: str, user_id: int = UID):
    message = SimpleNamespace(
        text=text,
        chat_id=user_id,
        message_id=100,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=100)),
    )
    return SimpleNamespace(
        effective_message=message,
        message=message,
        effective_user=SimpleNamespace(id=user_id, first_name="Test", username="t"),
        effective_chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
        callback_query=None,
        update_id=1,
    )


async def _toggle_row(db, supplement_id: int, on_date, user_id: int = UID):
    """Build the real checklist keyboard and return its first habit-row button."""
    active = await db.get_active_supplements(user_id)
    taken = await db.get_taken_supplements(user_id, on_date)
    return keyboards.supplement_checklist_keyboard(
        active, taken, on_date, user_id
    ).inline_keyboard[0][0]


# ---------------------------------------------------------------------------
# Adherence is structurally separate from nutrition
# ---------------------------------------------------------------------------
def test_supplement_tables_are_introduced_together_at_v9():
    assert TABLE_INTRODUCED["supplements"] == 9
    assert TABLE_INTRODUCED["supplement_logs"] == 9
    required = required_tables_for(9)
    assert {"supplements", "supplement_logs"} <= required
    # v8 predates them, so a v8 rollback copy is never asked for these tables.
    assert not ({"supplements", "supplement_logs"} & required_tables_for(8))


async def test_migrating_a_populated_v8_database_adds_supplements_and_keeps_rows(
    monkeypatch,
):
    """The exact step the deployed v8 database will take on next startup.

    Stopping at 8, writing real rows, then stepping to 9 proves the new tables
    arrive on an existing ledger rather than only on the fresh database the rest
    of the suite builds.
    """
    from bot import migrations
    from bot.database import DatabaseManager

    mgr = DatabaseManager(":memory:")
    await mgr.connect()
    try:
        # Migrate to 8 only, by pinning the runner's target for this step.
        # monkeypatch (not a manual save/restore) guarantees the global is put
        # back even if an assertion below fails — a leaked LATEST_VERSION would
        # silently mis-target every later test in the session.
        with monkeypatch.context() as patched:
            patched.setattr(migrations, "LATEST_VERSION", 8)
            await migrations.run_migrations(mgr.conn)
        assert await migrations.get_user_version(mgr.conn) == 8

        await mgr.ensure_user(UID, "t", "Test")
        habit_id, _ = await mgr.add_habit(UID, "Read")
        await mgr.check_habit(UID, habit_id, today_local())

        await migrations.run_migrations(mgr.conn)

        assert await migrations.get_user_version(mgr.conn) == 9
        cursor = await mgr.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        tables = {row["name"] for row in await cursor.fetchall()}
        assert {"supplements", "supplement_logs"} <= tables

        # Pre-existing data is untouched, and the new section starts empty.
        assert await mgr.get_checked_habits(UID, today_local()) == {habit_id}
        assert await mgr.get_active_supplements(UID) == []

        cursor = await mgr.conn.execute("PRAGMA foreign_key_check")
        assert await cursor.fetchall() == []
    finally:
        await mgr.close()


async def test_a_supplement_carries_no_nutrition_columns(db):
    """The 'never affects calories' rule, asserted where it cannot be bypassed.

    A handler could be careful and still leak nutrition later. If the columns do
    not exist, no future code path can quietly start summing them.
    """
    rows = await db._query_all("PRAGMA table_info(supplements)")
    columns = {row["name"] for row in rows}
    assert not (
        columns
        & {"calories", "protein_g", "carbs_g", "fat_g", "basis_amount", "base_unit"}
    )
    assert {"dose_amount", "dose_unit", "timing"} <= columns


async def test_taking_a_supplement_does_not_touch_meals_or_calories(db):
    await db.ensure_user(UID, "t", "Test")
    sid, _ = await db.add_supplement(UID, "Vitamin D3", dose_amount=2, dose_unit="caps")
    today = today_local()

    await db.take_supplement(UID, sid, today)

    assert await db.get_today_meal_count(UID, today) == 0
    assert await db.get_today_calories(UID, today) == (0, False)


# ---------------------------------------------------------------------------
# Typed setup grammar
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        ("Magnesium", ("Magnesium", None, None, None)),
        ("Vitamin D3, 2 capsules", ("Vitamin D3", 2.0, "capsules", None)),
        # A second field with no leading number is timing, not a nameless dose.
        ("Vitamin D3, morning", ("Vitamin D3", None, None, "morning")),
        (
            "Omega 3, 1000 mg, with dinner",
            ("Omega 3", 1000.0, "mg", "with dinner"),
        ),
        ("Zinc, 0.5 tablet", ("Zinc", 0.5, "tablet", None)),
        # A bare number is a dose with no unit rather than an error.
        ("Creatine, 5", ("Creatine", 5.0, None, None)),
    ],
)
def test_parse_supplement_input_accepts_the_documented_shapes(text, expected):
    fields = parse_supplement_input(text)
    assert (
        fields["name"],
        fields["dose_amount"],
        fields["dose_unit"],
        fields["timing"],
    ) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        ", 2 capsules",
        "A, b, c, d",
        f"Too big, {MAX_DOSE_AMOUNT * 2:g} mg",
        "Zero, 0 mg",
        "Negative, -1 mg",
        "X" * 51,
    ],
)
def test_parse_supplement_input_rejects_bad_input_with_a_user_message(text):
    with pytest.raises(SupplementInputError) as excinfo:
        parse_supplement_input(text)
    assert str(excinfo.value)


@pytest.mark.parametrize("field", ["-1 mg", "+0 mg", "-2", "0"])
def test_a_failed_dose_is_never_reinterpreted_as_timing(field):
    """A malformed dose must be refused, not quietly filed under timing.

    ``Vitamin D3, -1 mg`` originally fell through to the timing branch, because
    the dose pattern rejects the minus sign — so nonsense was stored as if the
    user had asked for it. Anything opening like a number is a dose attempt and
    has to pass dose validation.
    """
    with pytest.raises(SupplementInputError):
        parse_supplement_input(f"Vitamin D3, {field}")


def test_a_leading_decimal_dose_is_accepted():
    fields = parse_supplement_input("Zinc, .5 tablet")
    assert (fields["dose_amount"], fields["dose_unit"]) == (0.5, "tablet")


def test_dose_label_renders_only_the_parts_that_exist():
    assert keyboards.supplement_dose_label({}) == ""
    assert (
        keyboards.supplement_dose_label({"dose_amount": 2.0, "dose_unit": "capsules"})
        == " · 2 capsules"
    )
    assert keyboards.supplement_dose_label({"timing": "morning"}) == " · morning"
    assert (
        keyboards.supplement_dose_label(
            {"dose_amount": 1000.0, "dose_unit": "mg", "timing": "with dinner"}
        )
        == " · 1000 mg, with dinner"
    )


# ---------------------------------------------------------------------------
# Storage: dose round-trip, reactivation, bounds
# ---------------------------------------------------------------------------
async def test_dose_and_timing_survive_storage_and_reach_the_button(db):
    await db.ensure_user(UID, "t", "Test")
    await db.add_supplement(
        UID, "Omega 3", dose_amount=1000, dose_unit="mg", timing="with dinner"
    )

    row = await _toggle_row(db, None, today_local())
    assert row.text == "⬜ Omega 3 · 1000 mg, with dinner"


async def test_adding_the_same_name_twice_is_idempotent(db):
    await db.ensure_user(UID, "t", "Test")
    first, status = await db.add_supplement(UID, "Zinc")
    assert status == "added"
    second, status = await db.add_supplement(UID, "  zinc  ")
    assert (second, status) == (first, "already_active")
    assert len(await db.get_active_supplements(UID)) == 1


async def test_reactivating_refreshes_the_dose_and_keeps_history(db):
    await db.ensure_user(UID, "t", "Test")
    sid, _ = await db.add_supplement(UID, "Iron", dose_amount=1, dose_unit="tablet")
    yesterday = today_local() - timedelta(days=1)
    await db.take_supplement(UID, sid, yesterday)

    assert await db.deactivate_supplement(UID, sid)
    assert await db.get_active_supplements(UID) == []

    again, status = await db.add_supplement(UID, "Iron", dose_amount=2, dose_unit="tablets")
    assert (again, status) == (sid, "reactivated")
    active = await db.get_active_supplements(UID)
    assert (active[0]["dose_amount"], active[0]["dose_unit"]) == (2.0, "tablets")
    # Deactivation is an archive: the earlier adherence row is still there.
    assert await db.get_taken_supplements(UID, yesterday) == {sid}


async def test_a_second_tap_on_the_same_day_records_nothing_extra(db):
    await db.ensure_user(UID, "t", "Test")
    sid, _ = await db.add_supplement(UID, "Zinc")
    today = today_local()

    assert await db.take_supplement(UID, sid, today) is True
    assert await db.take_supplement(UID, sid, today) is False

    rows = await db.get_supplement_logs_range(UID, today, today)
    assert len(rows) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"dose_amount": 0},
        {"dose_amount": -1},
        {"dose_amount": MAX_DOSE_AMOUNT * 2},
        {"dose_amount": float("inf")},
        {"dose_unit": "u" * 31},
        {"timing": "t" * 31},
    ],
)
async def test_out_of_bounds_dose_fields_are_refused_at_the_database(db, kwargs):
    await db.ensure_user(UID, "t", "Test")
    with pytest.raises(ValueError):
        await db.add_supplement(UID, "Bad", **kwargs)
    assert await db.get_active_supplements(UID) == []


async def test_streak_counts_consecutive_days_and_stops_at_a_gap(db):
    await db.ensure_user(UID, "t", "Test")
    sid, _ = await db.add_supplement(UID, "Zinc")
    today = today_local()
    for offset in (0, 1, 2, 4):  # a deliberate gap at 3
        await db.take_supplement(UID, sid, today - timedelta(days=offset))

    assert await db.get_supplement_streak(UID, sid, today) == 3


async def test_an_unrecorded_today_means_no_streak(db):
    await db.ensure_user(UID, "t", "Test")
    sid, _ = await db.add_supplement(UID, "Zinc")
    await db.take_supplement(UID, sid, today_local() - timedelta(days=1))

    assert await db.get_supplement_streak(UID, sid, today_local()) == 0


# ---------------------------------------------------------------------------
# Two-user isolation and stale callbacks
# ---------------------------------------------------------------------------
async def test_tapping_a_supplement_records_only_the_acting_users_day(db):
    await db.ensure_user(UID, "t", "Test")
    await db.ensure_user(OTHER, "o", "Other")
    mine, _ = await db.add_supplement(UID, "Zinc")
    theirs, _ = await db.add_supplement(OTHER, "Zinc")
    today = today_local()

    row = await _toggle_row(db, mine, today)
    query = _query(row.callback_data)
    await supplements.supplement_take_callback(_callback_update(query), _context(db))

    assert await db.get_taken_supplements(UID, today) == {mine}
    assert await db.get_taken_supplements(OTHER, today) == set()
    assert theirs not in await db.get_taken_supplements(UID, today)


async def test_another_users_callback_cannot_write_or_clear_the_keyboard(db):
    await db.ensure_user(UID, "t", "Test")
    await db.ensure_user(OTHER, "o", "Other")
    theirs, _ = await db.add_supplement(OTHER, "Zinc")
    today = today_local()

    # A keyboard minted for OTHER, tapped by UID.
    query = _query(f"supp_c_{OTHER}_{theirs}_{today.isoformat()}")
    await supplements.supplement_take_callback(_callback_update(query), _context(db))

    assert await db.get_taken_supplements(OTHER, today) == set()
    assert await db.get_taken_supplements(UID, today) == set()
    query.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.parametrize("days_ago", [2, 7, 400])
async def test_a_date_outside_the_window_is_refused_without_writing(db, days_ago):
    await db.ensure_user(UID, "t", "Test")
    sid, _ = await db.add_supplement(UID, "Zinc")
    stale = today_local() - timedelta(days=days_ago)

    query = _query(f"supp_c_{UID}_{sid}_{stale.isoformat()}")
    await supplements.supplement_take_callback(_callback_update(query), _context(db))

    assert await db.get_supplement_logs_range(UID, stale, today_local()) == []
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_yesterday_remains_writable(db):
    await db.ensure_user(UID, "t", "Test")
    sid, _ = await db.add_supplement(UID, "Zinc")
    yesterday = today_local() - timedelta(days=1)

    query = _query(f"supp_c_{UID}_{sid}_{yesterday.isoformat()}")
    await supplements.supplement_take_callback(_callback_update(query), _context(db))

    assert await db.get_taken_supplements(UID, yesterday) == {sid}


async def test_a_deactivated_supplement_cannot_be_checked_off(db):
    await db.ensure_user(UID, "t", "Test")
    sid, _ = await db.add_supplement(UID, "Zinc")
    today = today_local()
    row = await _toggle_row(db, sid, today)
    await db.deactivate_supplement(UID, sid)

    query = _query(row.callback_data)
    await supplements.supplement_take_callback(_callback_update(query), _context(db))

    assert await db.get_taken_supplements(UID, today) == set()


async def test_the_noop_label_never_writes(db):
    await db.ensure_user(UID, "t", "Test")
    sid, _ = await db.add_supplement(UID, "Zinc")

    query = _query(f"supp_noop_{UID}_date")
    await supplements.supplement_noop_callback(_callback_update(query), _context(db))
    query.answer.assert_awaited_once()

    label = _query(f"supp_noop_{UID}_{sid}")
    await supplements.supplement_noop_callback(_callback_update(label), _context(db))
    assert "refresh" in label.answer.call_args.args[0]

    assert await db.get_taken_supplements(UID, today_local()) == set()


async def test_untake_removes_only_the_targeted_day(db):
    await db.ensure_user(UID, "t", "Test")
    sid, _ = await db.add_supplement(UID, "Zinc")
    today = today_local()
    yesterday = today - timedelta(days=1)
    await db.take_supplement(UID, sid, today)
    await db.take_supplement(UID, sid, yesterday)

    query = _query(f"supp_u_{UID}_{sid}_{today.isoformat()}")
    await supplements.supplement_untake_callback(_callback_update(query), _context(db))

    assert await db.get_taken_supplements(UID, today) == set()
    assert await db.get_taken_supplements(UID, yesterday) == {sid}


async def test_a_taken_row_renders_as_untake_and_back(db):
    """The button always carries the action opposite to current state."""
    await db.ensure_user(UID, "t", "Test")
    sid, _ = await db.add_supplement(UID, "Zinc")
    today = today_local()

    assert (await _toggle_row(db, sid, today)).callback_data.startswith("supp_c_")
    await db.take_supplement(UID, sid, today)
    assert (await _toggle_row(db, sid, today)).callback_data.startswith("supp_u_")


# ---------------------------------------------------------------------------
# Setup flow
# ---------------------------------------------------------------------------
async def test_typed_setup_stores_every_field(db):
    await db.ensure_user(UID, "t", "Test")
    update = _message_update("Omega 3, 1000 mg, with dinner")

    await supplements.add_supplement_text(update, _context(db))

    active = await db.get_active_supplements(UID)
    assert len(active) == 1
    assert active[0]["name"] == "Omega 3"
    assert active[0]["dose_amount"] == 1000.0
    assert active[0]["dose_unit"] == "mg"
    assert active[0]["timing"] == "with dinner"


async def test_bad_typed_setup_explains_itself_and_stores_nothing(db):
    await db.ensure_user(UID, "t", "Test")
    update = _message_update("A, b, c, d")

    await supplements.add_supplement_text(update, _context(db))

    assert await db.get_active_supplements(UID) == []
    assert "❌" in update.message.reply_text.call_args.args[0]


async def test_the_active_supplement_limit_is_enforced(db, monkeypatch):
    await db.ensure_user(UID, "t", "Test")
    monkeypatch.setattr(supplements, "MAX_ACTIVE_SUPPLEMENTS", 2)
    for name in ("A", "B"):
        await db.add_supplement(UID, name)

    await supplements.add_supplement_text(_message_update("C"), _context(db))

    assert {row["name"] for row in await db.get_active_supplements(UID)} == {"A", "B"}


def test_the_limit_leaves_room_for_the_checklist_control_rows():
    """Pagination assumes a bounded list; keep the cap aligned with habits."""
    assert MAX_ACTIVE_SUPPLEMENTS == keyboards.MAX_ACTIVE_HABITS


async def test_remove_requires_the_live_setup_prompt(db):
    await db.ensure_user(UID, "t", "Test")
    sid, _ = await db.add_supplement(UID, "Zinc")

    # No active setup conversation: the button must not deactivate anything.
    query = _query(f"supp_remove_{UID}_{sid}")
    await supplements.remove_supplement_callback(
        _callback_update(query), _context(db)
    )

    assert len(await db.get_active_supplements(UID)) == 1


# ---------------------------------------------------------------------------
# The two checklists cannot route into each other
# ---------------------------------------------------------------------------
async def test_a_habit_callback_is_rejected_by_the_supplement_handler(db):
    await db.ensure_user(UID, "t", "Test")
    sid, _ = await db.add_supplement(UID, "Zinc")
    today = today_local()

    query = _query(f"habit_c_{UID}_{sid}_{today.isoformat()}")
    await supplements.supplement_take_callback(_callback_update(query), _context(db))

    assert await db.get_taken_supplements(UID, today) == set()


async def test_supplement_keyboards_only_emit_the_supp_prefix(db):
    await db.ensure_user(UID, "t", "Test")
    await db.add_supplement(UID, "Zinc", dose_amount=1, dose_unit="tablet")
    active = await db.get_active_supplements(UID)

    markup = keyboards.supplement_checklist_keyboard(
        active, set(), today_local(), UID
    )
    setup = keyboards.supplement_setup_keyboard(active, UID)

    for keyboard in (markup, setup):
        for row in keyboard.inline_keyboard:
            for button in row:
                assert button.callback_data.startswith("supp_")
                # Telegram's hard limit; a long dose label must not push past it.
                assert len(button.callback_data.encode("utf-8")) <= 64
