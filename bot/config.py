"""
Configuration loader for Ledger bot.

Reads .env, exposes typed constants, and provides timezone helpers.
"""

import os
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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

_default_db_path = str(Path(__file__).resolve().parent.parent / "ledger.db")
DB_PATH: str = os.getenv("DB_PATH", _default_db_path)

# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------
LOCAL_TZ: ZoneInfo = ZoneInfo(os.getenv("TZ", "Asia/Kolkata"))


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
_raw_ids = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: frozenset[int] = frozenset(
    int(uid.strip()) for uid in _raw_ids.split(",") if uid.strip()
)
if not ALLOWED_USER_IDS:
    raise RuntimeError(
        "ALLOWED_USER_IDS not set in .env — add your Telegram user ID "
        "(message @userinfobot to find it)"
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
