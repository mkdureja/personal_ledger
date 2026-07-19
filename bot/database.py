"""
Async SQLite database manager for Ledger bot.

Single shared connection with WAL mode, foreign keys, and composite indexes.
Row-presence semantics for habit_logs (no 'completed' column).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

import aiosqlite

logger = logging.getLogger(__name__)

HabitAddStatus = Literal["added", "reactivated", "already_active"]

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS study_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    subject      TEXT NOT NULL,
    duration_min INTEGER NOT NULL,
    notes        TEXT,
    logged_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS gym_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    exercise    TEXT NOT NULL,
    sets        INTEGER NOT NULL,
    reps        INTEGER NOT NULL,
    weight_kg   REAL,
    logged_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS diet_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    meal_type   TEXT NOT NULL CHECK(meal_type IN ('breakfast','lunch','dinner','snack')),
    food_items  TEXT NOT NULL,
    calories    INTEGER,
    logged_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS habits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    habit_name  TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active   INTEGER DEFAULT 1,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS habit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    habit_id    INTEGER NOT NULL,
    log_date    DATE NOT NULL,
    UNIQUE(user_id, habit_id, log_date),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (habit_id) REFERENCES habits(id)
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_study_user_date ON study_logs(user_id, logged_at);
CREATE INDEX IF NOT EXISTS idx_gym_user_date ON gym_logs(user_id, logged_at);
CREATE INDEX IF NOT EXISTS idx_diet_user_date ON diet_logs(user_id, logged_at);
CREATE INDEX IF NOT EXISTS idx_habit_logs_user_date ON habit_logs(user_id, log_date);
"""

# Partial unique index: only active habits must have unique names per user.
# This allows deactivate → re-add without collision.
_PARTIAL_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_habits_active
    ON habits(user_id, habit_name) WHERE is_active = 1;
"""

# Existing databases cannot gain a new foreign key without rebuilding the table.
# These triggers enforce the same ownership and active-habit invariant for both
# existing and newly-created databases, without invalidating historical rows.
_HABIT_LOG_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_habit_logs_validate_insert
BEFORE INSERT ON habit_logs
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM habits AS h
    JOIN users AS u ON u.user_id = h.user_id
    WHERE h.id = NEW.habit_id
      AND h.user_id = NEW.user_id
      AND h.is_active = 1
)
BEGIN
    SELECT RAISE(ABORT, 'habit must be active and belong to user');
END;

CREATE TRIGGER IF NOT EXISTS trg_habit_logs_validate_update
BEFORE UPDATE OF user_id, habit_id ON habit_logs
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM habits AS h
    JOIN users AS u ON u.user_id = h.user_id
    WHERE h.id = NEW.habit_id
      AND h.user_id = NEW.user_id
      AND h.is_active = 1
)
BEGIN
    SELECT RAISE(ABORT, 'habit must be active and belong to user');
END;
"""


def _sqlite_timestamp(value: datetime) -> str:
    """Format a timestamp like SQLite's ``CURRENT_TIMESTAMP``.

    SQLite compares the TIMESTAMP values in this schema as text.  A space must
    separate the date and time; ``datetime.isoformat()`` uses ``T`` and sorts
    after same-day ``CURRENT_TIMESTAMP`` values.
    """
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _utc_timestamp_now() -> str:
    """Return the current UTC time in SQLite-sortable microsecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


class DatabaseManager:
    """Async SQLite manager holding a single shared connection."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the connection and set pragmas."""
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = aiosqlite.Row
        logger.info("Database connected: %s", self.db_path)

    async def init_db(self) -> None:
        """Create tables and indexes if they don't exist."""
        assert self._conn is not None, "Call connect() first"
        async with self._write_operation():
            await self._conn.executescript(_SCHEMA)
            # Indexes must be created individually (executescript doesn't return
            # cursors, but these are safe as IF NOT EXISTS).
            for stmt in _INDEXES.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await self._conn.execute(stmt)
            # Partial unique index
            await self._conn.execute(_PARTIAL_INDEX.strip())
            await self._conn.executescript(_HABIT_LOG_TRIGGERS)
        logger.info("Database schema initialized")

    async def close(self) -> None:
        """Close the connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    @asynccontextmanager
    async def _write_operation(self) -> AsyncIterator[None]:
        """Serialize a complete mutation and close its transaction safely."""
        async with self._write_lock:
            try:
                yield
                await self.conn.commit()
            except BaseException:
                if self.conn.in_transaction:
                    await self.conn.rollback()
                raise

    # -------------------------------------------------------------------
    # Users
    # -------------------------------------------------------------------
    async def ensure_user(
        self, user_id: int, username: str | None, first_name: str | None
    ) -> None:
        """Insert or update user record."""
        async with self._write_operation():
            await self.conn.execute(
                """
                INSERT INTO users (user_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name
                """,
                (user_id, username, first_name),
            )

    # -------------------------------------------------------------------
    # Study
    # -------------------------------------------------------------------
    async def log_study(
        self,
        user_id: int,
        subject: str,
        duration_min: int,
        notes: str | None = None,
    ) -> int:
        """Log a study session. Returns the row ID."""
        async with self._write_operation():
            cursor = await self.conn.execute(
                "INSERT INTO study_logs "
                "(user_id, subject, duration_min, notes, logged_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, subject, duration_min, notes, _utc_timestamp_now()),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    async def get_study_logs(
        self, user_id: int, start_date: date, end_date: date
    ) -> list[aiosqlite.Row]:
        """Get study logs for a user within a local-date range.

        Dates are compared by converting logged_at to the local timezone
        at the application layer, but here we do a rough UTC filter and
        let the caller bucket precisely.  For simplicity we fetch a
        slightly wider window (±1 day) and let the caller filter.
        """
        # Widen by 1 day on each side to handle TZ offset
        start_utc = datetime(start_date.year, start_date.month, start_date.day) - timedelta(days=1)
        end_utc = datetime(end_date.year, end_date.month, end_date.day) + timedelta(days=2)
        cursor = await self.conn.execute(
            "SELECT * FROM study_logs WHERE user_id = ? AND logged_at >= ? AND logged_at < ? "
            "ORDER BY logged_at",
            (user_id, _sqlite_timestamp(start_utc), _sqlite_timestamp(end_utc)),
        )
        return await cursor.fetchall()

    # -------------------------------------------------------------------
    # Gym
    # -------------------------------------------------------------------
    async def log_gym(
        self,
        user_id: int,
        exercise: str,
        sets: int,
        reps: int,
        weight_kg: float | None = None,
    ) -> int:
        """Log a single gym exercise. Returns the row ID."""
        async with self._write_operation():
            cursor = await self.conn.execute(
                "INSERT INTO gym_logs "
                "(user_id, exercise, sets, reps, weight_kg, logged_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, exercise, sets, reps, weight_kg, _utc_timestamp_now()),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    async def get_gym_logs(
        self, user_id: int, start_date: date, end_date: date
    ) -> list[aiosqlite.Row]:
        """Get gym logs within a local-date range (with ±1 day buffer)."""
        start_utc = datetime(start_date.year, start_date.month, start_date.day) - timedelta(days=1)
        end_utc = datetime(end_date.year, end_date.month, end_date.day) + timedelta(days=2)
        cursor = await self.conn.execute(
            "SELECT * FROM gym_logs WHERE user_id = ? AND logged_at >= ? AND logged_at < ? "
            "ORDER BY logged_at",
            (user_id, _sqlite_timestamp(start_utc), _sqlite_timestamp(end_utc)),
        )
        return await cursor.fetchall()

    # -------------------------------------------------------------------
    # Diet
    # -------------------------------------------------------------------
    async def log_diet(
        self,
        user_id: int,
        meal_type: str,
        food_items: str,
        calories: int | None = None,
    ) -> int:
        """Log a diet entry. Returns the row ID."""
        async with self._write_operation():
            cursor = await self.conn.execute(
                "INSERT INTO diet_logs "
                "(user_id, meal_type, food_items, calories, logged_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, meal_type, food_items, calories, _utc_timestamp_now()),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    async def get_diet_logs(
        self, user_id: int, start_date: date, end_date: date
    ) -> list[aiosqlite.Row]:
        """Get diet logs within a local-date range (with ±1 day buffer)."""
        start_utc = datetime(start_date.year, start_date.month, start_date.day) - timedelta(days=1)
        end_utc = datetime(end_date.year, end_date.month, end_date.day) + timedelta(days=2)
        cursor = await self.conn.execute(
            "SELECT * FROM diet_logs WHERE user_id = ? AND logged_at >= ? AND logged_at < ? "
            "ORDER BY logged_at",
            (user_id, _sqlite_timestamp(start_utc), _sqlite_timestamp(end_utc)),
        )
        return await cursor.fetchall()

    # -------------------------------------------------------------------
    # Habits
    # -------------------------------------------------------------------
    async def add_habit(
        self, user_id: int, habit_name: str
    ) -> tuple[int, HabitAddStatus]:
        """Add, reactivate, or find an active habit.

        Returns ``(habit_id, status)`` where status is ``"added"``,
        ``"reactivated"``, or ``"already_active"``.
        """
        async with self._write_operation():
            cursor = await self.conn.execute(
                "SELECT id, is_active FROM habits "
                "WHERE user_id = ? AND habit_name = ? "
                "ORDER BY is_active DESC, id LIMIT 1",
                (user_id, habit_name),
            )
            row = await cursor.fetchone()
            if row is not None and row["is_active"]:
                return row["id"], "already_active"

            if row is not None:
                cursor = await self.conn.execute(
                    """
                    UPDATE habits
                    SET is_active = 1
                    WHERE id = ?
                      AND is_active = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM habits
                          WHERE user_id = ? AND habit_name = ? AND is_active = 1
                      )
                    """,
                    (row["id"], user_id, habit_name),
                )
                if cursor.rowcount > 0:
                    return row["id"], "reactivated"

            cursor = await self.conn.execute(
                """
                INSERT INTO habits (user_id, habit_name)
                VALUES (?, ?)
                ON CONFLICT(user_id, habit_name) WHERE is_active = 1 DO NOTHING
                RETURNING id
                """,
                (user_id, habit_name),
            )
            inserted = await cursor.fetchone()
            if inserted is not None:
                return inserted["id"], "added"

            # This can occur if another database connection made the habit
            # active between this operation's read and write.
            cursor = await self.conn.execute(
                "SELECT id FROM habits "
                "WHERE user_id = ? AND habit_name = ? AND is_active = 1",
                (user_id, habit_name),
            )
            active = await cursor.fetchone()
            if active is None:
                raise RuntimeError("Habit add completed without an active habit")
            return active["id"], "already_active"

    async def deactivate_habit(self, user_id: int, habit_id: int) -> bool:
        """Soft-delete a habit. Returns True if a row was affected."""
        async with self._write_operation():
            cursor = await self.conn.execute(
                "UPDATE habits SET is_active = 0 "
                "WHERE id = ? AND user_id = ? AND is_active = 1",
                (habit_id, user_id),
            )
            return cursor.rowcount > 0

    async def get_active_habits(self, user_id: int) -> list[aiosqlite.Row]:
        """Get all active habits for a user."""
        cursor = await self.conn.execute(
            "SELECT * FROM habits WHERE user_id = ? AND is_active = 1 ORDER BY id",
            (user_id,),
        )
        return await cursor.fetchall()

    async def check_habit(self, user_id: int, habit_id: int, log_date: date) -> bool:
        """Mark a habit as done for a specific local date.

        Returns True if inserted. Returns False if the row already exists or if
        the habit is inactive, missing, or owned by another user.
        """
        async with self._write_operation():
            cursor = await self.conn.execute(
                """
                INSERT INTO habit_logs (user_id, habit_id, log_date)
                SELECT ?, h.id, ?
                FROM habits AS h
                JOIN users AS u ON u.user_id = h.user_id
                WHERE h.id = ? AND h.user_id = ? AND h.is_active = 1
                ON CONFLICT(user_id, habit_id, log_date) DO NOTHING
                """,
                (user_id, log_date.isoformat(), habit_id, user_id),
            )
            return cursor.rowcount > 0

    async def uncheck_habit(self, user_id: int, habit_id: int, log_date: date) -> bool:
        """Remove a habit check for a specific local date.

        Returns True if a row was deleted.
        """
        async with self._write_operation():
            cursor = await self.conn.execute(
                "DELETE FROM habit_logs "
                "WHERE user_id = ? AND habit_id = ? AND log_date = ?",
                (user_id, habit_id, log_date.isoformat()),
            )
            return cursor.rowcount > 0

    async def get_checked_habits(
        self, user_id: int, log_date: date
    ) -> set[int]:
        """Get the set of habit_ids checked on a specific local date."""
        cursor = await self.conn.execute(
            "SELECT habit_id FROM habit_logs WHERE user_id = ? AND log_date = ?",
            (user_id, log_date.isoformat()),
        )
        rows = await cursor.fetchall()
        return {row["habit_id"] for row in rows}

    async def get_habit_logs_range(
        self, user_id: int, start_date: date, end_date: date
    ) -> list[aiosqlite.Row]:
        """Get habit logs in a date range (inclusive)."""
        cursor = await self.conn.execute(
            "SELECT * FROM habit_logs WHERE user_id = ? AND log_date >= ? AND log_date <= ? "
            "ORDER BY log_date",
            (user_id, start_date.isoformat(), end_date.isoformat()),
        )
        return await cursor.fetchall()

    async def get_streak(self, user_id: int, habit_id: int, today: date) -> int:
        """Calculate current streak for a habit.

        Streak = number of consecutive days with a row in habit_logs,
        counting backward from today.  If today is not checked, streak is 0.
        """
        cursor = await self.conn.execute(
            "SELECT log_date FROM habit_logs "
            "WHERE user_id = ? AND habit_id = ? AND log_date <= ? "
            "ORDER BY log_date DESC",
            (user_id, habit_id, today.isoformat()),
        )
        rows = await cursor.fetchall()
        if not rows:
            return 0

        streak = 0
        expected = today
        for row in rows:
            row_date = date.fromisoformat(row["log_date"])
            if row_date == expected:
                streak += 1
                expected -= timedelta(days=1)
            else:
                break
        return streak

    # -------------------------------------------------------------------
    # Undo (delete most recent log across all tables)
    # -------------------------------------------------------------------
    async def undo_last(self, user_id: int) -> dict[str, Any] | None:
        """Delete the most recent log entry for this user (within 24h).

        Checks study_logs, gym_logs, diet_logs. habit_logs use a different
        undo path (uncheck_habit).

        Returns a dict with the deleted entry info, or None if nothing found.
        """
        async with self._write_operation():
            return await self._undo_last_locked(user_id)

    async def _undo_last_locked(self, user_id: int) -> dict[str, Any] | None:
        """Select and delete the latest entry while the write lock is held."""
        tables = [
            ("study_logs", "📖 Study"),
            ("gym_logs", "🏋️ Gym"),
            ("diet_logs", "🍽️ Diet"),
        ]
        latest: dict[str, Any] | None = None
        latest_table: str | None = None

        for table_name, label in tables:
            cursor = await self.conn.execute(
                f"SELECT *, '{label}' as category FROM {table_name} "  # noqa: S608
                f"WHERE user_id = ? ORDER BY logged_at DESC, id DESC LIMIT 1",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                continue

            logged_at_str = row["logged_at"]
            if logged_at_str is None:
                continue

            logged_at = datetime.fromisoformat(logged_at_str)
            # SQLite CURRENT_TIMESTAMP is naive UTC — make it aware
            if logged_at.tzinfo is None:
                logged_at = logged_at.replace(tzinfo=timezone.utc)
            # Check if within 24h
            if (datetime.now(timezone.utc) - logged_at).total_seconds() > 86400:
                continue

            if latest is None or logged_at_str > latest["logged_at"]:
                latest = dict(row)
                latest_table = table_name

        if latest is None or latest_table is None:
            return None

        cursor = await self.conn.execute(
            f"DELETE FROM {latest_table} WHERE id = ? AND user_id = ?",  # noqa: S608
            (latest["id"], user_id),
        )
        if cursor.rowcount <= 0:
            return None
        return latest

    # -------------------------------------------------------------------
    # Summary helpers
    # -------------------------------------------------------------------
    async def get_today_study_total(self, user_id: int, local_today: date) -> int:
        """Total study minutes for today (local date)."""
        logs = await self.get_study_logs(user_id, local_today, local_today)
        from .config import local_date_from_utc

        total = 0
        for row in logs:
            if local_date_from_utc(datetime.fromisoformat(row["logged_at"])) == local_today:
                total += row["duration_min"]
        return total

    async def get_today_calories(self, user_id: int, local_today: date) -> tuple[int, bool]:
        """Total calories for today. Returns (total, has_incomplete).

        has_incomplete is True if any meal has NULL calories.
        """
        logs = await self.get_diet_logs(user_id, local_today, local_today)
        from .config import local_date_from_utc

        total = 0
        has_incomplete = False
        for row in logs:
            if local_date_from_utc(datetime.fromisoformat(row["logged_at"])) == local_today:
                if row["calories"] is not None:
                    total += row["calories"]
                else:
                    has_incomplete = True
        return total, has_incomplete

    async def get_users_with_unchecked_habits(
        self, allowed_ids: frozenset[int], local_today: date
    ) -> dict[int, list[str]]:
        """For each allowed user, get list of unchecked habit names for today.

        Returns {user_id: [habit_name, ...]} — only users with unchecked habits.
        """
        result: dict[int, list[str]] = {}
        for uid in allowed_ids:
            habits = await self.get_active_habits(uid)
            if not habits:
                continue
            checked = await self.get_checked_habits(uid, local_today)
            unchecked = [h["habit_name"] for h in habits if h["id"] not in checked]
            if unchecked:
                result[uid] = unchecked
        return result
