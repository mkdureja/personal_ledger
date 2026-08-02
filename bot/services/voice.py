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
* **Bounded in time, not just size.** Duration is capped by the caller *before*
  download, and the model is loaded once and reused — but neither stops a model
  download from stalling or a decoder from wedging. Both the load and each
  transcription run under a wall-clock ceiling, and waiting for the lock counts
  against the caller's budget. Updates are processed sequentially, so an
  unbounded wait here is an outage for both users, not a slow reply for one.

A timeout releases the *event loop*, which is what keeps the bot answering;
Python cannot cancel the worker thread underneath, so a timed-out load also
latches the transcriber as unavailable rather than starting a second one.

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
    "timed_out": (
        "That took too long to transcribe and I stopped waiting. "
        "Try a shorter note, or type the meal instead."
    ),
    "too_large": "That recording is too large to process. Try a shorter note.",
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

    def __init__(
        self,
        model_size: str = "base",
        *,
        load_timeout: float | None = None,
        transcribe_timeout: float | None = None,
    ) -> None:
        self._model_size = model_size
        self._model: Any | None = None
        self._load_failed = False
        self._load_timeout = load_timeout
        self._transcribe_timeout = transcribe_timeout
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

    async def ensure_loaded(self) -> Transcript | None:
        """Load the model if needed. ``None`` on success, else the failure.

        Exposed so startup can pay the load cost once, out of any user's way.
        """
        async with self._lock:
            return await self._load_locked()

    async def _load_locked(self) -> Transcript | None:
        """Load the model. Caller holds the lock."""
        if self._model is not None:
            return None
        if self._load_failed:
            return Transcript(reason="model_unavailable")

        try:
            self._model = await _with_timeout(
                asyncio.to_thread(self._load_model), self._load_timeout
            )
        except asyncio.TimeoutError:
            # The worker thread cannot be cancelled and may still be downloading.
            # Latch the failure so the next note fails fast instead of queueing
            # behind it and stalling the bot again.
            self._load_failed = True
            logger.warning(
                "Speech model load exceeded %ss; voice stays unavailable this run",
                self._load_timeout,
            )
            return Transcript(reason="timed_out")

        if self._model is None:
            self._load_failed = True
            # Distinguish "package missing" from "model would not load": the
            # first is an install, the second is usually disk or a failed
            # download, and the user can act on neither the same way.
            try:
                import faster_whisper  # noqa: F401
            except ImportError:
                return Transcript(reason="not_installed")
            return Transcript(reason="model_unavailable")
        return None

    async def transcribe(self, path: str) -> Transcript:
        """Transcribe an audio file at ``path``. Never raises.

        Every stage — queueing behind another note, loading the model, and
        decoding — has its own ceiling, so a caller can never be parked here
        forever. The load keeps its own (larger) budget rather than eating this
        one, because a cold first load legitimately includes a download.
        """
        budget = self._transcribe_timeout
        deadline = None if budget is None else _now() + budget
        try:
            await _with_timeout(self._lock.acquire(), _remaining(deadline))
        except asyncio.TimeoutError:
            logger.info("Gave up waiting for the transcription lock")
            return Transcript(reason="timed_out")

        try:
            failure = await self._load_locked()
            if failure is not None:
                return failure
            try:
                text = await _with_timeout(
                    asyncio.to_thread(self._transcribe_sync, path),
                    _remaining(deadline),
                )
            except asyncio.TimeoutError:
                logger.warning("Transcription exceeded %ss; giving up", budget)
                return Transcript(reason="timed_out")
            except Exception:
                logger.warning("Transcription failed", exc_info=False)
                return Transcript(reason="failed")
        finally:
            self._lock.release()

        text = (text or "").strip()
        if not text:
            return Transcript(reason="empty")
        return Transcript(text=text)


def _now() -> float:
    return asyncio.get_running_loop().time()


def _remaining(deadline: float | None) -> float | None:
    """Seconds left before ``deadline``, never negative. ``None`` = unbounded."""
    if deadline is None:
        return None
    return max(0.0, deadline - _now())


async def _with_timeout(awaitable: Any, timeout: float | None) -> Any:
    """``asyncio.wait_for`` that treats ``None`` as "no limit"."""
    if timeout is None:
        return await awaitable
    return await asyncio.wait_for(awaitable, timeout)
