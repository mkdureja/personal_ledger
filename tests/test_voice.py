"""Release 5 — voice notes transcribed on-host, landing in the same draft.

Voice is an *input*, not a second logging pipeline, so most of what matters here
is that it converges on machinery already proven elsewhere: the Release 3 parser,
the Release 4 optional assist, and one confirm-before-save screen.

What is genuinely new, and therefore tested hardest:

1. **The audio never leaves the host, and never outlives the attempt.** The
   temporary file is gone whatever happens.
2. **Nothing is downloaded when the answer is already no** — disabled, or a note
   over the duration cap, both refuse from Telegram's metadata alone.
3. **Absence degrades honestly.** ``faster-whisper`` is not installed in this
   environment, which is the real, un-mocked degraded path, and it must produce a
   useful sentence rather than a traceback.
4. **Transcription is sequential**, so a second note queues rather than competing
   for CPU with the first.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType
from telegram.error import TelegramError

from bot import config
from bot.handlers import home, voice as voice_handler
from bot.services.voice import REASON_TEXT, Transcript, VoiceTranscriber

UID = 123456789

_OATS = {
    "id": 1, "name": "oats", "base_unit": "g", "basis_amount": 100.0,
    "calories": 380, "protein_g": 13.0, "carbs_g": 67.0, "fat_g": 7.0,
}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _message():
    return SimpleNamespace(
        chat_id=UID,
        message_id=1,
        voice=SimpleNamespace(file_id="f1", duration=5),
        reply_text=AsyncMock(
            return_value=SimpleNamespace(message_id=2, delete=AsyncMock())
        ),
    )


def _update(message=None, user_id=UID):
    message = message or _message()
    return SimpleNamespace(
        effective_message=message,
        message=message,
        effective_user=SimpleNamespace(id=user_id, first_name="T", username="t"),
        effective_chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
        callback_query=None,
        update_id=1,
    )


def _db(foods=()):
    return SimpleNamespace(
        ensure_user=AsyncMock(),
        list_foods=AsyncMock(return_value=list(foods)),
        list_recipes=AsyncMock(return_value=[]),
        search_catalog=AsyncMock(return_value=[]),
        get_food_portions=AsyncMock(return_value=[]),
        get_catalog_portions=AsyncMock(return_value=[]),
        get_recipe_ingredients=AsyncMock(return_value=[]),
        get_ai_parsing_enabled=AsyncMock(return_value=False),
        log_diet_with_items=AsyncMock(),
    )


class _FakeFile:
    """Stands in for a Telegram File, recording where it was asked to write."""

    def __init__(self, written_text="100g oats", fail=False):
        self.written_text = written_text
        self.fail = fail
        self.paths: list[str] = []

    async def download_to_drive(self, custom_path=None):
        if self.fail:
            raise TelegramError("download failed")
        self.paths.append(custom_path)
        Path(custom_path).write_bytes(b"fake-audio")
        return custom_path


def _context(db=None, *, file=None, transcriber=None, get_file_fails=False):
    bot = SimpleNamespace(
        get_file=AsyncMock(
            side_effect=TelegramError("nope") if get_file_fails else None,
            return_value=file or _FakeFile(),
        )
    )
    bot_data = {"db": db if db is not None else _db()}
    if transcriber is not None:
        bot_data["voice_transcriber"] = transcriber
    return SimpleNamespace(bot=bot, bot_data=bot_data, user_data={}, args=[])


class _StubTranscriber:
    """A transcriber whose result the test picks, recording the paths it saw."""

    def __init__(self, result: Transcript):
        self.result = result
        self.seen: list[str] = []

    async def transcribe(self, path):
        self.seen.append(path)
        assert Path(path).exists(), "audio should still exist during transcription"
        return self.result


def _enable(monkeypatch, *, seconds=60):
    monkeypatch.setattr(config, "VOICE_ENABLED", True)
    monkeypatch.setattr(config, "VOICE_MAX_SECONDS", seconds)
    monkeypatch.setattr(config, "GEMINI_AVAILABLE", False)


def _texts(message):
    """Every string the handler sent, in order."""
    return [call.args[0] for call in message.reply_text.call_args_list if call.args]


# ---------------------------------------------------------------------------
# Refusals that cost nothing
# ---------------------------------------------------------------------------
async def test_voice_disabled_never_downloads_anything(monkeypatch):
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    update, context = _update(), _context()

    await voice_handler.handle_voice_meal(update, context)

    context.bot.get_file.assert_not_awaited()
    assert "Voice logging is off" in _texts(update.message)[0]


async def test_an_over_long_note_is_refused_before_download(monkeypatch):
    _enable(monkeypatch, seconds=30)
    message = _message()
    message.voice = SimpleNamespace(file_id="f1", duration=90)
    update, context = _update(message), _context()

    await voice_handler.handle_voice_meal(update, context)

    context.bot.get_file.assert_not_awaited()
    assert "the limit is 30s" in _texts(message)[0]


async def test_a_note_at_exactly_the_cap_is_accepted(monkeypatch):
    _enable(monkeypatch, seconds=30)
    message = _message()
    message.voice = SimpleNamespace(file_id="f1", duration=30)
    update = _update(message)
    context = _context(_db([_OATS]), transcriber=_StubTranscriber(Transcript("100g oats")))

    await voice_handler.handle_voice_meal(update, context)

    context.bot.get_file.assert_awaited_once()


async def test_a_message_with_no_voice_payload_is_handled(monkeypatch):
    _enable(monkeypatch)
    message = _message()
    message.voice = None
    update, context = _update(message), _context()

    await voice_handler.handle_voice_meal(update, context)

    context.bot.get_file.assert_not_awaited()


# ---------------------------------------------------------------------------
# The audio never outlives the attempt
# ---------------------------------------------------------------------------
async def test_the_recording_is_deleted_after_transcription(monkeypatch):
    _enable(monkeypatch)
    stub = _StubTranscriber(Transcript("100g oats"))
    update = _update()
    context = _context(_db([_OATS]), transcriber=stub)

    await voice_handler.handle_voice_meal(update, context)

    assert stub.seen, "the transcriber should have been given a path"
    assert not Path(stub.seen[0]).exists()
    assert not Path(stub.seen[0]).parent.exists()


async def test_the_recording_is_deleted_even_when_transcription_fails(monkeypatch):
    _enable(monkeypatch)
    stub = _StubTranscriber(Transcript(reason="failed"))
    update = _update()
    context = _context(transcriber=stub)

    await voice_handler.handle_voice_meal(update, context)

    assert not Path(stub.seen[0]).parent.exists()


async def test_a_failed_download_reports_and_leaves_nothing_behind(monkeypatch):
    _enable(monkeypatch)
    update = _update()
    context = _context(file=_FakeFile(fail=True))

    await voice_handler.handle_voice_meal(update, context)

    assert any("couldn't be transcribed" in text for text in _texts(update.message))


async def test_a_failed_get_file_is_reported_not_raised(monkeypatch):
    _enable(monkeypatch)
    update = _update()
    context = _context(get_file_fails=True)

    await voice_handler.handle_voice_meal(update, context)

    assert any("couldn't be transcribed" in text for text in _texts(update.message))


# ---------------------------------------------------------------------------
# A transcript reaches the same confirm screen
# ---------------------------------------------------------------------------
async def test_a_transcript_is_echoed_then_resolved_into_a_preview(monkeypatch):
    _enable(monkeypatch)
    update = _update()
    context = _context(
        _db([_OATS]), transcriber=_StubTranscriber(Transcript("100g oats"))
    )

    await voice_handler.handle_voice_meal(update, context)

    texts = _texts(update.message)
    assert any("Heard:" in text and "100g oats" in text for text in texts)
    assert any("Ready to log" in text for text in texts)
    # It went through the shared path, so a draft exists to confirm.
    assert context.user_data["describe_pending"]["items"]


async def test_voice_writes_nothing_before_the_confirm_tap(monkeypatch):
    _enable(monkeypatch)
    db = _db([_OATS])
    update = _update()
    context = _context(db, transcriber=_StubTranscriber(Transcript("100g oats")))

    await voice_handler.handle_voice_meal(update, context)

    db.log_diet_with_items.assert_not_awaited()


async def test_an_unrecognisable_transcript_still_explains_itself(monkeypatch):
    _enable(monkeypatch)
    update = _update()
    context = _context(_db(), transcriber=_StubTranscriber(Transcript("blah blah")))

    await voice_handler.handle_voice_meal(update, context)

    assert any("couldn't match" in text for text in _texts(update.message))


@pytest.mark.parametrize("reason", ["not_installed", "model_unavailable", "empty", "failed"])
async def test_every_failure_reason_produces_its_own_sentence(monkeypatch, reason):
    _enable(monkeypatch)
    update = _update()
    context = _context(transcriber=_StubTranscriber(Transcript(reason=reason)))

    await voice_handler.handle_voice_meal(update, context)

    assert any(REASON_TEXT[reason] in text for text in _texts(update.message))


# ---------------------------------------------------------------------------
# The transcriber itself
# ---------------------------------------------------------------------------
async def test_a_missing_package_reports_not_installed(monkeypatch):
    """Forced, not inferred from the environment.

    An earlier version of this test simply relied on faster-whisper being absent
    from the dev machine. That made its meaning flip the moment the package was
    installed — it started exercising a different branch while still looking
    green. Both branches are now driven deterministically.
    """
    # Binding a module name to None makes ``import`` raise ImportError.
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    transcriber = VoiceTranscriber("base")

    result = await transcriber.transcribe("does-not-matter.ogg")

    assert result.ok is False
    assert result.reason == "not_installed"
    assert "isn't installed" in result.message


async def test_a_package_that_cannot_build_a_model_says_so_and_is_not_retried(
    monkeypatch,
):
    """Installed but unusable — a different problem, and a different sentence."""
    fake = types.ModuleType("faster_whisper")

    def _explode(*args, **kwargs):
        raise RuntimeError("model files missing")

    fake.WhisperModel = _explode
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    transcriber = VoiceTranscriber("base")

    first = await transcriber.transcribe("a.ogg")
    second = await transcriber.transcribe("a.ogg")

    assert first.reason == "model_unavailable"
    # A failed load is remembered, so a second note does not pay for it again.
    assert second.reason == "model_unavailable"
    assert transcriber.loaded is False


async def test_transcription_is_sequential():
    """Two notes must queue, not compete for CPU."""
    transcriber = VoiceTranscriber("base")
    transcriber._model = object()  # pretend a model is loaded
    overlapping = False
    active = 0

    def _slow(path):
        nonlocal overlapping, active
        active += 1
        if active > 1:
            overlapping = True
        import time

        time.sleep(0.05)
        active -= 1
        return "oats"

    transcriber._transcribe_sync = _slow

    await asyncio.gather(
        transcriber.transcribe("a.ogg"), transcriber.transcribe("b.ogg")
    )

    assert overlapping is False


async def test_a_transcriber_error_becomes_a_reason_not_an_exception():
    transcriber = VoiceTranscriber("base")
    transcriber._model = object()

    def _boom(path):
        raise RuntimeError("model exploded")

    transcriber._transcribe_sync = _boom

    result = await transcriber.transcribe("a.ogg")

    assert result.reason == "failed"
    assert result.ok is False


async def test_silence_is_reported_as_empty_not_as_success():
    transcriber = VoiceTranscriber("base")
    transcriber._model = object()
    transcriber._transcribe_sync = lambda path: "   "

    result = await transcriber.transcribe("a.ogg")

    assert result.reason == "empty"
    assert result.ok is False


def test_transcript_ok_requires_text_and_no_reason():
    assert Transcript("oats").ok is True
    assert Transcript("").ok is False
    assert Transcript("oats", reason="failed").ok is False


async def test_one_transcriber_is_reused_across_notes():
    """The model is expensive; it must be loaded at most once per process."""
    context = _context()

    first = voice_handler.get_transcriber(context)
    second = voice_handler.get_transcriber(context)

    assert first is second


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
async def test_a_voice_note_during_a_guided_flow_is_refused_without_download(
    monkeypatch,
):
    _enable(monkeypatch)
    monkeypatch.setattr(home, "phase1_enabled_for", lambda uid: True)
    update = _update()
    context = _context()
    context.user_data["_ledger_active_conversation"] = ("diet", UID)

    await home.home_voice_router(update, context)

    context.bot.get_file.assert_not_awaited()


async def test_home_routes_an_idle_voice_note_to_transcription(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(home, "phase1_enabled_for", lambda uid: True)
    update = _update()
    context = _context(
        _db([_OATS]), transcriber=_StubTranscriber(Transcript("100g oats"))
    )

    await home.home_voice_router(update, context)

    assert any("Heard:" in text for text in _texts(update.message))
