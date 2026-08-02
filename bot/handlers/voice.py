"""Voice meal notes: record it, and it lands in the same confirm screen.

Release 5. A voice note is transcribed **on this host** and the transcript is
handed to the Release 3 typed-meal path — the same parser, the same optional
Gemini assist, the same preview, the same one-tap save. Voice adds an input, not
a second logging pipeline, so there is exactly one place a meal can be written.

The audio is deleted as soon as it has been transcribed. It is never stored,
never attached to a log row, and never sent anywhere.

Order of checks is deliberate: the duration cap is enforced from Telegram's
declared metadata *before* the file is downloaded, so an oversized recording
costs nothing.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .. import config
from ..services.voice import VoiceTranscriber
from .common import escape_html, reply_html
from .describe import start_describe

logger = logging.getLogger(__name__)

_TRANSCRIBER_KEY = "voice_transcriber"

_DISABLED_TEXT = (
    "🎤 Voice logging is off on this bot.\n"
    "Type the meal instead — for example "
    "<code>/describe 2 eggs, 100g oats</code>."
)


def get_transcriber(context: ContextTypes.DEFAULT_TYPE) -> VoiceTranscriber:
    """The transcriber for this application, creating it on first use."""
    return transcriber_for(context.bot_data)


def transcriber_for(bot_data) -> VoiceTranscriber:
    """One transcriber per process, so the model is loaded at most once.

    Stored on ``bot_data`` rather than a module global: a module global would
    leak the loaded model between tests and, worse, between an application that
    was torn down and one that replaced it. Takes the mapping rather than a
    ``Context`` so startup, which has no update to build a context from, shares
    exactly one instance with the handlers.
    """
    transcriber = bot_data.get(_TRANSCRIBER_KEY)
    if transcriber is None:
        transcriber = VoiceTranscriber(
            config.VOICE_MODEL_SIZE,
            load_timeout=config.VOICE_LOAD_TIMEOUT_SECONDS,
            transcribe_timeout=config.VOICE_TRANSCRIBE_TIMEOUT_SECONDS,
        )
        bot_data[_TRANSCRIBER_KEY] = transcriber
    return transcriber


async def preload_transcriber(bot_data) -> None:
    """Load the speech model now, so no user's request pays for it."""
    failure = await transcriber_for(bot_data).ensure_loaded()
    if failure is None:
        logger.info("Speech model ready")
    else:
        logger.warning("Speech model unavailable at startup (%s)", failure.reason)


async def handle_voice_meal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Transcribe a voice note and open the typed-meal confirm screen."""
    message = update.effective_message
    voice = getattr(message, "voice", None)

    if not config.VOICE_ENABLED:
        await reply_html(message, _DISABLED_TEXT)
        return
    if voice is None:
        await reply_html(message, _DISABLED_TEXT)
        return

    # Checked against Telegram's own metadata, before any download.
    duration = getattr(voice, "duration", None) or 0
    if duration > config.VOICE_MAX_SECONDS:
        await reply_html(
            message,
            f"🎤 That note is {int(duration)}s; the limit is "
            f"{config.VOICE_MAX_SECONDS}s. Record a shorter one, or type the meal.",
        )
        return

    # Duration is what Telegram *declares*; size is what actually gets written to
    # disk and fed to the decoder. Checking both means a wrong or hostile
    # duration cannot turn into an unbounded download.
    size = getattr(voice, "file_size", None) or 0
    if size > config.VOICE_MAX_FILE_BYTES:
        await reply_html(
            message,
            f"🎤 That recording is {size // (1024 * 1024)} MB; the limit is "
            f"{config.VOICE_MAX_FILE_BYTES // (1024 * 1024)} MB. "
            "Record a shorter one, or type the meal.",
        )
        return

    notice = None
    try:
        notice = await message.reply_text("🎤 Listening…")
    except TelegramError:
        logger.debug("Could not send transcription notice", exc_info=True)

    transcript = await _transcribe_voice(update, context, voice)

    if notice is not None:
        try:
            await notice.delete()
        except TelegramError:
            logger.debug("Could not remove transcription notice", exc_info=True)

    if not transcript.ok:
        # ``transcript.message`` is our own static copy, so escaping it would
        # only turn its apostrophes into entities. Transcribed *speech* below is
        # user-supplied and is escaped.
        await reply_html(message, f"🎤 {transcript.message}")
        return

    # Echo what was heard before anything is resolved. A wrong transcription is
    # the most likely failure here, and the user needs to see it to trust — or
    # correct — the preview that follows.
    await reply_html(message, f"🎤 Heard: <i>{escape_html(transcript.text)}</i>")
    await start_describe(message, context, update.effective_user.id, transcript.text)


async def _transcribe_voice(update: Update, context, voice):
    """Download to a temporary file, transcribe, and delete the audio."""
    from ..services.voice import Transcript

    try:
        telegram_file = await context.bot.get_file(voice.file_id)
    except TelegramError:
        logger.warning("Could not fetch voice file", exc_info=False)
        return Transcript(reason="failed")

    with tempfile.TemporaryDirectory(prefix="ledger-voice-") as tmpdir:
        # The temporary directory is removed on exit whatever happens, so the
        # recording never outlives the transcription attempt.
        path = Path(tmpdir) / "note.ogg"
        try:
            await telegram_file.download_to_drive(custom_path=str(path))
        except (TelegramError, OSError):
            logger.warning("Could not download voice note", exc_info=False)
            return Transcript(reason="failed")

        return await get_transcriber(context).transcribe(str(path))
