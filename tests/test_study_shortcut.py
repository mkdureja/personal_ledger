"""Tests for the unambiguous /study shortcut grammar."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import ConversationHandler

from bot.config import today_local
from bot.handlers import study
from bot.handlers.study import _AMBIGUOUS, _parse_study_shortcut, study_command


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["maths", "45"], (1, 45)),
        (["maths", "45", "revised", "trig"], (1, 45)),
        (["organic", "chem", "90"], (2, 90)),
        (["physics", "60m", "reviewed", "chapter", "2"], (1, 60)),
        (["physics", "reviewed", "45min"], (2, 45)),
        (["maths", "hello", "world"], None),
        (["maths"], None),
    ],
)
def test_parse_study_shortcut(args, expected):
    assert _parse_study_shortcut(args) == expected


@pytest.mark.parametrize(
    "args",
    [
        ["physics", "60", "reviewed", "chapter", "2"],  # two bare integers
        ["physics", "30m", "then", "45m"],  # two explicit markers
    ],
)
def test_parse_study_shortcut_is_ambiguous(args):
    assert _parse_study_shortcut(args) is _AMBIGUOUS


def _study_update(user_id):
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id, username="t", first_name="T"),
        effective_chat=SimpleNamespace(id=user_id),
    )
    return update, message


def _context(db, args):
    return SimpleNamespace(bot_data={"db": db}, user_data={}, args=args)


@pytest.mark.asyncio
async def test_shortcut_single_int_logs(db_with_user, user_id):
    update, _ = _study_update(user_id)
    result = await study_command(
        update, _context(db_with_user, ["maths", "45", "revised", "trig"])
    )
    assert result == ConversationHandler.END
    rows = await db_with_user.get_study_logs(user_id, today_local(), today_local())
    assert len(rows) == 1
    assert rows[0]["subject"] == "maths"
    assert rows[0]["duration_min"] == 45
    assert rows[0]["notes"] == "revised trig"


@pytest.mark.asyncio
async def test_shortcut_ambiguous_rejects_and_does_not_log(db_with_user, user_id):
    update, message = _study_update(user_id)
    result = await study_command(
        update, _context(db_with_user, ["physics", "60", "reviewed", "chapter", "2"])
    )
    assert result == ConversationHandler.END
    rows = await db_with_user.get_study_logs(user_id, today_local(), today_local())
    assert rows == []
    assert "can't tell which number" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_shortcut_marker_disambiguates(db_with_user, user_id):
    update, _ = _study_update(user_id)
    await study_command(
        update, _context(db_with_user, ["physics", "60m", "reviewed", "chapter", "2"])
    )
    rows = await db_with_user.get_study_logs(user_id, today_local(), today_local())
    assert len(rows) == 1
    assert rows[0]["subject"] == "physics"
    assert rows[0]["duration_min"] == 60
    assert rows[0]["notes"] == "reviewed chapter 2"


@pytest.mark.asyncio
async def test_shortcut_without_duration_enters_guided_flow(db_with_user, user_id):
    update, _ = _study_update(user_id)
    result = await study_command(update, _context(db_with_user, ["maths", "hello"]))
    assert result == study.SUBJECT
    rows = await db_with_user.get_study_logs(user_id, today_local(), today_local())
    assert rows == []


@pytest.mark.asyncio
async def test_shortcut_rejects_out_of_range_duration(db_with_user, user_id):
    update, message = _study_update(user_id)
    too_long = str(study.MAX_STUDY_MINUTES + 1)
    result = await study_command(update, _context(db_with_user, ["maths", too_long]))
    assert result == ConversationHandler.END
    rows = await db_with_user.get_study_logs(user_id, today_local(), today_local())
    assert rows == []
    assert "Duration must be between" in message.reply_text.await_args.args[0]
