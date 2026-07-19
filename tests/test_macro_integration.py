"""End-to-end coverage across macro handlers, storage, and undo output."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ParseMode
from telegram.ext import ConversationHandler

from bot.config import today_local
from bot.handlers.common import activate_conversation, cancel_command, undo_command
from bot.handlers.diet import diet_command


@pytest.mark.asyncio
async def test_quick_macro_log_persists_and_undo_reports_macros(
    db_with_user, user_id: int
) -> None:
    user = SimpleNamespace(id=user_id, username="tester", first_name="Test")
    log_message = SimpleNamespace(reply_text=AsyncMock())
    log_update = SimpleNamespace(
        message=log_message,
        effective_message=log_message,
        effective_user=user,
        effective_chat=SimpleNamespace(id=user_id),
    )
    log_context = SimpleNamespace(
        bot_data={"db": db_with_user},
        user_data={},
        args=["lunch", "dal+rice", "650", "p=25.5", "c=80", "f=15.25"],
    )

    result = await diet_command(log_update, log_context)

    assert result == ConversationHandler.END
    rows = await db_with_user.get_diet_logs(
        user_id, today_local(), today_local()
    )
    row = next(row for row in rows if row["food_items"] == "dal, rice")
    assert row["protein_g"] == pytest.approx(25.5)
    assert row["carbs_g"] == pytest.approx(80.0)
    assert row["fat_g"] == pytest.approx(15.25)

    undo_message = SimpleNamespace(reply_text=AsyncMock())
    undo_update = SimpleNamespace(
        message=undo_message,
        effective_user=user,
        effective_chat=SimpleNamespace(id=user_id),
    )
    undo_context = SimpleNamespace(
        bot_data={"db": db_with_user},
        user_data={},
    )

    await undo_command(undo_update, undo_context)

    text = undo_message.reply_text.await_args.args[0]
    assert "P 25.5g · C 80g · F 15.25g" in text
    assert undo_message.reply_text.await_args.kwargs["parse_mode"] == ParseMode.HTML
    remaining = await db_with_user.get_diet_logs(
        user_id, today_local(), today_local()
    )
    assert all(item["id"] != row["id"] for item in remaining)


@pytest.mark.asyncio
async def test_cancel_clears_pending_guided_macro_state() -> None:
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message,
        effective_message=message,
        effective_chat=SimpleNamespace(id=42),
    )
    context = SimpleNamespace(
        user_data={
            "diet_meal_type": "dinner",
            "diet_food_items": "tofu and rice",
            "diet_calories": 700,
        }
    )
    activate_conversation(update, context, "diet")

    result = await cancel_command(update, context)

    assert result == ConversationHandler.END
    assert context.user_data == {}


@pytest.mark.asyncio
async def test_undo_confirmation_bounds_oversized_legacy_diet_values(
    user_id: int,
) -> None:
    oversized = "&" * 10_000
    db = SimpleNamespace(
        undo_last=AsyncMock(
            return_value={
                "category": oversized,
                "meal_type": oversized,
                "food_items": oversized,
                "calories": oversized,
                "protein_g": oversized,
                "carbs_g": oversized,
                "fat_g": oversized,
            }
        )
    )
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
    )
    context = SimpleNamespace(bot_data={"db": db}, user_data={})

    await undo_command(update, context)

    text = message.reply_text.await_args.args[0]
    utf16_units = len(text.encode("utf-16-le")) // 2
    assert utf16_units < 4_096
    assert oversized not in text
    assert "…" in text
