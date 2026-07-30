"""/recent must never build a message past Telegram's limit (codex finding #9).

Ten valid diet entries with maximal, HTML-expanding descriptions previously
concatenated into a single ~25k-unit message that Telegram rejects, so the
reconciliation view silently failed for exactly the user who needed it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.handlers.recent import _telegram_text_units, recent_command

# Telegram's hard ceiling; our packer targets 4000 units of headroom below it.
_TELEGRAM_HARD_LIMIT = 4096


def _diet_entry(entry_id: int, summary: str) -> dict:
    return {
        "kind": "diet",
        "id": entry_id,
        "logged_at": "2026-07-18 06:30:00",
        "summary": summary,
        "n1": 500,
        "n2": None,
    }


def _run(entries):
    db = SimpleNamespace(get_recent_entries=AsyncMock(return_value=entries))
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot_data={"db": db})
    return message, recent_command(update, context)


async def test_recent_small_input_is_one_message():
    message, coro = _run([_diet_entry(1, "Oats and milk")])
    await coro
    assert message.reply_text.await_count == 1


async def test_recent_splits_max_input_and_stays_under_limit():
    # 10 entries, each a maximal 500-char description of HTML-expanding '&'
    # (escapes to "&amp;", 5 units each -> ~2500 units per entry).
    big = "&" * 500
    message, coro = _run([_diet_entry(i, big) for i in range(10)])
    await coro

    calls = message.reply_text.await_args_list
    assert len(calls) > 1, "a maximal batch must be split across messages"

    for call in calls:
        text = call.args[0]
        assert _telegram_text_units(text) <= _TELEGRAM_HARD_LIMIT

    combined = "\n".join(call.args[0] for call in calls)
    # Header shown once; every entry represented exactly once.
    assert combined.count("Recent entries") == 1
    assert combined.count("500 cal") == 10
