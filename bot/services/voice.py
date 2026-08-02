"""Local speech-to-text for voice meal notes. Audio never leaves the host.

Release 5. This is the deliberate asymmetry in the design: meal *text* may be
sent to a remote parser with consent, but the recording itself is transcribed
on-device. A voice note carries far more than its words — who is speaking, who
else is in the room, background noise — so it stays here.

Three properties shape the implementation:

* **Optional at every level.** ``faster-whisper`` is not a hard dependency and is
  imported inside the call, never at module scope. Without it installed the bot
  runs exactly as before and says so plainly. The test suite never needs it.
* **Sequential.** Transcription is CPU-bound and will happily consume every core.
  One shared lock means two voice notes queue instead of competing, so a second
  note cannot make the first one time out.
* **Bounded.** Duration is capped by the caller *before* download, and the model
  is loaded once and reused.

Every failure is reported as a typed reason rather than an exception, because
each one needs a different sentence to the user: install the package, wait for
the download, record something shorter, or just type it instead.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["Transcript", "VoiceTranscriber", "REASON_TEXT"]

#: Why transcription produced nothing. Each maps to its own user-facing sentence.
REASON_TEXT = {
    "not_installed": (
        "Voice transcription isn't installed on this bot. "
        "Type the meal instead, or use /describe."
    ),
    "model_unavailable": (
        "The speech model couldn't be loaded. Type the meal instead, "
        "or use /describe."
    ),
    "empty": "I couldn't hear any words in that. Try again, or type it instead.",
    "failed": "That recording couldn't be transcribed. Try typing it instead.",
}


@dataclass(frozen=True)
class Transcript:
    """The outcome of one transcription attempt."""

    text: str = ""
    reason: str | None = None

    @property
    def ok(self) -> bool:
        # ``strip`` matters: a recording of silence can transcribe to whitespace,
        # which is not speech and must not be treated as a usable result.
        return bool(self.text.strip()) and self.reason is None

    @property
    def message(self) -> str:
        return REASON_TEXT.get(self.reason or "", REASON_TEXT["failed"])


class VoiceTranscriber:
    """Loads one Whisper model lazily and transcribes one file at a time."""

    def __init__(self, model_size: str = "base") -> None:
        self._model_size = model_size
        self._model: Any | None = None
        self._load_failed = False
        # Guards both the one-time load and each transcription, so the model is
        # never constructed twice and never used concurrently.
        self._lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load_model(self) -> Any | None:
        """Import and construct the model. Runs in a worker thread."""
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            logger.info(
                "faster-whisper is not installed; voice notes stay unsupported"
            )
            return None
        try:
            # int8 on CPU is the practical choice for a household machine: a
            # fraction of the memory of float16 with no meaningful accuracy loss
            # for short dictation.
            return WhisperModel(self._model_size, device="cpu", compute_type="int8")
        except Exception:
            logger.warning(
                "Could not load the '%s' speech model", self._model_size, exc_info=False
            )
            return None

    def _transcribe_sync(self, path: str) -> str:
        """Run the model. Called only inside the lock, in a worker thread."""
        segments, _info = self._model.transcribe(
            path,
            beam_size=1,          # dictation of a short phrase needs no search
            vad_filter=True,      # drop leading/trailing silence
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def transcribe(self, path: str) -> Transcript:
        """Transcribe an audio file at ``path``. Never raises."""
        async with self._lock:
            if self._model is None:
                if self._load_failed:
                    return Transcript(reason="model_unavailable")
                self._model = await asyncio.to_thread(self._load_model)
                if self._model is None:
                    self._load_failed = True
                    # Distinguish "package missing" from "model would not load":
                    # the first is an install, the second is usually disk or a
                    # failed download, and the user can act on neither the same way.
                    try:
                        import faster_whisper  # noqa: F401
                    except ImportError:
                        return Transcript(reason="not_installed")
                    return Transcript(reason="model_unavailable")

            try:
                text = await asyncio.to_thread(self._transcribe_sync, path)
            except Exception:
                logger.warning("Transcription failed", exc_info=False)
                return Transcript(reason="failed")

        text = (text or "").strip()
        if not text:
            return Transcript(reason="empty")
        return Transcript(text=text)
