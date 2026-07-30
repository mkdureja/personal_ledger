"""Diagnostic output must match the privacy claims (plan §0.3).

The operations runbook promises that diagnostics carry "only sanitized
counts/categories — never a token, username, first name, or raw Telegram ID". A
chat ID *is* the user's identity, and these lines land in ordinary service logs,
so the reminder and Repeat failure paths must not name the recipient. The
per-user answer is not lost: ``reminder_deliveries`` records the same sanitized
category against the owner row.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from telegram.error import Forbidden, NetworkError, RetryAfter

from bot import main as main_module
from bot.handlers import home as home_module
from bot.handlers import reminders as reminders_module
from scripts import send_bot_message

CHAT_ID = 987654321


class _FailingBot:
    """A bot whose sends always fail with the configured exception."""

    def __init__(self, error):
        self._error = error
        self.attempts = 0

    async def send_message(self, **_kwargs):
        self.attempts += 1
        raise self._error


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    """Keep retry paths instant; the backoff itself is tested elsewhere."""

    async def _instant(_delay):
        return None

    monkeypatch.setattr(reminders_module.asyncio, "sleep", _instant)


# ---------------------------------------------------------------------------
# Reminder delivery diagnostics
# ---------------------------------------------------------------------------
async def test_permanent_send_failure_log_omits_the_chat_id(caplog):
    bot = _FailingBot(Forbidden("bot was blocked by the user"))
    with caplog.at_level(logging.WARNING, logger=reminders_module.__name__):
        ok, category = await reminders_module._send_with_retry_classified(
            bot, CHAT_ID, "text"
        )
    assert (ok, category) == (False, "permanent")
    assert str(CHAT_ID) not in caplog.text
    assert "Permanent reminder send failure" in caplog.text


async def test_rate_limited_log_omits_the_chat_id(caplog):
    bot = _FailingBot(RetryAfter(600))
    with caplog.at_level(logging.WARNING, logger=reminders_module.__name__):
        ok, category = await reminders_module._send_with_retry_classified(
            bot, CHAT_ID, "text"
        )
    assert (ok, category) == (False, "rate_limited")
    assert str(CHAT_ID) not in caplog.text
    # The actionable numbers — the requested and capped delays — are still there.
    assert "600" in caplog.text


async def test_retry_exhausted_log_omits_the_chat_id(caplog):
    bot = _FailingBot(NetworkError("transient"))
    with caplog.at_level(logging.WARNING, logger=reminders_module.__name__):
        ok, category = await reminders_module._send_with_retry_classified(
            bot, CHAT_ID, "text"
        )
    assert (ok, category) == (False, "retry_exhausted")
    assert bot.attempts == reminders_module._MAX_SEND_ATTEMPTS
    assert str(CHAT_ID) not in caplog.text


async def test_anchor_build_failure_log_omits_the_user_id(db, caplog, monkeypatch):
    """One user's build failure is reported by anchor id, not by identity."""
    await db.ensure_user(CHAT_ID, "u", "U")
    await db.set_reminders_enabled(CHAT_ID, True)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("anchor build bug")

    monkeypatch.setattr(reminders_module, "build_anchor_message", _boom)
    monkeypatch.setattr(reminders_module, "ALLOWED_USER_IDS", frozenset({CHAT_ID}))

    anchor = SimpleNamespace(
        id="morning", emoji="🌅", title="Morning", sections=("study",), quote=False
    )
    context = SimpleNamespace(
        bot=_FailingBot(Forbidden("x")),
        bot_data={"db": db, "routine_targets": None, "routine_quotes": None},
        job=SimpleNamespace(data=anchor),
    )

    with caplog.at_level(logging.ERROR, logger=reminders_module.__name__):
        await reminders_module.anchor_job(context)

    assert str(CHAT_ID) not in caplog.text
    assert "morning" in caplog.text


# ---------------------------------------------------------------------------
# Repeat diagnostics
# ---------------------------------------------------------------------------
async def test_repeat_failure_log_omits_the_user_id(caplog, monkeypatch):
    sent: list[str] = []

    class _Db:
        async def ensure_user(self, *_args):
            return None

        async def repeat_last_meal(self, *_args, **_kwargs):
            raise RuntimeError("write failed")

    async def _reply(text, **_kwargs):
        sent.append(text)

    update = SimpleNamespace(
        effective_message=SimpleNamespace(reply_text=_reply),
        effective_user=SimpleNamespace(id=CHAT_ID, username="u", first_name="U"),
        callback_query=None,
    )
    context = SimpleNamespace(bot_data={"db": _Db()}, user_data={})
    monkeypatch.setattr(home_module, "phase1_enabled_for", lambda _uid: True)

    with caplog.at_level(logging.ERROR, logger=home_module.__name__):
        await home_module.repeat_last_meal(update, context)

    assert sent and "Couldn't repeat" in sent[0]
    assert str(CHAT_ID) not in caplog.text
    assert "nothing was written" in caplog.text


# ---------------------------------------------------------------------------
# Manual-send helper
# ---------------------------------------------------------------------------
def test_manual_send_helper_redacts_the_token():
    token = "123456:SUPERSECRETTOKEN"
    raw = (
        "BadRequest: Bad Request: chat not found "
        f"(https://api.telegram.org/bot{token}/sendMessage)"
    )
    redacted = send_bot_message._redact(raw, token)
    assert token not in redacted
    assert "SUPERSECRETTOKEN" not in redacted
    assert "***" in redacted
    # The bare secret half can appear without its bot-id prefix.
    assert "SUPERSECRETTOKEN" not in send_bot_message._redact(
        "auth failed for SUPERSECRETTOKEN", token
    )
    assert send_bot_message._redact("nothing to hide", "") == "nothing to hide"


def test_manual_send_helper_carries_no_personal_names():
    source = (send_bot_message.__doc__ or "") + (
        send_bot_message.main.__doc__ or ""
    )
    for name in ("Ratika", "Manoj"):
        assert name not in source


# ---------------------------------------------------------------------------
# Startup cancellation
# ---------------------------------------------------------------------------
async def test_catalog_seeding_failure_is_degraded_not_fatal(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(main_module, "DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(main_module, "ROUTINE_PATH", str(tmp_path / "none.yaml"))

    async def _boom(self, _foods):
        raise RuntimeError("seed table locked")

    monkeypatch.setattr(main_module.DatabaseManager, "seed_catalog", _boom)
    application = _build_application()

    with caplog.at_level(logging.WARNING, logger=main_module.__name__):
        await main_module.post_init(application)
    try:
        assert "Catalog seeding failed" in caplog.text
        assert application.bot_data["db"] is not None
    finally:
        await main_module.post_shutdown(application)


@pytest.mark.parametrize("error", [asyncio.CancelledError, KeyboardInterrupt])
async def test_startup_cancellation_propagates(tmp_path, monkeypatch, error):
    """Cancellation and interrupt must not be swallowed as a degraded catalog.

    ``except BaseException`` here used to log "catalog seeding failed" for a
    Ctrl-C and then continue into polling.
    """
    monkeypatch.setattr(main_module, "DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(main_module, "ROUTINE_PATH", str(tmp_path / "none.yaml"))

    async def _cancel(self, _foods):
        raise error()

    monkeypatch.setattr(main_module.DatabaseManager, "seed_catalog", _cancel)
    application = _build_application()

    with pytest.raises(error):
        await main_module.post_init(application)
    # The connection is registered before optional steps, so shutdown can close it.
    assert application.bot_data.get("db") is not None
    await main_module.post_shutdown(application)


def _build_application():
    from telegram.ext import ApplicationBuilder

    return ApplicationBuilder().token("123456:TEST_TOKEN").build()
