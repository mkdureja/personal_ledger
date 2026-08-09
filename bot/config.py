"""
Configuration loader for Ledger bot.

Reads .env, exposes typed constants, and provides timezone helpers.
"""

import os
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env from project root (one level up from bot/)
# ---------------------------------------------------------------------------
# ``load_dotenv`` does not override what is already in the environment, so an
# explicit value always wins — but anything the caller *omits* still comes from
# the deployment .env. For the test suite that is a leak in one direction only,
# and a nasty one: every newly added setting silently becomes something the
# suite reads from a real installation. It has already bitten twice (real
# PHASE1_ENABLED_USER_IDS failing collection; a real GEMINI_API_KEY able to send
# test meal text to Google), and pinning each setting as it appears only ever
# fixes the settings someone remembered.
#
# ``LEDGER_SKIP_DOTENV`` closes the class instead: tests declare every value they
# depend on and read no deployment file at all. It is never set in production.
_env_path = Path(__file__).resolve().parent.parent / ".env"
if os.getenv("LEDGER_SKIP_DOTENV", "").strip().lower() not in {"1", "true", "yes", "on"}:
    load_dotenv(_env_path)

# ---------------------------------------------------------------------------
# Core config
# ---------------------------------------------------------------------------
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set in .env — get one from @BotFather")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_under_root(raw: str) -> str:
    """Resolve a configured path against the project root when it is relative.

    Keeps history in one place no matter which working directory the process is
    launched from. ``:memory:`` (SQLite in-memory) is passed through untouched.
    """
    if raw == ":memory:":
        return raw
    path = Path(raw)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return str(path)


DB_PATH: str = _resolve_under_root(os.getenv("DB_PATH", str(_PROJECT_ROOT / "ledger.db")))

# ---------------------------------------------------------------------------
# Backup destination for the pre-migration preflight
# ---------------------------------------------------------------------------
# Directory that receives the verified pre-migration backup. It must resolve
# *outside* the project root — the same containment rule ``--dest`` enforces,
# because a rollback copy beside ``ledger.db`` shares the disk and the accidental
# deletion it exists to survive.
#
# Unset is legal while the schema is current: an ordinary start with no pending
# migration needs no backup. It becomes a hard startup error only when a non-empty
# migration is pending (see ``bot/migration_preflight.py``), which is exactly the
# moment the rollback point matters. Validated here so a bad value fails at import
# rather than halfway through a deployment.
BACKUP_DEST_DIR: str = os.getenv("BACKUP_DEST_DIR", "").strip()
if BACKUP_DEST_DIR:
    _backup_dest = Path(BACKUP_DEST_DIR).expanduser().resolve()
    if _backup_dest == _PROJECT_ROOT or _PROJECT_ROOT in _backup_dest.parents:
        raise RuntimeError(
            f"BACKUP_DEST_DIR must resolve outside the project root; "
            f"{_backup_dest} is inside it. Choose a directory on another path "
            "(see docs/backup_runbook.md)."
        )
    BACKUP_DEST_DIR = str(_backup_dest)

# ---------------------------------------------------------------------------
# Optional Gemini parsing (Release 4) — absent by default
# ---------------------------------------------------------------------------
# Unset is the normal, supported state: with no key the typed-meal parser stays
# fully deterministic and nothing about logging changes. The key is read here so
# there is exactly one place it enters the process, and it is never logged,
# echoed into a message, or written to the database.
#
# This is only the *capability*. Whether a given user's meal text may be sent is
# still a separate, per-user decision — see AI_PARSING_DEFAULT_ON below for what
# answers that question when the user has not made one.
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()

# What a user who has never run ``/aiparse`` is treated as having chosen.
#
# This started life as default-OFF, on the principle that consent cannot be
# inferred. It is now default-ON at the owner's explicit instruction, which is
# his to give for his own household bot — but the principle is kept where it
# actually matters: this default only ever answers for a user who has *not*
# chosen (schema v15 stores that as NULL, distinct from a real 0). An explicit
# ``/aiparse off`` is a decision, and no default overrides it. Nothing here
# writes a consent timestamp, because nobody consented; and the per-message
# disclosure in the preview still tells each user, on the message where it
# happened, that the text was sent.
AI_PARSING_DEFAULT_ON: bool = os.getenv(
    "AI_PARSING_DEFAULT_ON", "true"
).strip().casefold() in {"1", "true", "yes", "on"}
# Chosen by measurement against the live free tier, and re-measured after the
# first choice degraded in service:
#   gemini-flash-latest       3.6s, correct, 5 requests/minute free   <- default
#   gemini-flash-lite-latest  measured 1.6s, then began hanging past 45s the same
#                             day, on phrases it had previously answered
#   gemini-2.0-flash          free-tier quota of *zero* — unusable
#   gemini-2.5-flash(-lite)   HTTP 404 for generateContent on this tier
#
# The lite alias is left documented rather than deleted: it was genuinely the
# best option when measured, and the lesson is that a moving alias can degrade
# under you. Anything here can be overridden in .env without a code change, which
# is the actual mitigation. Note thinkingBudget=0 is not available — the newer
# Flash models reject it with HTTP 400 — so slower thinking tiers cannot be made
# fast; they can only be swapped out.
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip()
GEMINI_AVAILABLE: bool = bool(GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# Optional local voice transcription (Release 5) — off by default
# ---------------------------------------------------------------------------
# Default-off because enabling it downloads a speech model (hundreds of MB) on
# first use. Unlike Gemini parsing, this needs no consent switch: the audio never
# leaves the host, which is exactly why transcription is local while parsing may
# be remote.
VOICE_ENABLED: bool = os.getenv("VOICE_ENABLED", "").strip().lower() in {
    "1", "true", "yes", "on"
}
# Whisper size. "base" is the smallest that handles ordinary meal dictation;
# "tiny" is faster and noticeably worse at food names.
VOICE_MODEL_SIZE: str = os.getenv("VOICE_MODEL_SIZE", "base").strip() or "base"


def _positive_int(name: str, default: int, *, maximum: int) -> int:
    """Read a bounded positive integer, failing at import rather than at use."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a whole number; got {raw!r}.") from exc
    if value <= 0 or value > maximum:
        raise RuntimeError(f"{name} must be between 1 and {maximum}; got {value}.")
    return value


# A hard cap on accepted audio, checked from Telegram's declared duration before
# anything is downloaded. A meal description is a sentence; anything much longer
# is a mistake, and transcription cost grows with length.
VOICE_MAX_SECONDS: int = _positive_int("VOICE_MAX_SECONDS", 60, maximum=600)

# Wall-clock ceilings on the two operations that can hang indefinitely. Updates
# are processed sequentially (``concurrent_updates(False)``), so anything that
# blocks in a handler blocks *both* users — an unreachable model download or a
# wedged decoder would otherwise take the whole bot down with no diagnostic.
#
# The load budget is generous because a cold first load downloads the model
# (~21s measured for "base", including the download); the transcribe budget is
# per note, on audio already capped at VOICE_MAX_SECONDS.
VOICE_LOAD_TIMEOUT_SECONDS: int = _positive_int(
    "VOICE_LOAD_TIMEOUT_SECONDS", 300, maximum=1800
)
VOICE_TRANSCRIBE_TIMEOUT_SECONDS: int = _positive_int(
    "VOICE_TRANSCRIBE_TIMEOUT_SECONDS", 120, maximum=900
)

# A voice note that passed the duration check can still be an enormous file if
# the declared duration was wrong. Opus voice notes run well under 32 kB/s, so
# this is roomy for a legitimate note of the maximum length.
VOICE_MAX_FILE_BYTES: int = _positive_int(
    "VOICE_MAX_FILE_BYTES", 8 * 1024 * 1024, maximum=64 * 1024 * 1024
)

# Load the speech model during startup instead of inside the first user's
# request. Costs a slower start; keeps the first voice note from paying a
# multi-second stall that looks like the bot ignoring them.
VOICE_PRELOAD: bool = os.getenv("VOICE_PRELOAD", "true").strip().lower() in {
    "1", "true", "yes", "on"
}

# Optional routine file (motivational anchors). When absent, the bot falls
# back to the single legacy reminder configured by REMINDER_HOUR below.
ROUTINE_PATH: str = _resolve_under_root(
    os.getenv("ROUTINE_PATH", str(_PROJECT_ROOT / "routine.yaml"))
)

# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------
_tz_name = os.getenv("TZ", "Asia/Kolkata")
try:
    LOCAL_TZ: ZoneInfo = ZoneInfo(_tz_name)
except (ZoneInfoNotFoundError, ValueError) as exc:
    raise RuntimeError(
        f"TZ={_tz_name!r} is not a valid IANA timezone (e.g. 'Asia/Kolkata'). "
        "Check TZ in .env and ensure the tzdata package is installed."
    ) from exc


def today_local() -> date:
    """Current date in the configured local timezone."""
    return datetime.now(LOCAL_TZ).date()


def now_local() -> datetime:
    """Current datetime in the configured local timezone."""
    return datetime.now(LOCAL_TZ)


def localize(utc_dt: datetime) -> datetime:
    """Convert a UTC datetime (or naive, assumed UTC) to local timezone."""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(LOCAL_TZ)


def local_date_from_utc(utc_dt: datetime) -> date:
    """Extract the local date from a UTC datetime."""
    return localize(utc_dt).date()


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
def _parse_id_tokens(
    raw: str, setting: str, *, allow_empty: bool
) -> frozenset[int]:
    """Parse a comma-separated Telegram-ID list into validated positive IDs.

    Rejects non-integers, zero, negatives, and duplicates with actionable,
    setting-specific errors rather than silently normalizing them — Telegram user
    IDs are always positive. The duplicate diagnostic reports a count, not the ID
    value, so a real Telegram ID is never echoed into a crash log. When
    ``allow_empty`` is false an empty list is itself an error (used for the
    mandatory ``ALLOWED_USER_IDS``); the Phase 1 rollout lists default to empty.
    """
    tokens = [tok.strip() for tok in raw.split(",") if tok.strip()]
    if not tokens:
        if allow_empty:
            return frozenset()
        raise RuntimeError(
            f"{setting} not set in .env — add your Telegram user ID "
            "(message @userinfobot to find it)"
        )
    seen: set[int] = set()
    duplicate_count = 0
    for token in tokens:
        try:
            value = int(token)
        except ValueError as exc:
            raise RuntimeError(
                f"{setting} must be a comma-separated list of integer "
                f"Telegram IDs; {token!r} is not an integer."
            ) from exc
        if value <= 0:
            raise RuntimeError(
                f"{setting} must contain positive Telegram IDs; "
                f"got a non-positive value ({value})."
            )
        if value in seen:
            duplicate_count += 1
        seen.add(value)
    if duplicate_count:
        raise RuntimeError(
            f"{setting} lists {duplicate_count} duplicate ID(s); "
            "include each authorized Telegram ID exactly once."
        )
    return frozenset(seen)


def _parse_allowed_user_ids(raw: str) -> frozenset[int]:
    """Parse the mandatory ``ALLOWED_USER_IDS`` list (must be non-empty)."""
    return _parse_id_tokens(raw, "ALLOWED_USER_IDS", allow_empty=False)


ALLOWED_USER_IDS: frozenset[int] = _parse_allowed_user_ids(
    os.getenv("ALLOWED_USER_IDS", "")
)

# ---------------------------------------------------------------------------
# Phase 1 rollout flags (read once at startup; changing them needs a restart)
# ---------------------------------------------------------------------------
# ``PHASE1_ENABLED_USER_IDS`` is a subset of ``ALLOWED_USER_IDS``; empty means
# Phase 1 Home and fast mutations are off for everyone.
PHASE1_ENABLED_USER_IDS: frozenset[int] = _parse_id_tokens(
    os.getenv("PHASE1_ENABLED_USER_IDS", ""),
    "PHASE1_ENABLED_USER_IDS",
    allow_empty=True,
)
_phase1_not_allowed = PHASE1_ENABLED_USER_IDS - ALLOWED_USER_IDS
if _phase1_not_allowed:
    raise RuntimeError(
        "PHASE1_ENABLED_USER_IDS must be a subset of ALLOWED_USER_IDS; "
        f"{len(_phase1_not_allowed)} ID(s) are not authorized."
    )

# ``HOME_KEYBOARD_MODE`` is exactly one of off/pilot/on/remove.
#   off    — never send the persistent keyboard; remove a stale one.
#   pilot  — send only to HOME_KEYBOARD_PILOT_USER_IDS; remove for others.
#   on     — send to every Phase 1-enabled user; remove for others.
#   remove — send ReplyKeyboardRemove to every authorized user (rollback).
HOME_KEYBOARD_MODES = ("off", "pilot", "on", "remove")
HOME_KEYBOARD_MODE: str = os.getenv("HOME_KEYBOARD_MODE", "off").strip()
if HOME_KEYBOARD_MODE not in HOME_KEYBOARD_MODES:
    raise RuntimeError(
        "HOME_KEYBOARD_MODE must be one of "
        f"{', '.join(HOME_KEYBOARD_MODES)}; got {HOME_KEYBOARD_MODE!r}."
    )

# ``HOME_KEYBOARD_PILOT_USER_IDS`` is a subset of both ALLOWED_USER_IDS and
# PHASE1_ENABLED_USER_IDS. It is consulted only in ``pilot`` mode.
HOME_KEYBOARD_PILOT_USER_IDS: frozenset[int] = _parse_id_tokens(
    os.getenv("HOME_KEYBOARD_PILOT_USER_IDS", ""),
    "HOME_KEYBOARD_PILOT_USER_IDS",
    allow_empty=True,
)
_pilot_not_enabled = HOME_KEYBOARD_PILOT_USER_IDS - PHASE1_ENABLED_USER_IDS
if _pilot_not_enabled:
    raise RuntimeError(
        "HOME_KEYBOARD_PILOT_USER_IDS must be a subset of "
        "PHASE1_ENABLED_USER_IDS (and thus ALLOWED_USER_IDS); "
        f"{len(_pilot_not_enabled)} ID(s) are not Phase 1-enabled."
    )


def phase1_enabled_for(user_id: int) -> bool:
    """Whether Phase 1 Home and fast mutations are enabled for this user."""
    return user_id in PHASE1_ENABLED_USER_IDS


def home_keyboard_action_for(user_id: int) -> str:
    """Return ``"send"`` or ``"remove"`` for this user's persistent keyboard.

    Encodes the effective-keyboard table: ``off``/``remove`` always remove,
    ``pilot`` sends only to the pilot list, ``on`` sends to every Phase
    1-enabled user; everyone else has the stale keyboard removed.
    """
    if HOME_KEYBOARD_MODE == "pilot":
        return "send" if user_id in HOME_KEYBOARD_PILOT_USER_IDS else "remove"
    if HOME_KEYBOARD_MODE == "on":
        return "send" if user_id in PHASE1_ENABLED_USER_IDS else "remove"
    # "off" and "remove" never send.
    return "remove"

# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------
try:
    REMINDER_HOUR: int = int(os.getenv("REMINDER_HOUR", "20"))
except (ValueError, TypeError) as exc:
    raise RuntimeError(
        "REMINDER_HOUR must be an integer 0–23 in .env"
    ) from exc
if not 0 <= REMINDER_HOUR <= 23:
    raise RuntimeError(
        f"REMINDER_HOUR must be 0–23, got {REMINDER_HOUR}"
    )
# Note: PTB handles DST-aware tzinfo correctly for ``run_daily``, but if
# ``TZ`` is set to a zone with DST transitions the wall-clock hour of the
# reminder may shift by one hour during the transition day.  Asia/Kolkata
# (the default) has no DST, so this is a non-issue for the default config.
REMINDER_TIME: time = time(hour=REMINDER_HOUR, minute=0, second=0, tzinfo=LOCAL_TZ)

# ---------------------------------------------------------------------------
# Conversation timeout (seconds)
# ---------------------------------------------------------------------------
# Idle window before a guided flow is abandoned. Assembling a multi-item meal
# involves real-world pauses — reading a label, finishing a bite — and five
# minutes was short enough to discard drafts mid-use. Fifteen is forgiving
# without leaving a forgotten flow blocking new commands for long, and /cancel
# is always available regardless.
CONVERSATION_TIMEOUT: int = 900

# ---------------------------------------------------------------------------
# Logging format
# ---------------------------------------------------------------------------
LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
