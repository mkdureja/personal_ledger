"""/meals — a day's food, item by item.

What must hold:

* **The day is the local day.** Meals are stored as UTC instants and read back
  by local calendar date, so a late-night meal has to land on the day it was
  eaten and on no other. Getting this wrong is the one error that would make
  the view actively misleading rather than merely incomplete.
* **Unknown is not zero.** Macros are optional on a meal; a day containing one
  reports its totals as known values rather than quietly summing a hole to a
  smaller number.
* **It reads any day, and only days that exist.** Reading is harmless, so there
  is no today/yesterday window — but a future date is refused, because an empty
  answer there would read as lost data.
* **It never writes.** Every button on it is a navigation payload, owner-scoped
  like every other family in this bot.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import keyboards, meal_day
from bot.config import today_local
from bot.handlers import meals

UID = 123456789  # matches conftest ALLOWED_USER_IDS
OTHER = 987654321


# ---------------------------------------------------------------------------
# The date grammar (pure)
# ---------------------------------------------------------------------------
TODAY = date(2026, 8, 13)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", TODAY),
        ("today", TODAY),
        ("  Today ", TODAY),
        ("yesterday", date(2026, 8, 12)),
        ("2026-08-11", date(2026, 8, 11)),
        ("11 aug", date(2026, 8, 11)),
        ("aug 11", date(2026, 8, 11)),
        ("11 August", date(2026, 8, 11)),
    ],
)
def test_the_day_grammar_accepts_what_people_type(text, expected):
    assert meal_day.parse_day(text, TODAY) == expected


def test_a_month_that_has_not_come_round_yet_means_last_year():
    """December read in January is last December, not eleven months away."""
    assert meal_day.parse_day("25 dec", date(2027, 1, 3)) == date(2026, 12, 25)


@pytest.mark.parametrize("text", ["11", "banana", "32 aug", "2026-13-01", "11 xyz"])
def test_an_unreadable_day_is_refused_rather_than_guessed(text):
    with pytest.raises(meal_day.DayInputError):
        meal_day.parse_day(text, TODAY)


def test_a_future_day_is_refused():
    with pytest.raises(meal_day.DayInputError):
        meal_day.parse_day("2026-08-14", TODAY)


# ---------------------------------------------------------------------------
# Totals and layout (pure)
# ---------------------------------------------------------------------------
def _meal(**overrides):
    row = {
        "id": 1,
        "meal_type": "lunch",
        "food_items": "100 g Skyr",
        "calories": 101,
        "protein_g": 11.0,
        "carbs_g": 9.5,
        "fat_g": 2.1,
        "logged_at": "2026-08-13 07:37:09",
    }
    row.update(overrides)
    return row


def test_totals_add_up_and_report_full_coverage():
    totals = meal_day.day_totals([_meal(), _meal(id=2, calories=99, protein_g=1.0)])
    assert totals.calories == 200
    assert totals.protein_g == 12.0
    assert totals.missing == frozenset()
    assert "known values" not in meal_day.totals_line(totals)


def test_a_missing_macro_is_never_summed_as_zero():
    totals = meal_day.day_totals([_meal(), _meal(id=2, protein_g=None)])
    assert totals.protein_g == 11.0
    assert totals.missing == frozenset({"protein_g"})
    assert "known values" in meal_day.totals_line(totals)


def test_an_item_without_macros_renders_a_dash_not_a_zero():
    block = meal_day.meal_block(
        _meal(),
        [{"display_name": "1 samosa", "calories": 260, "protein_g": None,
          "carbs_g": None, "fat_g": None}],
        local_time="13:07",
        escape=lambda value: str(value),
    )
    assert "260 cal · P — · C — · F —" in block


def test_a_meal_names_every_item_and_what_it_cost():
    block = meal_day.meal_block(
        _meal(calories=399, protein_g=40.8, carbs_g=52.4, fat_g=4.5,
              meal_type="breakfast"),
        [
            {"display_name": "1 scoop Cosmix protein", "calories": 142,
             "protein_g": 23, "carbs_g": 11, "fat_g": 1},
            {"display_name": "150 g Skyr", "calories": 152,
             "protein_g": 16.5, "carbs_g": 14.25, "fat_g": 3.15},
        ],
        local_time="08:10",
        escape=lambda value: str(value),
    )
    assert "🌅 <b>Breakfast</b> · 08:10 — 399 cal" in block
    assert " · 1 scoop Cosmix protein" in block
    assert "   142 cal · P 23 · C 11 · F 1" in block
    assert " · 150 g Skyr" in block


def test_a_meal_with_no_stored_items_still_shows_what_it_was():
    """A meal predating structured items must not vanish from its own day."""
    block = meal_day.meal_block(
        _meal(food_items="dal chawal"),
        [],
        local_time="13:07",
        escape=lambda value: str(value),
    )
    assert "dal chawal" in block


def test_an_empty_day_says_so_without_pretending_to_totals():
    heading = meal_day.day_heading(TODAY, meal_day.day_totals([]), 0)
    assert "Nothing logged" in heading
    assert "cal" not in heading


def test_the_heading_counts_meals_and_leads_with_the_date():
    heading = meal_day.day_heading(TODAY, meal_day.day_totals([_meal()]), 1)
    assert heading.startswith("🍽️ <b>Thu 13 Aug 2026</b> — 1 meal")


def test_the_module_stays_free_of_telegram_and_the_database():
    """The layout is testable because it depends on nothing that needs a bot."""
    import ast
    import pathlib

    source = pathlib.Path(meal_day.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"telegram", "aiosqlite", "bot"}


# ---------------------------------------------------------------------------
# The keyboard
# ---------------------------------------------------------------------------
def test_the_day_payload_survives_the_round_trip():
    data = keyboards.meal_day_data(UID, date(2026, 8, 11))

    assert keyboards.parse_meal_day(data, UID) == date(2026, 8, 11)
    assert keyboards.parse_meal_day(data, OTHER) is None


@pytest.mark.parametrize(
    "data",
    ["", "mday", "mday_21i3v9", "mday_21i3v9_20260899", "mday_21i3v9_2026081",
     "meal_21i3v9_20260811"],
)
def test_a_malformed_day_payload_is_rejected(data):
    assert keyboards.parse_meal_day(data, UID) is None


def test_today_has_no_forward_button():
    """The day after today holds nothing; a button for it invites the wrong read."""
    markup = keyboards.meal_day_keyboard(UID, TODAY, has_next=False)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert len(labels) == 1
    assert labels[0].startswith("◀️")

    past = keyboards.meal_day_keyboard(UID, date(2026, 8, 11), has_next=True)
    assert len(past.inline_keyboard[0]) == 2


# ---------------------------------------------------------------------------
# The handler, against a real database
# ---------------------------------------------------------------------------
def _message():
    return SimpleNamespace(
        message_id=7,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=8)),
    )


def _context(db):
    return SimpleNamespace(bot_data={"db": db}, user_data={}, args=[])


def _update(message, args=()):
    return SimpleNamespace(
        effective_message=message,
        message=message,
        effective_user=SimpleNamespace(id=UID, username="t", first_name="T"),
        effective_chat=SimpleNamespace(id=UID, type="private"),
        callback_query=None,
    )


def _texts(message):
    return [call.args[0] for call in message.reply_text.call_args_list]


async def _log(db, meal_type: str, *items):
    return await db.log_diet_with_items(UID, meal_type, list(items))


def _item(name: str, calories: int, **overrides):
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
        "calories": calories,
        "protein_g": 1.0,
        "carbs_g": 2.0,
        "fat_g": 3.0,
    }
    item.update(overrides)
    return item


async def test_the_day_lists_every_item_of_every_meal(db_with_user, user_id):
    await _log(db_with_user, "breakfast", _item("Oats", 380), _item("Banana", 105))
    await _log(db_with_user, "lunch", _item("Dal chawal", 520))

    message = _message()
    context = _context(db_with_user)
    context.args = []
    await meals.meals_command(_update(message), context)

    body = "\n".join(_texts(message))
    for name in ("Oats", "Banana", "Dal chawal"):
        assert name in body
    assert "3 meals" not in body  # two meals, three items
    assert "2 meals" in body
    assert "1005 cal" in body


async def test_a_day_with_nothing_logged_says_so(db_with_user, user_id):
    message = _message()
    context = _context(db_with_user)
    context.args = ["2026-01-02"]
    await meals.meals_command(_update(message), context)

    assert "Nothing logged" in "\n".join(_texts(message))


async def test_an_unreadable_day_is_answered_not_swallowed(db_with_user, user_id):
    message = _message()
    context = _context(db_with_user)
    context.args = ["banana"]
    await meals.meals_command(_update(message), context)

    assert "❌" in _texts(message)[0]


async def test_yesterdays_meal_does_not_leak_into_today(db_with_user, user_id):
    """The local-date filter is the whole correctness claim of this view."""
    await _log(db_with_user, "dinner", _item("Late dinner", 700))
    # Move it back a day, keeping the same wall-clock time.
    async with db_with_user._write_operation():
        await db_with_user.conn.execute(
            "UPDATE diet_logs SET logged_at = datetime(logged_at, '-1 day')"
        )

    message = _message()
    context = _context(db_with_user)
    context.args = []
    await meals.meals_command(_update(message), context)
    assert "Late dinner" not in "\n".join(_texts(message))

    yesterday = _message()
    context = _context(db_with_user)
    context.args = ["yesterday"]
    await meals.meals_command(_update(yesterday), context)
    assert "Late dinner" in "\n".join(_texts(yesterday))


async def test_the_keyboard_steps_to_the_day_before(db_with_user, user_id):
    message = _message()
    context = _context(db_with_user)
    context.args = []
    await meals.meals_command(_update(message), context)

    markup = message.reply_text.call_args.kwargs["reply_markup"]
    day = keyboards.parse_meal_day(
        markup.inline_keyboard[0][0].callback_data, user_id
    )
    assert day == today_local() - timedelta(days=1)


async def test_another_users_day_button_reads_nothing(db_with_user, user_id):
    """A borrowed payload must not render one person's day to another."""
    await _log(db_with_user, "lunch", _item("Dal chawal", 520))
    query = SimpleNamespace(
        data=keyboards.meal_day_data(user_id, today_local()),
        answer=AsyncMock(),
        message=_message(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=OTHER, username="o", first_name="O"),
        effective_chat=SimpleNamespace(id=OTHER, type="private"),
    )

    await meals.meal_day_callback(update, _context(db_with_user))

    assert not query.message.reply_text.await_count


async def test_a_day_payload_for_someone_else_does_not_decode(db_with_user, user_id):
    """The owner check that would refuse a *permitted* second household member."""
    data = keyboards.meal_day_data(user_id, today_local())

    assert keyboards.parse_meal_day(data, OTHER) is None


async def test_items_are_read_in_one_query(db_with_user, user_id):
    """A day view renders whole; it must not cost a round trip per meal."""
    first = await _log(db_with_user, "breakfast", _item("Oats", 380))
    second = await _log(db_with_user, "lunch", _item("Dal", 200))

    grouped = await db_with_user.get_diet_items_for_meals(user_id, [first, second])

    assert {row["display_name"] for row in grouped[first]} == {"Oats"}
    assert {row["display_name"] for row in grouped[second]} == {"Dal"}
    assert await db_with_user.get_diet_items_for_meals(user_id, []) == {}


async def test_a_day_entered_in_one_sitting_still_reads_in_meal_order(
    db_with_user, user_id
):
    """Logging breakfast after lunch must not print it after lunch."""
    await _log(db_with_user, "lunch", _item("Dal chawal", 520))
    await _log(db_with_user, "breakfast", _item("Oats", 380))
    await _log(db_with_user, "snack", _item("Tea", 20))

    message = _message()
    context = _context(db_with_user)
    context.args = []
    await meals.meals_command(_update(message), context)

    body = "\n".join(_texts(message))
    assert body.index("Oats") < body.index("Dal chawal") < body.index("Tea")
