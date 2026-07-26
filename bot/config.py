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
def _parse_allowed_user_ids(raw: str) -> frozenset[int]:
    """Parse ALLOWED_USER_IDS into validated, positive Telegram IDs.

    Rejects non-integers, zero, negatives, and duplicates with actionable,
    setting-specific errors rather than silently normalizing them — Telegram user
    IDs are always positive. The duplicate diagnostic reports a count, not the ID
    value, so a real Telegram ID is never echoed into a crash log.
    """
    tokens = [tok.strip() for tok in raw.split(",") if tok.strip()]
    if not tokens:
        raise RuntimeError(
            "ALLOWED_USER_IDS not set in .env — add your Telegram user ID "
            "(message @userinfobot to find it)"
        )
    seen: set[int] = set()
    duplicate_count = 0
    for token in tokens:
        try:
            value = int(token)
        except ValueError as exc:
            raise RuntimeError(
                "ALLOWED_USER_IDS must be a comma-separated list of integer "
                f"Telegram IDs; {token!r} is not an integer."
            ) from exc
        if value <= 0:
            raise RuntimeError(
                "ALLOWED_USER_IDS must contain positive Telegram IDs; "
                f"got a non-positive value ({value})."
            )
        if value in seen:
            duplicate_count += 1
        seen.add(value)
    if duplicate_count:
        raise RuntimeError(
            f"ALLOWED_USER_IDS lists {duplicate_count} duplicate ID(s); "
            "include each authorized Telegram ID exactly once."
        )
    return frozenset(seen)


ALLOWED_USER_IDS: frozenset[int] = _parse_allowed_user_ids(
    os.getenv("ALLOWED_USER_IDS", "")
)

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
CONVERSATION_TIMEOUT: int = 300

# ---------------------------------------------------------------------------
# Logging format
# ---------------------------------------------------------------------------
LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
