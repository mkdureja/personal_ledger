"""Two-user tenant isolation (implementation_plan Phase 2).

Two synthetic users, Manoj and Ratika, prove the privacy boundary at two layers:

1. **Routing** — representative Update objects are checked against the *real*
   handlers registered by ``build_application()``. This exercises the genuine
   AUTH_FILTER, private-chat restriction, command entities, and callback patterns.
   Command auth is enforced here (a third user / group update routes to no
   handler); callback auth is deliberately enforced one layer deeper, inside
   ``authorized_callback`` (see test_auth.py), so a callback pattern still matches
   at the routing layer and is denied at execution.
2. **Data** — a two-user database proves every read is owner-scoped, identical
   names coexist per user, cross-owner mutations are no-ops that leave both users'
   rows intact, and simultaneous guided conversations keep separate user_data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User

import bot.handlers.common as common
from bot.database import DatabaseManager
from bot.handlers.common import AUTH_FILTER
from bot.handlers.study import receive_subject
from bot.main import build_application

MANOJ = 123456789  # matches conftest ALLOWED_USER_IDS
RATIKA = 987654321  # added to the allowlist only within the routing fixture
THIRD = 555000222  # never authorized
TODAY = datetime.now(timezone.utc).date()

# A non-networking bot stub; check_update reads only .username (for /cmd@bot).
_BOT = MagicMock()
_BOT.username = "LedgerTestBot"


# ---------------------------------------------------------------------------
# Routing layer — real handlers via build_application()
# ---------------------------------------------------------------------------
@pytest.fixture
def routing_app():
    """The real application with both Manoj and Ratika authorized.

    Ratika is added to the shared AUTH_FILTER's user set (and the callback
    allowlist) for the duration of the test, then removed so global state is not
    leaked to other modules.
    """
    app = build_application()
    AUTH_FILTER.base_filter.add_user_ids(RATIKA)
    original_allowed = common.ALLOWED_USER_IDS
    common.ALLOWED_USER_IDS = frozenset({MANOJ, RATIKA})
    try:
        yield app
    finally:
        AUTH_FILTER.base_filter.remove_user_ids(RATIKA)
        common.ALLOWED_USER_IDS = original_allowed


def _accepting_handler(app, update) -> str | None:
    """Name of the first registered handler that would handle ``update``."""
    for group in sorted(app.handlers):
        for handler in app.handlers[group]:
            if handler.check_update(update):
                callback = getattr(handler, "callback", None)
                return getattr(callback, "__name__", type(handler).__name__)
    return None


def _command_update(user_id: int, text: str, chat_type: str = "private") -> Update:
    chat = Chat(id=user_id, type=chat_type)
    user = User(id=user_id, is_bot=False, first_name="U")
    command = text.split()[0]
    entities = [MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(command))]
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
        entities=entities,
    )
    message.set_bot(_BOT)
    return Update(update_id=1, message=message)


def _callback_update(user_id: int, data: str, chat_type: str = "private") -> Update:
    chat = Chat(id=user_id, type=chat_type)
    user = User(id=user_id, is_bot=False, first_name="U")
    message = Message(message_id=1, date=datetime.now(timezone.utc), chat=chat, from_user=user)
    message.set_bot(_BOT)
    query = CallbackQuery(
        id="1", from_user=user, chat_instance="ci", data=data, message=message
    )
    query.set_bot(_BOT)
    return Update(update_id=1, callback_query=query)


def test_both_allowed_users_route_private_commands(routing_app):
    assert _accepting_handler(routing_app, _command_update(MANOJ, "/streak")) == "streak_command"
    assert _accepting_handler(routing_app, _command_update(RATIKA, "/streak")) == "streak_command"


def test_third_user_command_routes_to_no_handler(routing_app):
    assert _accepting_handler(routing_app, _command_update(THIRD, "/streak")) is None
    assert _accepting_handler(routing_app, _command_update(THIRD, "/summary")) is None


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
def test_allowed_user_denied_outside_private_chat(routing_app, chat_type):
    update = _command_update(MANOJ, "/streak", chat_type=chat_type)
    assert _accepting_handler(routing_app, update) is None


def test_callback_pattern_routes_regardless_of_user(routing_app):
    """Callback routing matches by pattern; ownership/auth is enforced in-handler.

    (test_auth.py proves authorized_callback denies the third user, group chat,
    and missing chat context at execution.)
    """
    assert (
        _accepting_handler(routing_app, _callback_update(MANOJ, "habit_check_5"))
        == "habit_check_callback"
    )
    # A third user's press still matches the pattern — denial happens one layer in.
    assert (
        _accepting_handler(routing_app, _callback_update(THIRD, "habit_check_5"))
        == "habit_check_callback"
    )


# ---------------------------------------------------------------------------
# Data layer — two-user database isolation
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def two_user_db():
    mgr = DatabaseManager(":memory:")
    await mgr.connect()
    await mgr.init_db()
    await mgr.ensure_user(MANOJ, "manoj", "Manoj")
    await mgr.ensure_user(RATIKA, "ratika", "Ratika")
    yield mgr
    await mgr.close()


async def test_identical_names_coexist_per_user(two_user_db):
    m_hid, m_status = await two_user_db.add_habit(MANOJ, "Read")
    r_hid, r_status = await two_user_db.add_habit(RATIKA, "Read")
    assert m_status == "added" and r_status == "added"
    assert m_hid != r_hid

    await two_user_db.save_food(MANOJ, "Oats", "g", 100.0, calories=350, protein_g=1, carbs_g=2, fat_g=3)
    await two_user_db.save_food(RATIKA, "Oats", "g", 100.0, calories=350, protein_g=1, carbs_g=2, fat_g=3)
    m_foods = await two_user_db.list_foods(MANOJ)
    r_foods = await two_user_db.list_foods(RATIKA)
    assert len(m_foods) == 1 and len(r_foods) == 1
    assert m_foods[0]["id"] != r_foods[0]["id"]


async def test_reads_return_only_the_acting_users_rows(two_user_db):
    await two_user_db.log_study(MANOJ, "Manoj-Math", 60)
    await two_user_db.log_study(RATIKA, "Ratika-History", 45)
    await two_user_db.log_gym(MANOJ, "Manoj-Squats", 3, 5, 100.0)
    await two_user_db.log_gym(RATIKA, "Ratika-Bench", 3, 5, 80.0)

    m_study = await two_user_db.get_study_logs(MANOJ, TODAY, TODAY)
    r_study = await two_user_db.get_study_logs(RATIKA, TODAY, TODAY)
    assert [row["subject"] for row in m_study] == ["Manoj-Math"]
    assert [row["subject"] for row in r_study] == ["Ratika-History"]

    m_gym = await two_user_db.get_gym_logs(MANOJ, TODAY, TODAY)
    r_gym = await two_user_db.get_gym_logs(RATIKA, TODAY, TODAY)
    assert [row["exercise"] for row in m_gym] == ["Manoj-Squats"]
    assert [row["exercise"] for row in r_gym] == ["Ratika-Bench"]


async def test_cross_owner_habit_operations_are_noops(two_user_db):
    ratika_hid, _ = await two_user_db.add_habit(RATIKA, "Yoga")

    # Manoj cannot check, uncheck, streak, or deactivate Ratika's habit.
    assert await two_user_db.check_habit(MANOJ, ratika_hid, TODAY) is False
    assert await two_user_db.get_checked_habits(MANOJ, TODAY) == set()
    assert await two_user_db.get_checked_habits(RATIKA, TODAY) == set()
    assert await two_user_db.get_streak(MANOJ, ratika_hid, TODAY) == 0
    assert await two_user_db.deactivate_habit(MANOJ, ratika_hid) is False

    # Ratika's habit is untouched and still active.
    active = await two_user_db.get_active_habits(RATIKA)
    assert [h["id"] for h in active] == [ratika_hid]


async def test_cross_owner_delete_log_is_noop(two_user_db):
    ratika_log_id = await two_user_db.log_study(RATIKA, "Ratika-Physics", 30)

    result = await two_user_db.delete_log_by_id(MANOJ, "study_logs", ratika_log_id)
    assert result is None  # nothing deleted for the wrong owner

    remaining = await two_user_db.get_study_logs(RATIKA, TODAY, TODAY)
    assert [row["subject"] for row in remaining] == ["Ratika-Physics"]


async def test_cross_owner_archive_food_is_noop(two_user_db):
    await two_user_db.save_food(RATIKA, "Rice", "g", 100.0, calories=100, protein_g=1, carbs_g=2, fat_g=3)
    ratika_food = (await two_user_db.list_foods(RATIKA))[0]

    result = await two_user_db.archive_food(MANOJ, ratika_food["id"])
    assert result["status"] == "not_found"

    assert len(await two_user_db.list_foods(RATIKA)) == 1  # still active


async def test_owner_can_operate_on_own_data_after_cross_attempts(two_user_db):
    """A failed cross-owner attempt must not poison the real owner's own action."""
    ratika_hid, _ = await two_user_db.add_habit(RATIKA, "Meditate")
    await two_user_db.check_habit(MANOJ, ratika_hid, TODAY)  # no-op

    assert await two_user_db.check_habit(RATIKA, ratika_hid, TODAY) is True
    assert await two_user_db.get_streak(RATIKA, ratika_hid, TODAY) == 1


async def test_simultaneous_conversations_keep_separate_user_data(two_user_db):
    """Two guided study flows in flight must not share context.user_data."""

    def _text_update(user_id: int, text: str):
        message = SimpleNamespace(text=text, reply_text=AsyncMock())
        return SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=user_id),
        )

    manoj_ctx = SimpleNamespace(user_data={}, bot_data={"db": two_user_db})
    ratika_ctx = SimpleNamespace(user_data={}, bot_data={"db": two_user_db})

    await receive_subject(_text_update(MANOJ, "Calculus"), manoj_ctx)
    await receive_subject(_text_update(RATIKA, "Poetry"), ratika_ctx)

    assert manoj_ctx.user_data["study_subject"] == "Calculus"
    assert ratika_ctx.user_data["study_subject"] == "Poetry"
