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
_env_path = Path(__file__).resolve().parent.parent / ".env"
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
# a separate, per-user, default-OFF consent decision.
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
# Chosen by measurement against the live free tier, not by reputation:
#   gemini-flash-lite-latest  1.6s, correct segmentation, has quota  <- default
#   gemini-flash-latest       2.9s, correct, but only 5 requests/minute free
#   gemini-2.0-flash          free-tier quota of *zero* — unusable
#   gemini-2.5-flash(-lite)   HTTP 404 for generateContent on this tier
# Splitting a phrase into items needs no reasoning, so the lite tier is the right
# fit; thinkingBudget=0 is not an option because the newer Flash models reject it
# with HTTP 400. An alias rather than a pin, because the pinned names above turned
# out to be the unreliable ones. Override in .env if a specific model is needed.
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest").strip()
GEMINI_AVAILABLE: bool = bool(GEMINI_API_KEY)

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
