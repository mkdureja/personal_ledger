"""Guided-flow failure consistency (implementation_plan Phase 6).

If a state-advancing prompt fails to send, the flow must end cleanly with no
half-advanced state — context.user_data and the conversation position can never
disagree. A durable mutation that already succeeded (a saved gym exercise) is
kept; the user is not invited to blindly retry it.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from telegram.constants import ChatType
from telegram.error import NetworkError
from telegram.ext import ConversationHandler

from bot.database import DatabaseManager
from bot.handlers import gym, study
from bot.handlers.common import activate_conversation, active_conversation_flow

USER = 123456789


@pytest_asyncio.fixture
async def db():
    mgr = DatabaseManager(":memory:")
    await mgr.connect()
    await mgr.init_db()
    await mgr.ensure_user(USER, "u", "U")
    yield mgr
    await mgr.close()


def _failing_update(text: str):
    """An update whose replies all raise NetworkError (a TelegramError)."""
    message = SimpleNamespace(
        text=text,
        message_id=5,
        reply_text=AsyncMock(side_effect=NetworkError("down")),
    )
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=USER),
        effective_chat=SimpleNamespace(id=USER, type=ChatType.PRIVATE),
    )


def _ctx(db, **user_data):
    return SimpleNamespace(bot_data={"db": db}, user_data=dict(user_data))


# ---------------------------------------------------------------------------
# Study — each state-advancing prompt fails independently
# ---------------------------------------------------------------------------
async def test_study_subject_prompt_failure_ends_and_clears(db):
    update = _failing_update("Calculus")
    context = _ctx(db)
    activate_conversation(update, context, "study")

    result = await study.receive_subject(update, context)

    assert result == ConversationHandler.END
    assert "study_subject" not in context.user_data
    assert active_conversation_flow(context) is None


async def test_study_duration_prompt_failure_ends_and_clears(db):
    update = _failing_update("45")
    context = _ctx(db, study_subject="Calculus")
    activate_conversation(update, context, "study")

    result = await study.receive_duration(update, context)

    assert result == ConversationHandler.END
    assert "study_subject" not in context.user_data
    assert "study_duration" not in context.user_data
    assert active_conversation_flow(context) is None


# ---------------------------------------------------------------------------
# Gym — each state-advancing prompt fails independently
# ---------------------------------------------------------------------------
async def test_gym_exercise_prompt_failure_ends_and_clears(db):
    update = _failing_update("Squats")
    context = _ctx(db, gym_exercises=[])
    activate_conversation(update, context, "gym")

    result = await gym.receive_exercise(update, context)

    assert result == ConversationHandler.END
    assert "gym_current_exercise" not in context.user_data
    assert active_conversation_flow(context) is None


async def test_gym_sets_prompt_failure_ends_and_clears(db):
    update = _failing_update("3")
    context = _ctx(db, gym_exercises=[], gym_current_exercise="Squats")
    activate_conversation(update, context, "gym")

    result = await gym.receive_sets(update, context)

    assert result == ConversationHandler.END
    assert "gym_current_sets" not in context.user_data
    assert active_conversation_flow(context) is None


async def test_gym_reps_prompt_failure_ends_and_clears(db):
    update = _failing_update("10")
    context = _ctx(
        db, gym_exercises=[], gym_current_exercise="Squats", gym_current_sets=3
    )
    activate_conversation(update, context, "gym")

    result = await gym.receive_reps(update, context)

    assert result == ConversationHandler.END
    assert "gym_current_reps" not in context.user_data
    assert active_conversation_flow(context) is None


# ---------------------------------------------------------------------------
# A durable save survives a failed continuation prompt (no blind retry)
# ---------------------------------------------------------------------------
async def test_saved_gym_exercise_survives_failed_continuation(db):
    update = _failing_update("/skip")  # bodyweight; triggers save then "log another?"
    context = _ctx(
        db,
        gym_exercises=[],
        gym_current_exercise="Pushups",
        gym_current_sets=3,
        gym_current_reps=12,
    )
    activate_conversation(update, context, "gym")

    result = await gym.receive_weight(update, context)

    assert result == ConversationHandler.END  # flow ended, not stuck
    # The exercise was persisted before the failed prompt — visible via /recent.
    recent = await db.get_recent_entries(USER)
    assert any(e["kind"] == "gym" and e["summary"] == "Pushups" for e in recent)


# ---------------------------------------------------------------------------
# One user's failed flow does not touch another user's state
# ---------------------------------------------------------------------------
async def test_one_users_failure_does_not_affect_another(db):
    manoj = _failing_update("Calculus")
    manoj_ctx = _ctx(db)
    activate_conversation(manoj, manoj_ctx, "study")

    # Ratika is mid-flow in her own context with committed state.
    ratika_ctx = _ctx(db, study_subject="Poetry")

    await study.receive_subject(manoj, manoj_ctx)

    assert active_conversation_flow(manoj_ctx) is None  # Manoj cleared
    assert ratika_ctx.user_data["study_subject"] == "Poetry"  # Ratika untouched
